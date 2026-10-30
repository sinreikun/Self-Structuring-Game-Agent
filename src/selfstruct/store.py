from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import threading
import time
import tempfile
import uuid
import zipfile
from pathlib import Path

from PIL import Image

from .config import atomic_write


class CapacityError(OSError):
    pass


PROVENANCE = {"observed", "action_experience", "inferred", "self_generated_model", "external", "human_provided"}
VALIDATION = {"unknown", "hypothesis", "supported", "reproduced", "disputed", "refuted"}


class Store:
    def __init__(self, root: Path, quota: int):
        self.root = root
        self.account_root = root
        self.quota = quota
        self.lock = threading.RLock()
        (root / "host").mkdir(parents=True, exist_ok=True)
        (root / "evidence").mkdir(exist_ok=True)
        self.db = sqlite3.connect(root / "host" / "experience.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          PRAGMA synchronous=FULL;
          PRAGMA foreign_keys=ON;
          CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, mode TEXT, started REAL, ended REAL);
          CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY, image_hash TEXT, captured REAL, revision TEXT);
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, session TEXT REFERENCES sessions(id),
            before_hash TEXT, after_hash TEXT, action TEXT, duration REAL, sent REAL, observed REAL,
            process_generation INTEGER, revision TEXT, analyzer TEXT, state TEXT,
            feature INTEGER, change REAL, cost REAL, prediction REAL);
          CREATE TABLE IF NOT EXISTS knowledge(id INTEGER PRIMARY KEY, kind TEXT, text TEXT,
            provenance TEXT, validation TEXT, evidence TEXT, created REAL);
          CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, kind TEXT, state TEXT, payload TEXT, updated REAL);
          CREATE TABLE IF NOT EXISTS proposals(id INTEGER PRIMARY KEY, job TEXT, response TEXT, created REAL);
          CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY, created REAL, revision TEXT, adopted INTEGER,
            summary TEXT, metrics TEXT);
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
          CREATE TABLE IF NOT EXISTS claims(knowledge INTEGER PRIMARY KEY REFERENCES knowledge(id),
            action TEXT, probability REAL, after_id INTEGER, tested_until INTEGER);
          CREATE INDEX IF NOT EXISTS event_session ON events(session,id);
        """)
        with self.db:
            self.db.execute("UPDATE events SET state='interrupted' WHERE state IN ('pending','sent')")
            self.db.execute("UPDATE jobs SET state='pending' WHERE state='running'")

    def usage(self) -> int:
        return sum(p.stat().st_size for p in self.account_root.rglob("*") if p.is_file())

    def reserve(self, amount: int) -> None:
        if self.usage() + amount + 1024**2 > self.quota:
            raise CapacityError("保存容量の上限に達しました。根拠画像を保持して一時停止しました。")

    def image(self, image: Image.Image) -> str:
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        data = buf.getvalue()
        digest = hashlib.sha256(data).hexdigest()
        with self.lock:
            path = self.root / "evidence" / (digest + ".png")
            if not path.exists():
                self.reserve(len(data))
                atomic_write(path, data)
        return digest

    def session(self, mode: str, resume: bool) -> str:
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM sessions WHERE ended IS NULL ORDER BY started DESC LIMIT 1").fetchone()
            if resume and row and row["mode"] == mode:
                return row["id"]
            self.db.execute("UPDATE sessions SET ended=? WHERE ended IS NULL", (time.time(),))
            sid = uuid.uuid4().hex
            self.db.execute("INSERT INTO sessions VALUES (?,?,?,NULL)", (sid, mode, time.time()))
            return sid

    def begin(self, session: str, before: str, action: str, duration: float, generation: int,
              revision: str, feature: int, prediction: float) -> int:
        with self.lock, self.db:
            self.reserve(4096)
            cur = self.db.execute("""INSERT INTO events(session,before_hash,action,duration,sent,
              process_generation,revision,analyzer,state,feature,prediction)
              VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (session, before, action, duration, None, generation,
                                                   revision, revision, "pending", feature, prediction))
            return cur.lastrowid

    def sent(self, event: int, timestamp: float):
        with self.lock, self.db:
            self.db.execute("UPDATE events SET sent=?,state='sent' WHERE id=?", (timestamp, event))

    def interrupt(self, event: int):
        with self.lock, self.db:
            self.db.execute("UPDATE events SET state='interrupted' WHERE id=? AND state<>'complete'", (event,))

    def observation(self, image: Image.Image, revision: str) -> str:
        captured = time.time()
        digest = self.image(image)
        with self.lock, self.db:
            self.db.execute("INSERT INTO observations(image_hash,captured,revision) VALUES(?,?,?)", (digest, captured, revision))
        return digest

    def observed_image(self, event: int, phase: str) -> Image.Image:
        if phase not in {'before', 'after'}:
            raise ValueError("Invalid observation phase")
        with self.lock:
            row = self.db.execute("SELECT before_hash,after_hash FROM events WHERE id=?", (event,)).fetchone()
        if row is None:
            raise ValueError("Unknown evidence")
        digest = row[0 if phase == 'before' else 1]
        if not digest:
            raise ValueError("Missing evidence")
        with Image.open(self.root / "evidence" / (digest + ".png")) as image:
            return image.copy()

    def finish(self, event: int, after: str, change: float, cost: float) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE events SET after_hash=?,observed=?,change=?,cost=?,state='complete' WHERE id=?",
                            (after, time.time(), change, cost, event))

    def rows(self, limit: int = 512) -> list[dict]:
        with self.lock:
            return [dict(r) for r in reversed(self.db.execute(
                "SELECT * FROM events WHERE state='complete' ORDER BY id DESC LIMIT ?", (limit,)).fetchall())]

    def regression_rows(self, before: int) -> list[dict]:
        with self.lock:
            recent = self.db.execute("SELECT * FROM events WHERE state='complete' AND id<=? ORDER BY id DESC LIMIT 128", (before,)).fetchall()
            successful = self.db.execute("""SELECT * FROM events WHERE state='complete' AND id<=? AND prediction>=0
              AND (prediction-CASE WHEN change>.01 THEN 1 ELSE 0 END)*(prediction-CASE WHEN change>.01 THEN 1 ELSE 0 END)<.04
              ORDER BY id DESC LIMIT 128""", (before,)).fetchall()
            return sorted({row['id']:dict(row) for row in [*recent,*successful]}.values(),key=lambda row:row['id'])

    def knowledge(self, kind: str, text: str, provenance: str, validation: str, evidence: list[int]) -> int:
        if provenance not in PROVENANCE or validation not in VALIDATION:
            raise ValueError("Invalid provenance or validation")
        if not text or len(text) > 4000 or not evidence:
            raise ValueError("根拠を伴う短い知識が必要です。")
        with self.lock, self.db:
            found = self.db.execute(f"SELECT count(*) FROM events WHERE state='complete' AND id IN ({','.join('?' for _ in evidence)})", evidence).fetchone()[0]
            if found != len(set(evidence)):
                raise ValueError("Unknown evidence")
            self.reserve(8192)
            return self.db.execute("INSERT INTO knowledge(kind,text,provenance,validation,evidence,created) VALUES(?,?,?,?,?,?)",
                                   (kind, text, provenance, validation, json.dumps(evidence), time.time())).lastrowid

    def knowledge_rows(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM knowledge ORDER BY id DESC")]

    def claim(self, knowledge: int, action: str, probability: float, after_id: int):
        if not 0 <= probability <= 1:
            raise ValueError("Invalid predicted probability")
        with self.lock, self.db:
            self.db.execute("INSERT INTO claims VALUES(?,?,?,?,?)", (knowledge, action, probability, after_id, after_id))

    def validate_claims(self):
        import math
        with self.lock, self.db:
            for claim in self.db.execute("SELECT * FROM claims").fetchall():
                rows = self.db.execute("SELECT id,change FROM events WHERE id>? AND action=? AND state='complete' ORDER BY id LIMIT 128",
                                       (claim["after_id"], claim["action"])).fetchall()
                if len(rows) < 16 or rows[-1]["id"] <= claim["tested_until"]:
                    continue
                n = len(rows)
                p = sum(r["change"] > .01 for r in rows) / n
                # Wilson interval for independently observed binary image changes.
                z = 1.96
                center = (p + z*z / (2*n)) / (1 + z*z/n)
                radius = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / (1 + z*z/n)
                predicted = claim["probability"]
                status = "refuted" if predicted < center-radius or predicted > center+radius else ("reproduced" if n >= 32 else "supported")
                old = self.db.execute("SELECT evidence FROM knowledge WHERE id=?", (claim["knowledge"],)).fetchone()
                evidence = list(dict.fromkeys(json.loads(old[0]) + [r["id"] for r in rows]))
                self.db.execute("UPDATE knowledge SET validation=?,evidence=? WHERE id=?", (status, json.dumps(evidence), claim["knowledge"]))
                self.db.execute("UPDATE claims SET tested_until=? WHERE knowledge=?", (rows[-1]["id"], claim["knowledge"]))

    def job(self, kind: str, payload: dict) -> str:
        jid = uuid.uuid4().hex
        with self.lock, self.db:
            serialized = json.dumps(payload)
            self.reserve(8192 + len(serialized.encode()))
            self.db.execute("INSERT INTO jobs VALUES (?,?,?,?,?)", (jid, kind, "pending", serialized, time.time()))
        return jid

    def proposal(self, jid: str, response: str):
        with self.lock, self.db:
            self.reserve(len(response.encode()) + 8192)
            self.db.execute("INSERT INTO proposals(job,response,created) VALUES(?,?,?)", (jid,response,time.time()))

    def set_job(self, jid: str, state: str, payload: dict | None = None) -> None:
        with self.lock, self.db:
            if payload is None:
                self.db.execute("UPDATE jobs SET state=?,updated=? WHERE id=?", (state, time.time(), jid))
            else:
                self.reserve(8192 + len(json.dumps(payload).encode()))
                self.db.execute("UPDATE jobs SET state=?,payload=?,updated=? WHERE id=?", (state, json.dumps(payload), time.time(), jid))

    def jobs(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM jobs WHERE state='pending' ORDER BY updated")]

    def get_job(self, jid: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            if row is None:
                raise ValueError("Unknown job")
            return dict(row)

    def report(self, revision: str, adopted: bool, summary: str, metrics: dict) -> None:
        with self.lock, self.db:
            self.reserve(8192 + len(json.dumps(metrics).encode()))
            self.db.execute("INSERT INTO reports(created,revision,adopted,summary,metrics) VALUES(?,?,?,?,?)",
                            (time.time(), revision, int(adopted), summary, json.dumps(metrics)))

    def reports(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 100")]

    def book(self) -> str:
        labels = {"unknown": "未確認", "hypothesis": "仮説", "supported": "支持あり", "reproduced": "再現済み", "disputed": "矛盾あり", "refuted": "反証済み"}
        lines = ["# 研究ノート", "", "画面と入力の観測から作成しました。仮説は確定した事実ではありません。", ""]
        for row in self.knowledge_rows():
            lines += [f"## {labels[row['validation']]}", row["text"], f"根拠経験数: {len(json.loads(row['evidence']))}", ""]
        if not self.knowledge_rows():
            lines.append("まだ発見はありません。画面の意味は未確認です。")
        lines += ["", "## 自己改善"]
        lines.extend(r["summary"] for r in self.reports())
        return "\n".join(lines)

    def export(self, destination: Path) -> None:
        """Consistent DB backup; logs/settings never include authentication secrets."""
        with self.lock:
            descriptor, temp_name = tempfile.mkstemp(prefix="selfstruct-backup-", suffix=".sqlite3", dir=destination.parent)
            import os
            os.close(descriptor)
            temp = Path(temp_name)
            target = sqlite3.connect(temp)
            try:
                self.db.backup(target)
            finally:
                target.close()
            try:
                with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as out:
                    out.write(temp, "diagnostics.sqlite3")
                    out.writestr("研究ノート.md", self.book())
                    for folder in ("host", "agents"):
                        for path in (self.root / folder).rglob("*"):
                            if path.is_file() and path.suffix in {".json", ".log", ".wat"}:
                                out.write(path, path.relative_to(self.root))
                    if self.account_root != self.root:
                        for path in (self.account_root / "host").glob("*"):
                            if path.is_file() and path.suffix in {".json", ".log"}:
                                out.write(path, "environment/host/" + path.name)
                    for row in self.rows(32):
                        for key in ("before_hash", "after_hash"):
                            path = self.root / "evidence" / (row[key] + ".png")
                            if path.exists() and path.relative_to(self.root).as_posix() not in out.namelist():
                                out.write(path, path.relative_to(self.root))
            finally:
                temp.unlink(missing_ok=True)

    def close(self) -> None:
        with self.lock:
            self.db.close()
