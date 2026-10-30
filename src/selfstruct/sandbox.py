from __future__ import annotations

import hashlib
import importlib.resources
import json
import math
from pathlib import Path
import threading
import time
import zlib

import wasmtime as wasm
from PIL import Image

from .config import atomic_write, write_json


def features(image: Image.Image) -> tuple[int, bytes]:
    raw = image.convert("L").resize((32, 32)).tobytes()
    # Quantization makes exact hash reuse less sensitive to small rendering noise.
    hashed = hashlib.blake2s(bytes(v // 16 for v in raw), digest_size=4).digest()
    return int.from_bytes(hashed, "little") & 0x7FFFFFFF, raw


def difference(a: Image.Image, b: Image.Image) -> float:
    ar, br = features(a)[1], features(b)[1]
    return sum(abs(x - y) for x, y in zip(ar, br)) / (255 * len(ar))


class Sandbox:
    """No imports, no WASI, no OS handles, finite fuel and linear memory."""
    def __init__(self, source: str):
        if len(source.encode()) > 256 * 1024:
            raise ValueError("Candidate source too large")
        config = wasm.Config()
        config.consume_fuel = True
        config.max_wasm_stack = 256 * 1024
        self.engine = wasm.Engine(config)
        self.module = wasm.Module(self.engine, wasm.wat2wasm(source))
        if self.module.imports:
            raise ValueError("OS, network and external imports are forbidden")
        self.store = wasm.Store(self.engine)
        self.store.set_limits(memory_size=16 * 1024**2, table_elements=2048, instances=1, tables=2, memories=1)
        self.store.set_fuel(2_000_000)
        self.instance = wasm.Instance(self.store, self.module, [])
        self.exports = self.instance.exports(self.store)
        expected = {"choose": 2, "duration": 1, "wait": 1, "predict": 2, "learn": 4}
        for name, count in expected.items():
            func = self.exports.get(name)
            if not isinstance(func, wasm.Func):
                raise ValueError("Missing agent export: " + name)
            typ = func.type(self.store)
            results = 0 if name == "learn" else 1
            if len(typ.params) != count or len(typ.results) != results or any(str(t) != "i32" for t in [*typ.params, *typ.results]):
                raise ValueError("Invalid agent ABI: " + name)
        self.memory = self.exports.get("memory")
        if not isinstance(self.memory, wasm.Memory) or self.memory.data_len(self.store) < 4096:
            raise ValueError("Agent requires memory for observed pixels")

    def call(self, name: str, *args: int):
        self.store.set_fuel(2_000_000)
        return self.exports[name](self.store, *args)

    def pixels(self, raw: bytes, offset: int = 0) -> None:
        if len(raw) != 1024 or offset not in {0, 1024}:
            raise ValueError("Invalid observation")
        self.memory.write(self.store, raw, offset)

    def action(self, feature: int, count: int) -> tuple[int, float, float, float]:
        choice = self.call("choose", feature, count)
        duration = self.call("duration", feature)
        wait = self.call("wait", feature)
        pred = self.call("predict", feature, choice)
        if not (0 <= choice < count and 10 <= duration <= 500 and 10 <= wait <= 2000 and -1 <= pred <= 1000):
            raise ValueError("Agent output violates input policy")
        return choice, duration / 1000, wait / 1000, pred / 1000 if pred >= 0 else -1

    def requests(self, files, reserve, observe=None, dispatch=None) -> None:
        """Optional capability protocol: JSON request at 4096; reply at 16384.

        This broker, not WASI or Python, is the only generated-code file access.
        Self code written here remains a candidate until separately evaluated.
        """
        if "request" not in self.exports:
            return
        for _ in range(4):
            size = self.call("request")
            if size == 0:
                break
            if not isinstance(size, int) or not 1 <= size <= 8192:
                raise ValueError("Invalid capability request")
            request = json.loads(bytes(self.memory.read(self.store, 4096, 4096 + size)))
            try:
                op = request["op"]
                path = request.get("path", "")
                if op == "write":
                    data = request["text"].encode()
                    reserve(len(data) + 4096)
                    files.write(path, data)
                    response = {"ok": True}
                elif op == "read":
                    response = {"ok": True, "text": files.read(path).decode()}
                elif op == "delete":
                    files.delete(path)
                    response = {"ok": True}
                elif op == "mkdir":
                    files.mkdir(path)
                    response = {"ok": True}
                elif op == "list":
                    response = {"ok": True, "files": files.list(path)}
                elif op == "observe" and observe is not None:
                    response = {"ok": True, **observe(request)}
                elif dispatch is not None:
                    response = {"ok": True, **dispatch(request)}
                else:
                    raise ValueError("Capability not granted")
            except (ValueError, OSError, KeyError) as exc:
                response = {"ok": False, "error": type(exc).__name__}
            raw = json.dumps(response, ensure_ascii=False).encode()
            if len(raw) > 16384:
                raw = b'{"ok":false,"error":"ResponseTooLarge"}'
            self.memory.write(self.store, len(raw).to_bytes(4, "little") + raw, 16384)

    def checkpoint(self) -> bytes:
        return zlib.compress(bytes(self.memory.read(self.store)), level=3)

    def restore(self, compressed: bytes) -> None:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, 16 * 1024**2 + 1)
        if len(raw) > 16 * 1024**2 or not decoder.eof or len(raw) % 65536:
            raise ValueError("Invalid runtime checkpoint")
        current = self.memory.data_len(self.store)
        if len(raw) > current:
            self.memory.grow(self.store, (len(raw) - current) // 65536)
        self.memory.write(self.store, raw)


class Revisions:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.state_path = root / "state.json"
        if not self.state_path.exists():
            seed = importlib.resources.files("selfstruct").joinpath("assets/seed.wat").read_text()
            rid = self.create(seed)
            write_json(self.state_path, {"active": rid, "stable": rid, "previous": rid, "trial": False})
        self.state = json.loads(self.state_path.read_text())
        try:
            self.load()
        except Exception:
            self.rollback()
            self.load()

    @property
    def active(self) -> str:
        return self.state["active"]

    def source(self, rid: str | None = None) -> str:
        rid = rid or self.active
        if len(rid) != 64 or any(ch not in "0123456789abcdef" for ch in rid):
            raise ValueError("Invalid revision")
        source = (self.root / rid / "agent.wat").read_text()
        if hashlib.sha256(source.encode()).hexdigest() != rid:
            raise ValueError("Revision integrity failure")
        return source

    def create(self, source: str) -> str:
        Sandbox(source)
        rid = hashlib.sha256(source.encode()).hexdigest()
        path = self.root / rid / "agent.wat"
        if not path.exists():
            atomic_write(path, source.encode())
        return rid

    def load(self) -> Sandbox:
        return Sandbox(self.source())

    def promote(self, rid: str) -> None:
        Sandbox(self.source(rid))
        with self.lock:
            if self.state["trial"]:
                raise ValueError("Previous revision still on probation")
            self.state.update(previous=self.state["stable"], active=rid, trial=True)
            write_json(self.state_path, self.state)

    def confirm(self) -> None:
        with self.lock:
            self.state.update(stable=self.active, trial=False)
            write_json(self.state_path, self.state)

    def rollback(self) -> None:
        with self.lock:
            old = self.state["previous"] if self.state["stable"] == self.active else self.state["stable"]
            self.state.update(active=old, stable=old, trial=False)
            write_json(self.state_path, self.state)


def replay(source: str, rows: list[dict], evidence: Path, keys: list[str]) -> dict:
    agent = Sandbox(source)
    errors, known, times = [], 0, []
    samples = []
    for row in rows:
        if row["action"] not in keys:
            raise ValueError("Replay keys differ from experiment configuration")
        with Image.open(evidence / (row["before_hash"] + ".png")) as image:
            feature, raw = features(image)
        with Image.open(evidence / (row["after_hash"] + ".png")) as image:
            after, after_raw = features(image)
        agent.pixels(raw)
        started = time.perf_counter()
        agent.action(feature, len(keys))
        prediction = agent.call("predict", feature, keys.index(row["action"]))
        elapsed = time.perf_counter() - started
        if not -1 <= prediction <= 1000:
            raise ValueError("Invalid replay prediction")
        outcome = int(row["change"] > 0.01)
        loss = 0.25 if prediction == -1 else (prediction / 1000 - outcome)**2
        errors.append(loss)
        known += prediction >= 0
        times.append(elapsed)
        samples.append({"id": row["id"], "loss": loss, "known": prediction >= 0})
        # Reveal the outcome only after scoring the prediction.
        agent.pixels(after_raw, 1024)
        agent.call("learn", feature, keys.index(row["action"]), after, outcome)
    n = len(rows)
    return {"n": n, "loss": sum(errors) / max(n, 1), "unknown": 1 - known / max(n, 1),
            "seconds": sum(times) / max(n, 1), "samples": samples}


def improves(baseline: dict, candidate: dict) -> bool:
    # Paired held-out comparison: require both practical and statistical improvement.
    if baseline["n"] < 24 or candidate["n"] != baseline["n"]:
        return False
    gains = [a["loss"] - b["loss"] for a, b in zip(baseline["samples"], candidate["samples"])]
    mean = sum(gains) / len(gains)
    variance = sum((g - mean)**2 for g in gains) / max(1, len(gains) - 1)
    bound = 1.96 * math.sqrt(variance / len(gains))
    regression = any(a["known"] and a["loss"] < .04 and b["loss"] > .16
                     for a, b in zip(baseline["samples"], candidate["samples"]))
    return (mean > max(.02, bound) and not regression and candidate["unknown"] <= baseline["unknown"]
            and candidate["seconds"] <= max(.005, baseline["seconds"] * 2))
