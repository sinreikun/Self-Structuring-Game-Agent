"""Offline integration exercise. Never presented as real-game validation."""
from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import time

from PIL import Image, ImageDraw

from .config import Config
from .engine import Controller


class SyntheticGame:
    def __init__(self):
        self.generation = 0
        self.position = 0
        self.held = False
        self.connected = False
        self.lock = threading.Lock()

    def start(self):
        self.generation += 1
        self.connected = True

    def connect(self):
        if not self.connected:
            from .windows import GameUnavailable
            raise GameUnavailable("synthetic disconnect")

    def capture(self):
        with self.lock:
            image = Image.new("RGB", (128, 96), (20 + self.position * 9 % 200, 30, 50))
            draw = ImageDraw.Draw(image)
            draw.rectangle((self.position % 100, 15, self.position % 100 + 20, 35), fill="white")
            return image

    def act(self, key, duration, cancel, on_sent=None):
        try:
            self.held = True
            if not cancel.is_set():
                if on_sent:
                    on_sent(time.time())
                with self.lock:
                    self.position += {"A": 3, "B": 7}[key]
        finally:
            self.release()

    def release(self):
        self.held = False

    def stop(self):
        self.release()
        self.connected = False

    def restart(self):
        self.stop()
        self.start()


def smoke():
    with tempfile.TemporaryDirectory(prefix="selfstruct-smoke-") as folder:
        config = Config(root=folder, keys=["A", "B"], quota_bytes=64 * 1024**2, research_every=10000)
        game = SyntheticGame()
        ctrl = Controller(config, game)
        ctrl.start()
        deadline = time.monotonic() + 10
        while ctrl.steps < 12 and time.monotonic() < deadline:
            time.sleep(.02)
        ctrl.pause()
        assert ctrl.steps >= 12, ctrl.error
        assert not game.held
        rows = ctrl.store.rows()
        assert len(rows) >= 12
        assert all((ctrl.experiment / "evidence" / (row["before_hash"] + ".png")).exists() for row in rows)
        ctrl.store.export(Path(folder) / "export.zip")
        from .research import evaluate
        source = ctrl.revisions.source()
        result = evaluate(source.replace('(i32.const -1)', '(i32.const 950)'), source,
                          rows, ctrl.store.root / "evidence", config.keys)
        assert result['candidate']['n'] >= 12
        assert result['candidate']['loss'] < result['baseline']['loss']
        sid, revision, experiment = ctrl.session, ctrl.revisions.active, ctrl.experiment
        ctrl.close()
        config.resume = True
        resumed = Controller(config, SyntheticGame())
        assert resumed.session == sid and resumed.revisions.active == revision and resumed.experiment == experiment
        assert len(resumed.store.rows()) >= 12
        resumed.close()
        print("PASS: synthetic capture/input, raw evidence, pause/release, export, isolated worker, persistent resume")


if __name__ == "__main__":
    smoke()
