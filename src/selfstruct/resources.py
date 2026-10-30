"""Host-only acquisition gateway; generated agents never receive this interface."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import tarfile
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .config import atomic_write, write_json


TRUSTED_HOSTS = {"api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
                 "huggingface.co", "cdn-lfs.huggingface.co", "cdn-lfs-us-1.hf.co", "cdn-lfs.hf.co",
                 "cas-bridge.xethub.hf.co", "transfer.xethub.hf.co", "us.aws.cdn.hf.co"}


def check_url(url: str, hosts: set[str]) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise ValueError("信頼された配布元ではありません。")


class SafeRedirect(HTTPRedirectHandler):
    def __init__(self, hosts: set[str]):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl, self.hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_json(url: str, hosts: set[str] = TRUSTED_HOSTS) -> dict:
    check_url(url, hosts)
    with build_opener(SafeRedirect(hosts)).open(Request(url, headers={"User-Agent": "SelfStruct/0.1"}), timeout=30) as reply:
        raw = reply.read(2 * 1024**2 + 1)
    if len(raw) > 2 * 1024**2:
        raise ValueError("配布情報が大きすぎます。")
    return json.loads(raw)


def verify_catalog(envelope: dict, public_key: str) -> dict:
    payload = base64.b64decode(envelope["payload"], validate=True)
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)).verify(
        base64.b64decode(envelope["signature"], validate=True), payload)
    catalog = json.loads(payload)
    if catalog.get("schema") != 1 or catalog["expires"] < time.time():
        raise ValueError("配布情報が期限切れか未対応です。")
    return catalog


class Gateway:
    def __init__(self, root: Path, quota: int, hosts: set[str] | None = None):
        self.root, self.quota = root, quota
        self.hosts = hosts or TRUSTED_HOSTS
        self.lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    def download(self, item: dict, kind: str, progress=lambda done, total: None,
                 cancel: threading.Event | None = None) -> Path:
        if kind not in {"engine", "model", "library", "update"}:
            raise ValueError("Invalid asset category")
        if item.get("trust") not in {"Trusted", "Verified Candidate"} or not item.get("license"):
            raise ValueError("取得元・ライセンスが未確認のため実行できません。")
        size, digest = int(item["size"]), item["sha256"]
        if size <= 0 or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("完全性情報がありません。")
        check_url(item["url"], self.hosts)
        with self.lock:
            folder = self.root / "assets" / kind / digest
            folder.mkdir(parents=True, exist_ok=True)
            dest, partial = folder / "artifact", folder / "artifact.part"
            if dest.exists() and dest.stat().st_size == size and self.digest(dest) == digest:
                if not (folder / "receipt.json").exists():
                    write_json(folder / "receipt.json", {**item, "kind": kind, "acquired": time.time()})
                return dest
            offset = partial.stat().st_size if partial.exists() else 0
            if offset > size:
                partial.unlink()
                offset = 0
            used = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
            if used + (size - offset) + 4 * 1024**2 > self.quota or shutil.disk_usage(folder).free < size - offset + 4 * 1024**2:
                raise OSError("取得に必要な保存容量がありません。")
            headers = {"User-Agent": "SelfStruct/0.1", "Accept-Encoding": "identity"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            opener = build_opener(SafeRedirect(self.hosts))
            with opener.open(Request(item["url"], headers=headers), timeout=60) as reply:
                if reply.status != 206:
                    offset = 0
                elif not reply.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                    raise ValueError("ダウンロード再開位置を検証できません。")
                with partial.open("ab" if offset else "wb") as out:
                    done = offset
                    while True:
                        if cancel and cancel.is_set():
                            raise InterruptedError("取得を中断しました。次回再開できます。")
                        data = reply.read(min(1024**2, size - done + 1))
                        if not data:
                            break
                        done += len(data)
                        if done > size:
                            raise ValueError("配布ファイルが宣言容量を超えています。")
                        out.write(data)
                        progress(done, size)
                    out.flush()
                    os.fsync(out.fileno())
            if partial.stat().st_size != size or self.digest(partial) != digest:
                partial.unlink(missing_ok=True)
                raise ValueError("配布ファイルの完全性を確認できません。取得物は使用しません。")
            os.replace(partial, dest)
            write_json(folder / "receipt.json", {**item, "kind": kind, "acquired": time.time()})
            return dest

    @staticmethod
    def digest(path: Path) -> str:
        with path.open("rb") as file:
            return hashlib.file_digest(file, "sha256").hexdigest()

    def extract_engine(self, artifact: Path) -> Path:
        dest = artifact.parent / "expanded"
        inventory = artifact.parent / "expanded-files.json"
        expected_name = "llama-server.exe" if os.name == "nt" else "llama-server"
        if dest.exists() and inventory.exists():
            files = json.loads(inventory.read_text())
            if all((dest / name).resolve().is_relative_to(dest.resolve()) and
                   self.digest(dest / name) == digest for name, digest in files.items()):
                servers = list(dest.rglob(expected_name))
                if len(servers) == 1:
                    return servers[0]
        if dest.exists():
            shutil.rmtree(dest)
        zipped = zipfile.is_zipfile(artifact)
        with (zipfile.ZipFile(artifact) if zipped else tarfile.open(artifact)) as archive:
            infos = archive.infolist() if zipped else archive.getmembers()
            total = sum(info.file_size if zipped else info.size for info in infos)
            used = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
            if total > 2 * 1024**3 or used + total > self.quota:
                raise ValueError("展開容量が上限を超えています。")
            for info in infos:
                path = Path((info.filename if zipped else info.name).replace("\\", "/"))
                link = (info.external_attr >> 16) & 0o170000 == 0o120000 if zipped else not (info.isfile() or info.isdir() or info.issym() or info.islnk())
                if path.is_absolute() or ".." in path.parts or ":" in str(path) or link:
                    raise ValueError("展開先が不正です。")
            if zipped:
                archive.extractall(dest)
            else:
                archive.extractall(dest, members=[i for i in infos if i.isfile() or i.isdir()], filter="data")
                # Official Linux builds use library symlinks. Materialize only
                # internal regular-file targets, leaving no runtime link escape.
                members = {i.name: i for i in infos}
                for info in infos:
                    if not (info.issym() or info.islnk()):
                        continue
                    current = info
                    for _ in range(16):
                        target = (Path(current.name).parent / current.linkname) if current.issym() else Path(current.linkname)
                        target_path = (dest / target).resolve()
                        if not target_path.is_relative_to(dest.resolve()):
                            raise ValueError("展開先が不正です。")
                        normalized = str(target_path.relative_to(dest.resolve()))
                        current = members.get(normalized)
                        if current is None:
                            raise ValueError("共有ライブラリの参照先がありません。")
                        if current.isfile():
                            if sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file()) + target_path.stat().st_size > self.quota:
                                raise ValueError("展開容量が上限を超えています。")
                            shutil.copy2(target_path, dest / info.name)
                            break
                    else:
                        raise ValueError("共有ライブラリの参照が循環しています。")
        servers = list(dest.rglob(expected_name))
        if len(servers) != 1:
            raise ValueError("推論エンジンが含まれていません。")
        write_json(inventory, {str(p.relative_to(dest)): self.digest(p) for p in dest.rglob("*") if p.is_file()})
        return servers[0]


class Catalog:
    """Official TLS metadata supplies immutable refs and expected SHA-256s.

    A release publisher can replace the model list with a signed catalog.
    No bytes execute before their expected digest is checked.
    """
    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            self.data = json.loads(path.read_text())
        else:
            self.data = json.loads(Path(__file__).with_name("assets").joinpath("catalog.json").read_text())

    def refresh(self, url: str, key: str) -> None:
        if not url or not key:
            raise ValueError("署名付き配布情報の設定がありません。")
        data = verify_catalog(fetch_json(url), key)
        if data.get("generation", 0) < self.data.get("generation", 0):
            raise ValueError("古い配布情報への巻き戻しを拒否しました。")
        write_json(self.path, data)
        self.data = data

    def resolve_model(self, selected: dict) -> dict:
        repo, filename = selected["repo"], selected["file"]
        if not repo.startswith("Qwen/") or ".." in repo:
            raise ValueError("モデルの配布者が未確認です。")
        data = fetch_json(f"https://huggingface.co/api/models/{repo}?blobs=true")
        entry = next(x for x in data["siblings"] if x["rfilename"] == filename)
        return {"name": selected["name"], "version": data["sha"], "license": "Apache-2.0",
                "trust": "Trusted", "size": entry["lfs"]["size"], "sha256": entry["lfs"]["sha256"],
                "url": f"https://huggingface.co/{repo}/resolve/{data['sha']}/{filename}"}

    @staticmethod
    def resolve_engine(target: str | None = None) -> dict:
        suffix = "bin-win-cpu-x64.zip" if (target or os.name) == "nt" else "bin-ubuntu-x64.tar.gz"
        releases = fetch_json("https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10")
        match = next(((release, asset) for release in releases if not release.get("draft")
                      for asset in release["assets"] if asset["name"].endswith(suffix)), None)
        if match is None:
            raise ValueError("対応する推論エンジンの公式配布物が見つかりません。")
        data, asset = match
        digest = asset.get("digest") or ""
        if not digest.startswith("sha256:"):
            raise ValueError("推論エンジンの公式SHA-256がありません。検証なしでは導入しません。")
        return {"name": "llama.cpp CPU", "version": data["tag_name"], "license": "MIT", "trust": "Trusted",
                "size": asset["size"], "sha256": digest.removeprefix("sha256:"), "url": asset["browser_download_url"]}


class ManagedLLM:
    def __init__(self, root: Path, quota: int):
        self.root, self.quota = root, quota
        self.process: subprocess.Popen | None = None
        self.state_path = root / "host" / "llm.json"
        self.log = None
        self.reader = None

    def install(self, selected: dict, progress, cancel: threading.Event) -> None:
        gateway = Gateway(self.root, self.quota)
        engine_item = Catalog.resolve_engine()
        engine = gateway.extract_engine(gateway.download(engine_item, "engine", progress, cancel))
        model_item = Catalog(self.root / "host" / "catalog.json").resolve_model(selected)
        model = gateway.download(model_item, "model", progress, cancel)
        old = json.loads(self.state_path.read_text()) if self.state_path.exists() else None
        self.stop()
        state = {"engine": str(engine), "model": str(model), "engine_sha256": Gateway.digest(engine),
                 "model_sha256": model_item["sha256"], "previous": old}
        try:
            self.start(state, cancel=cancel)
            from .research import LocalLLM
            LocalLLM("http://127.0.0.1:8766/v1", "local").complete("日本語で短く挨拶してください。", max_tokens=32)
            write_json(self.state_path, state)
        except Exception:
            self.stop()
            if old:
                self.start(old)
            raise

    def start(self, state: dict | None = None, cancel: threading.Event | None = None) -> bool:
        if self.process and self.process.poll() is None:
            return True
        if self.process or self.log:
            self.stop()
        if state is None:
            if not self.state_path.exists():
                return False
            state = json.loads(self.state_path.read_text())
        engine, model = Path(state["engine"]), Path(state["model"])
        for path, digest in ((engine, state["engine_sha256"]), (model, state["model_sha256"])):
            if not path.resolve().is_relative_to((self.root / "assets").resolve()) or Gateway.digest(path) != digest:
                raise ValueError("推論環境の完全性を確認できません。")
        manifests = [parent / "expanded-files.json" for parent in engine.parents if (parent / "expanded-files.json").is_file()]
        if not manifests:
            raise ValueError("推論エンジンの構成ファイルを確認できません。")
        manifest = manifests[0]
        expanded = (manifest.parent / "expanded").resolve()
        for name, digest in json.loads(manifest.read_text()).items():
            path = (expanded / name).resolve()
            if not path.is_relative_to(expanded) or Gateway.digest(path) != digest:
                raise ValueError("推論エンジンの共有ライブラリの完全性を確認できません。")
        log_path = self.root / "host" / "llm.log"
        # Bound logs across restarts.
        if log_path.exists() and log_path.stat().st_size > 4 * 1024**2:
            log_path.unlink()
        self.log = RotatingFileHandler(log_path, maxBytes=1024**2, backupCount=2, encoding="utf-8")
        self.process = subprocess.Popen([str(engine), "-m", str(model), "--host", "127.0.0.1", "--port", "8766",
                                         "--alias", "local", "-c", "8192", "--parallel", "1"],
                                        cwd=str(engine.parent), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        stream, handler = self.process.stdout, self.log
        def drain():
            try:
                while data := stream.readline(8192):
                    handler.emit(logging.LogRecord("llm", logging.INFO, "", 0, data.decode(errors="replace").rstrip(), (), None))
            finally:
                stream.close()
        self.reader = threading.Thread(target=drain, name="llm-log", daemon=True)
        self.reader.start()
        from .research import LocalLLM
        client = LocalLLM("http://127.0.0.1:8766/v1", "local")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if cancel and cancel.is_set():
                self.stop()
                raise InterruptedError("推論エンジンの起動を中断しました。")
            if self.process.poll() is not None:
                self.stop()
                raise OSError("推論エンジンの起動に失敗しました。")
            try:
                client.health()
                return True
            except OSError:
                time.sleep(.5)
        self.stop()
        raise TimeoutError("推論エンジンの起動確認が時間切れになりました。")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log:
            if self.reader:
                self.reader.join(timeout=2)
            self.log.close()
            self.log = None
