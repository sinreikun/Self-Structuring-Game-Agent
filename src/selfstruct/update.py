from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .config import atomic_write, write_json
from .resources import Gateway, fetch_json, verify_catalog


class Updater:
    def __init__(self, root: Path, quota: int, public_key: str, manifest_url: str):
        self.root, self.quota, self.public_key, self.manifest_url = root, quota, public_key, manifest_url

    def prepare(self, channel: str, version: str) -> dict | None:
        if not self.public_key or not self.manifest_url:
            raise ValueError("公式更新の配布設定がありません。署名付き配布版が必要です。")
        envelope = fetch_json(self.manifest_url)
        manifest = verify_catalog(envelope, self.public_key)
        if manifest["channel"] != channel:
            raise ValueError("更新チャンネルが異なります。")
        def version_tuple(value):
            return tuple(int(part) for part in value.split("."))
        if version_tuple(manifest["version"]) <= version_tuple(version):
            return None
        item = manifest["application"]
        artifact = Gateway(self.root, self.quota).download(item, "update")
        folder = self.root / "updates"
        folder.mkdir(exist_ok=True)
        write_json(folder / "manifest.json", envelope)
        return {"artifact": str(artifact), "sha256": item["sha256"], "version": manifest["version"]}

    def handoff(self, prepared: dict) -> None:
        if not getattr(sys, "frozen", False) or os.name != "nt":
            raise ValueError("本体の自動更新はWindows配布版で利用できます。")
        original = Path(sys.executable).resolve()
        helper = self.root / "updates" / "update-helper.exe"
        shutil.copy2(original, helper)
        ticket = self.root / "updates" / "ticket.json"
        write_json(ticket, {**prepared, "original": str(original), "parent": os.getpid(),
                            "public_key": self.public_key, "envelope": json.loads((self.root / "updates" / "manifest.json").read_text(encoding="utf-8"))})
        subprocess.Popen([str(helper), "--apply-update", str(ticket)], close_fds=True)


def apply_update(ticket: Path) -> bool:
    state = json.loads(ticket.read_text(encoding="utf-8"))
    # Trust root comes from the signed application, never the mutable ticket.
    from .release import UPDATE_KEY
    manifest = verify_catalog(state["envelope"], UPDATE_KEY)
    source, original = Path(state["artifact"]), Path(state["original"])
    if not getattr(sys, "frozen", False) or original.suffix.lower() != ".exe" or Gateway.digest(original) != Gateway.digest(Path(sys.executable)):
        raise ValueError("Update target does not match this application")
    if Gateway.digest(source) != manifest["application"]["sha256"]:
        raise ValueError("Update integrity failure")
    import ctypes as c
    from ctypes import wintypes as w
    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, state["parent"])
    if handle:
        try:
            if kernel.WaitForSingleObject(handle, 60000) != 0:
                raise TimeoutError("Application did not exit")
        finally:
            kernel.CloseHandle(handle)
    backup = original.with_suffix(".previous.exe")
    staging = original.with_suffix(".pending.exe")
    shutil.copy2(source, staging)
    if Gateway.digest(staging) != manifest["application"]["sha256"]:
        raise ValueError("Staged update integrity failure")
    shutil.copy2(original, backup)
    os.replace(staging, original)
    marker = ticket.parent / "healthy"
    marker.unlink(missing_ok=True)
    try:
        # Marker is written only after GUI/DB/Wasmtime initialization succeeds.
        child = subprocess.Popen([str(original), "--health-marker", str(marker)])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if marker.exists() and child.poll() is None:
                ticket.unlink(missing_ok=True)
                return True
            if child.poll() is not None:
                break
            time.sleep(.25)
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
    except Exception:
        pass
    os.replace(backup, original)
    atomic_write(ticket.parent / "rollback.txt", "新しい版の起動に失敗したため、前の安定版へ戻しました。".encode())
    subprocess.Popen([str(original)])
    return False
