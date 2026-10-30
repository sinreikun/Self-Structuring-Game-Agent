from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import queue
import threading
import time
import uuid
import hashlib
import json

from PIL import Image

from .config import Config, InstanceLock, RootFiles, atomic_write, write_json
from .research import LocalLLM, Scientist
from .sandbox import Revisions, difference, features
from .store import CapacityError, Store
from .windows import WindowMinimized, GameUnavailable, WindowsGame

log = logging.getLogger(__name__)


class Controller:
    def __init__(self, config: Config, game=None):
        self.config = config
        self.root = Path(config.root).resolve()
        (self.root / "host").mkdir(parents=True, exist_ok=True)
        self.instance_lock = InstanceLock(self.root / "host" / "experiment.lock")
        self.closed = False
        self.messages: queue.Queue[str] = queue.Queue(maxsize=100)
        self.latest: Image.Image | None = None
        self.latest_before: Image.Image | None = None
        self.status = "停止中"
        self.started = time.monotonic()
        self.steps = 0
        self.last_progress = time.monotonic()
        self.stopped = threading.Event()
        self.paused = threading.Event()
        self.cancel_input = threading.Event()
        self.thread: threading.Thread | None = None
        self.game = game
        self.restarts = 0
        self.error = ""
        self.experiment = self._experiment()
        self.store = Store(self.experiment, config.quota_bytes)
        self.store.account_root = self.root
        self.revisions = Revisions(self.experiment / "agents" / "revisions")
        self.agent = self.revisions.load()
        self.agent_id = self.revisions.active
        self.last_eid = 0
        self.files = RootFiles(self.experiment / "agents" / "workspace", min(config.quota_bytes // 10, 256 * 1024**2))
        self.confirm_steps = 0
        self.session = self.store.session(config.mode, config.resume)
        self._restore_learning()
        self.scientist = Scientist(self.store, self.revisions, LocalLLM(config.llm_url, config.llm_model),
                                   config.keys, config.research_every, self.notify)
        handler = RotatingFileHandler(self.root / "host" / "host.log", maxBytes=2 * 1024**2, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        self.log_handler = handler
        logging.getLogger("selfstruct").addHandler(handler)
        logging.getLogger("selfstruct").setLevel(logging.INFO)

    def _restore_learning(self):
        checkpoint = self.store.root / "host" / "checkpoint.json"
        restored = 0
        if checkpoint.exists():
            state = json.loads(checkpoint.read_text(encoding="utf-8"))
            if state["revision"] == self.agent_id:
                raw = (checkpoint.parent / state["file"]).read_bytes()
                if hashlib.sha256(raw).hexdigest() != state["hash"]:
                    raise ValueError("Runtime checkpoint integrity failure")
                self.agent.restore(raw)
                restored = state["event"]
        for row in self.store.rows(512):
            self.last_eid = row["id"]
            if row["id"] <= restored:
                continue
            if row["action"] not in self.config.keys:
                continue
            with Image.open(self.store.root / "evidence" / (row["before_hash"] + ".png")) as image:
                before, raw = features(image)
            with Image.open(self.store.root / "evidence" / (row["after_hash"] + ".png")) as image:
                after, after_raw = features(image)
            self.agent.pixels(raw)
            self.agent.pixels(after_raw, 1024)
            self.agent.call("learn", before, self.config.keys.index(row["action"]), after, int(row["change"] > .01))

    def checkpoint(self):
        raw = self.agent.checkpoint()
        self.store.reserve(len(raw) + 8192)
        digest = hashlib.sha256(raw).hexdigest()
        folder = self.store.root / "host"
        name = f"checkpoint-{digest}.bin"
        atomic_write(folder / name, raw)
        write_json(folder / "checkpoint.json", {"revision": self.agent_id, "event": self.last_eid, "file": name, "hash": digest})
        # These are replaceable caches, not primary evidence or revision history.
        for old in folder.glob("checkpoint-*.bin"):
            if old.name != name:
                old.unlink()

    def _experiment(self) -> Path:
        active_path = self.root / "host" / "experiment.json"
        if active_path.exists() and (self.config.resume or self.config.mode == "Experienced"):
            import json
            state = json.loads(active_path.read_text(encoding="utf-8"))
            path = self.root / "experiments" / state["id"]
            if not path.resolve().is_relative_to((self.root / "experiments").resolve()):
                raise ValueError("Invalid experiment")
            if state["keys"] != self.config.keys or state["game_exe"] != self.config.game_exe:
                raise ValueError("引き継ぐ個体とゲーム・入力一覧が異なります。NEWBORNで新規個体を作成してください。")
            if state["mode"] != self.config.mode and self.config.mode != "Experienced":
                raise ValueError("異なる知識モードの個体は引き継げません。")
            if self.config.mode == "Experienced" and state["mode"] == "Guided":
                raise ValueError("人間知識を含む個体はGuidedとして継続してください。")
            return path
        if self.config.mode == "Experienced":
            raise ValueError("引き継ぐ個体がありません。Newbornで開始してください。")
        identifier = uuid.uuid4().hex
        path = self.root / "experiments" / identifier
        path.mkdir(parents=True)
        write_json(active_path, {"id": identifier, "mode": self.config.mode, "keys": self.config.keys, "game_exe": self.config.game_exe})
        return path

    def notify(self, text: str):
        try:
            self.messages.put_nowait(text)
        except queue.Full:
            try:
                self.messages.get_nowait()
            except queue.Empty:
                pass
            self.messages.put_nowait(text)

    def start(self):
        if self.thread and self.thread.is_alive():
            self.cancel_input.clear()
            self.paused.clear()
            return
        self.stopped.clear()
        self.scientist.start()
        self.thread = threading.Thread(target=self.run, name="player", daemon=True)
        self.thread.start()
        threading.Thread(target=self.watchdog, name="watchdog", daemon=True).start()

    def pause(self):
        self.paused.set()
        self.cancel_input.set()
        if self.game:
            self.game.release()
        self.status = "一時停止中"

    def stop(self):
        self.stopped.set()
        self.cancel_input.set()
        self.scientist.stop_event.set()
        self.scientist.wake.set()
        if self.game:
            self.game.release()

    def close(self):
        if self.closed:
            return
        self.stop()
        if self.thread:
            self.thread.join(timeout=25)
            if self.thread.is_alive():
                raise TimeoutError("ゲーム操作の終了を待っています。")
        if self.scientist.thread.ident is not None:
            self.scientist.close()
        self.store.close()
        logging.getLogger("selfstruct").removeHandler(self.log_handler)
        self.log_handler.close()
        self.instance_lock.close()
        self.closed = True

    def watchdog(self):
        while not self.stopped.wait(.01):
            try:
                if self.game:
                    if hasattr(self.game, "release_due"):
                        self.game.release_due()
                    if time.monotonic() - self.last_progress > 5:
                        self.game.release()
                    if time.monotonic() - self.last_progress > 30 and not self.paused.is_set():
                        self.pause()
                        self.notify("観測が応答しないため入力を解放しました。接続状態を確認してください。")
            except Exception:
                log.exception("Watchdog release failed")

    def run(self):
        try:
            if self.game is None:
                self.config.validate()
                self.game = WindowsGame(self.config.game_exe, self.config.game_args, self.config.window_title, self.config.keys)
            self.status = "ゲームへ接続中"
            self.game.start()
            unchanged = 0
            while not self.stopped.is_set():
                self.last_progress = time.monotonic()
                if self.paused.is_set():
                    self.stopped.wait(.1)
                    continue
                self.status = "画面と入力の関係を実験中"
                try:
                    change = self.step()
                    unchanged = unchanged + 1 if change <= .001 else 0
                    if unchanged >= self.config.restart_after:
                        self.recover_game("画面変化が長時間確認できないため、有限の復帰試行後にゲームを再起動しました。")
                        unchanged = 0
                except WindowMinimized as exc:
                    self.game.release()
                    self.status = str(exc)
                    self.stopped.wait(.25)
                except CapacityError as exc:
                    self.pause()
                    self.notify(str(exc))
                except GameUnavailable:
                    log.exception("Game connection lost")
                    self.recover_game("ゲームとの接続を再検出し、必要に応じて再起動しました。")
                except Exception as exc:
                    log.exception("Runtime failure")
                    self.game.release()
                    self.scientist.abort_trial(repr(exc))
                    self.revisions.rollback()
                    self.agent = self.revisions.load()
                    self.agent_id = self.revisions.active
                    self.notify("内部処理の異常を記録し、前の安定世代に戻しました。")
                    self.restarts += 1
                    if self.restarts > self.config.max_restarts:
                        raise RuntimeError("復旧回数の上限に達しました。診断情報を保存して停止しました。") from exc
        except Exception as exc:
            log.exception("Host stopped")
            self.error = str(exc) if isinstance(exc, (ValueError, GameUnavailable, RuntimeError, CapacityError)) else "実験装置に問題が発生しました。開発者詳細に診断情報を保存しました。"
            self.notify(self.error)
        finally:
            try:
                self.checkpoint()
            except Exception:
                log.exception("Runtime checkpoint could not be saved; raw evidence retained")
            if self.game:
                self.game.release()
                self.game.stop()
            self.status = "停止中"
            self.stopped.set()

    def recover_game(self, message: str):
        self.game.release()
        if self.restarts >= self.config.max_restarts:
            raise RuntimeError("ゲームの復旧回数の上限に達しました。実験装置の状態を確認してください。")
        self.restarts += 1
        self.scientist.abort_trial("Game restarted during comparison")
        try:
            self.game.connect()
        except GameUnavailable:
            self.game.restart()
        else:
            self.game.restart()
        self.notify(message)

    def step(self) -> float:
        started = time.perf_counter()
        if self.revisions.active != self.agent_id:
            self.agent = self.revisions.load()
            self.agent_id = self.revisions.active
            self.confirm_steps = 0
            self._restore_learning()
        before = self.game.capture()
        feature, raw = features(before)
        self.agent.pixels(raw)
        trial = self.scientist.trial_agent(self.steps)
        revision, agent = trial if trial else (self.agent_id, self.agent)
        agent.pixels(raw)
        def observe(request):
            image = before
            if "event" in request:
                image = self.store.observed_image(int(request["event"]), request.get("phase", "before"))
            elif request.get("fresh"):
                image = self.game.capture()
                self.store.observation(image, revision)
            width, height = image.size
            box = request.get("box", [0, 0, width, height])
            if (not isinstance(box, list) or len(box) != 4 or any(not isinstance(v, int) for v in box) or
                    not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)):
                raise ValueError("Invalid observation region")
            pixels = image.crop(tuple(box)).convert("L").resize((32, 32))
            return {"width": width, "height": height, "gray": list(pixels.tobytes()), "observed": time.time()}
        agent.requests(self.files, self.store.reserve, observe, self.dispatch)
        if "research_due" in agent.exports:
            self.scientist.automatic = bool(agent.call("research_due", self.steps))
        index, duration, wait, pred = agent.action(feature, len(self.config.keys))
        before_hash = self.store.image(before)
        eid = self.store.begin(self.session, before_hash, self.config.keys[index], duration,
                               self.game.generation, revision, feature, pred)
        try:
            self.game.act(self.config.keys[index], duration, self.cancel_input, lambda timestamp: self.store.sent(eid, timestamp))
        except BaseException:
            self.store.interrupt(eid)
            raise
        if self.stopped.is_set() or self.paused.is_set():
            self.store.interrupt(eid)
            return 0
        self.cancel_input.wait(wait)
        after_image = self.game.capture()
        after, after_raw = features(after_image)
        after_hash = self.store.image(after_image)
        change = difference(before, after_image)
        self.store.finish(eid, after_hash, change, time.perf_counter() - started)
        self.agent.pixels(after_raw, 1024)
        if not self.scientist.live:
            self.agent.call("learn", feature, index, after, int(change > .01))
        self.scientist.trial_result({"id": eid, "action": self.config.keys[index], "feature": feature, "change": change}, raw, after_raw, after)
        self.latest_before, self.latest = before, after_image
        self.steps += 1
        self.last_eid = eid
        self.confirm_steps += 1
        if self.revisions.state["trial"] and self.confirm_steps >= 64:
            self.revisions.confirm()
        if self.steps % 64 == 0:
            self.checkpoint()
        self.last_progress = time.monotonic()
        self.scientist.tick()
        return change

    def dispatch(self, request: dict) -> dict:
        op = request["op"]
        if op == "job":
            job = self.store.get_job(request["id"])
            data = json.loads(job["payload"])
            return {"state": job["state"], "response": str(data.get("response", ""))[:6000]}
        if len(self.store.jobs()) >= 8:
            raise ValueError("Pending job budget exceeded")
        if op == "research":
            prompt = request["prompt"]
            if not isinstance(prompt, str) or len(prompt) > 6000:
                raise ValueError("Invalid research request")
            prefix = "未知の画面と入力に関するゲーム非依存の研究です。意味はunknownとして扱い、外部の攻略知識を補完しないでください。\n"
            jid = self.store.job("agent_llm", {"prompt": prefix + prompt})
        elif op == "candidate":
            path = self.files.path(request["path"])
            if path.stat().st_size > 256 * 1024:
                raise ValueError("Candidate too large")
            jid = self.store.job("candidate", {"source": path.read_text(encoding="utf-8"), "after": self.last_eid})
        else:
            raise ValueError("Capability not granted")
        self.scientist.tick()
        return {"job": jid}
