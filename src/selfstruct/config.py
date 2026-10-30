from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def write_json(path: Path, obj: object) -> None:
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2).encode())


def default_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SelfStruct"


@dataclass
class Config:
    root: str = field(default_factory=lambda: str(default_root()))
    game_exe: str = ""
    game_args: list[str] = field(default_factory=list)
    window_title: str = ""
    keys: list[str] = field(default_factory=lambda: ["UP", "DOWN", "LEFT", "RIGHT", "ENTER", "SPACE"])
    quota_bytes: int = 10 * 1024**3
    llm_url: str = "http://127.0.0.1:8766/v1"
    llm_model: str = "local"
    mode: str = "Newborn"
    channel: str = "Stable"
    resume: bool = False
    auto_run: bool = False
    research_every: int = 100
    restart_after: int = 300
    max_restarts: int = 5
    catalog_url: str = ""
    catalog_key: str = ""
    model_id: str = ""

    def validate(self, require_game: bool = True) -> None:
        if self.mode not in {"Newborn", "Experienced", "Guided"}:
            raise ValueError("個体の種類が不正です。")
        if self.channel not in {"Stable", "Development"}:
            raise ValueError("更新チャンネルが不正です。")
        if self.quota_bytes < 32 * 1024**2:
            raise ValueError("保存容量は32MB以上必要です。")
        if not self.keys or len(set(self.keys)) != len(self.keys):
            raise ValueError("入力許可キーを重複なく指定してください。")
        from .windows import KEY_CODES
        if any(key not in KEY_CODES for key in self.keys):
            raise ValueError("許可できないキーが含まれています。")
        if require_game and not Path(self.game_exe).is_file():
            raise ValueError("対象ゲームの実行ファイルを選択してください。")
        root = Path(self.root).resolve()
        if require_game and (root == Path(self.game_exe).resolve().parent or root in Path(self.game_exe).resolve().parents):
            raise ValueError("ゲーム本体を含まない専用の保存先を選択してください。")
        from urllib.parse import urlparse
        endpoint = urlparse(self.llm_url)
        if endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("LLM接続先にはローカルのHTTPアドレスを指定してください。")

    def save(self, path: Path) -> None:
        write_json(path, asdict(self))

    @classmethod
    def load(cls, path: Path) -> Config:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**{key: val for key, val in data.items() if key in cls.__dataclass_fields__})


class RootFiles:
    """The only writable-file broker available to generated code."""

    def __init__(self, root: Path, max_bytes: int):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes

    def path(self, relative: str) -> Path:
        rel = Path(relative)
        if not relative or rel.is_absolute() or ".." in rel.parts or ":" in relative or "\\" in relative:
            raise ValueError("作業領域外の操作は許可されません。")
        target = self.root / rel
        for parent in (target, *target.parents):
            if parent == self.root:
                break
            if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
                raise ValueError("リンクは許可されません。")
        if not target.resolve().is_relative_to(self.root):
            raise ValueError("作業領域外です。")
        return target

    def write(self, relative: str, data: bytes) -> None:
        dest = self.path(relative)
        used = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        old = dest.stat().st_size if dest.exists() else 0
        if used - old + len(data) > self.max_bytes:
            raise OSError("作業領域の保存容量が不足しています。")
        atomic_write(dest, data)

    def read(self, relative: str) -> bytes:
        target = self.path(relative)
        if target.stat().st_size > 8192:
            raise ValueError("読み取りは1回8KBまでです。")
        return target.read_bytes()

    def delete(self, relative: str) -> None:
        target = self.path(relative)
        if target.is_dir():
            target.rmdir()
        else:
            target.unlink(missing_ok=True)

    def mkdir(self, relative: str) -> None:
        self.path(relative).mkdir(parents=True, exist_ok=True)

    def list(self, relative: str) -> list[str]:
        return [p.name for p in self.path(relative).iterdir()][:100]


class InstanceLock:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise ValueError("同じ保存先を別の実験が使用中です。先にそちらを停止してください。") from exc

    def close(self):
        if self.file is not None:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()
            self.file = None

    def __del__(self):
        self.close()
