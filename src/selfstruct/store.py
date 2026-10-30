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
        self._usage_bytes = 0
        self._usage_at = 0.
        self._tracked_db_bytes = 0.
        self._gc_at = 0.
        self.storage_state = "通常"
        self.gc_thresholds = (.80, .90, .95, .98)
        self.emergency_fraction = .05
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
          CREATE TABLE IF NOT EXISTS evidence_pins(event INTEGER, reason TEXT, PRIMARY KEY(event,reason));
          CREATE TABLE IF NOT EXISTS gc_history(id INTEGER PRIMARY KEY, created REAL, stage TEXT, freed INTEGER, events INTEGER);
          CREATE TABLE IF NOT EXISTS image_inventory(hash TEXT PRIMARY KEY, bytes INTEGER, created REAL);
          CREATE TABLE IF NOT EXISTS compacted(signature TEXT PRIMARY KEY, count INTEGER, first REAL, last REAL);
          CREATE INDEX IF NOT EXISTS event_state ON events(state,id);
          CREATE INDEX IF NOT EXISTS knowledge_kind ON knowledge(kind);
          CREATE INDEX IF NOT EXISTS knowledge_status ON knowledge(validation,kind);
          CREATE INDEX IF NOT EXISTS event_session ON events(session,id);
        """)
        with self.db:
            self.db.execute("UPDATE events SET state='interrupted' WHERE state IN ('pending','sent')")
            self.db.execute("UPDATE jobs SET state='pending' WHERE state='running'")
            for report in self.db.execute('SELECT id,metrics FROM reports').fetchall():
                self.pin(self.metric_evidence(json.loads(report['metrics'])), 'report-'+str(report['id']))

    @staticmethod
    def metric_evidence(value):
        ids=set()
        if isinstance(value,dict):
            if type(value.get('id'))==int:ids.add(value['id'])
            for child in value.values():ids.update(Store.metric_evidence(child))
        elif isinstance(value,list):
            for child in value:ids.update(Store.metric_evidence(child))
        return ids

    def usage(self, force: bool = False) -> int:
        with self.lock:
            database=self.root/'host'/'experience.sqlite3'
            db_bytes=0
            for suffix in ('','-wal','-shm'):
                try:db_bytes+=Path(str(database)+suffix).stat().st_size
                except FileNotFoundError:pass
            self._usage_bytes+=db_bytes-self._tracked_db_bytes
            self._tracked_db_bytes=db_bytes
            if force or time.monotonic() - self._usage_at > 5:
                total = 0
                for path in self.account_root.rglob("*"):
                    try:
                        if path.is_file() and not path.is_symlink(): total += path.stat().st_size
                    except FileNotFoundError: pass
                self._usage_bytes = total
                self._usage_at = time.monotonic()
            return self._usage_bytes

    def storage_snapshot(self) -> dict:
        with self.lock:
            return {"used": self.usage(), "quota": self.quota, "stage": self.storage_state,
                    "reserved": int(self.quota*self.emergency_fraction)}

    def storage_breakdown(self) -> dict:
        result = {}
        for folder in self.account_root.iterdir():
            try:
                result[folder.name] = sum(p.stat().st_size for p in folder.rglob("*") if p.is_file() and not p.is_symlink()) if folder.is_dir() else folder.stat().st_size
            except FileNotFoundError: pass
        return result

    def pin(self, events, reason: str):
        with self.lock, self.db:
            self.db.executemany("INSERT OR IGNORE INTO evidence_pins VALUES (?,?)", ((int(e),reason) for e in events))

    def unpin(self, reason: str):
        with self.lock, self.db:
            self.db.execute("DELETE FROM evidence_pins WHERE reason=?", (reason,))

    def protect_recent(self, reason: str, limit: int = 16):
        self.pin([r["id"] for r in self.rows(limit)], reason)

    def collect(self, force: bool = False) -> int:
        """Only redundant, unreferenced primary images are collectible. No fuzzy deletion.

        Retain current replay window, live actions, explicit pins, every knowledge
        reference, first transition representatives and bounded unusual outcomes.
        Metadata is compacted into condition/outcome counts, never relabelled.
        """
        with self.lock:
            used = self.usage(force=force)
            ratio = used/max(1,self.quota)
            self.storage_state = ("重要根拠専用" if ratio>=self.gc_thresholds[3] else
                "低価値画像を抑制" if ratio>=self.gc_thresholds[2] else
                "積極的に自動整理" if ratio>=self.gc_thresholds[1] else
                "自動整理中" if ratio>=self.gc_thresholds[0] else "通常")
            if not force and (ratio < self.gc_thresholds[0] or time.monotonic()-self._gc_at < 2): return 0
            self._gc_at = time.monotonic()
            protected = {r[0] for r in self.db.execute("SELECT event FROM evidence_pins")}
            for row in self.db.execute("SELECT evidence FROM knowledge"):
                protected.update(json.loads(row[0]))
            protected.update(r[0] for r in self.db.execute("SELECT id FROM events WHERE state IN ('pending','sent')"))
            protected.update(r[0] for r in self.db.execute("SELECT id FROM events WHERE state='complete' ORDER BY id DESC LIMIT 128"))
            # First representatives preserve temporal and action/result relationships.
            protected.update(r[0] for r in self.db.execute("""SELECT min(id) FROM events WHERE state='complete'
                GROUP BY (feature & 255),action,CAST(change*10 AS INTEGER),CASE WHEN prediction<0 THEN -1 ELSE CAST(prediction*4 AS INTEGER) END LIMIT 256"""))
            protected.update(r[0] for r in self.db.execute("""SELECT id FROM events WHERE state='complete' AND
                prediction>=0 AND abs(prediction-CASE WHEN change>.01 THEN 1 ELSE 0 END)>.7 ORDER BY id DESC LIMIT 128"""))
            stale = [r for r in self.db.execute("SELECT id,feature,action,change,sent FROM events WHERE state='complete'") if r['id'] not in protected]
            with self.db:
                for r in self.db.execute("SELECT id,feature,action,change,sent FROM events WHERE state='summarized' AND id<(SELECT coalesce(max(id),0)-128 FROM events)").fetchall():
                    signature=json.dumps([r['feature'] & 255,r['action'],int((r['change'] or 0)*10)])
                    self.db.execute("INSERT INTO compacted VALUES (?,1,?,?) ON CONFLICT(signature) DO UPDATE SET count=count+1,last=excluded.last",(signature,r['sent'],r['sent']))
                    self.db.execute("UPDATE events SET state='compacted',before_hash=NULL,after_hash=NULL WHERE id=?",(r['id'],))
                for r in stale:
                    signature=json.dumps([r['feature'] & 255,r['action'],int(r['change']*10)])
                    self.db.execute("""INSERT INTO compacted VALUES (?,1,?,?) ON CONFLICT(signature) DO UPDATE
                        SET count=count+1,last=excluded.last""", (signature,r['sent'],r['sent']))
                    self.db.execute("UPDATE events SET state='compacted',before_hash=NULL,after_hash=NULL WHERE id=?",(r['id'],))
                # Old unreferenced compacted rows have been represented by statistics.
                self.db.execute("DELETE FROM events WHERE state='compacted' AND id<(SELECT coalesce(max(id),0)-10000 FROM events)")
                # Fresh observation requests have a bounded hot window, linked events/pins remain below.
                self.db.execute("DELETE FROM observations WHERE id<(SELECT coalesce(max(id),0)-128 FROM observations)")
                refs={r[0] for r in self.db.execute("SELECT before_hash FROM events WHERE before_hash IS NOT NULL UNION SELECT after_hash FROM events WHERE after_hash IS NOT NULL UNION SELECT image_hash FROM observations")}
                freed=0
                # Expired partial downloads are reproducible; active writes refresh mtime.
                for partial in (self.account_root/'assets').rglob('*.part'):
                    try:
                        if not partial.is_symlink() and time.time()-partial.stat().st_mtime>7*86400:
                            size=partial.stat().st_size;partial.unlink();freed+=size
                    except FileNotFoundError:pass
                for path in (self.root/'evidence').glob('*.png'):
                    try:
                        if path.stem not in refs and time.time()-path.stat().st_mtime>60:
                            size=path.stat().st_size;path.unlink();freed+=size
                            self.db.execute("DELETE FROM image_inventory WHERE hash=?",(path.stem,))
                    except FileNotFoundError: pass
                self.db.execute("INSERT INTO gc_history(created,stage,freed,events) VALUES(?,?,?,?)",(time.time(),self.storage_state,freed,len(stale)))
            if not self.db.in_transaction:
                self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            self._usage_at=0
            self.usage(force=True)
            return freed

    def reserve(self, amount: int, critical: bool = True) -> None:
        with self.lock:
            limit = self.quota if critical else int(self.quota*(1-self.emergency_fraction))
            used=self.usage()
            if used+amount+65536 > limit or used/self.quota>=self.gc_thresholds[0]:
                self.collect()
                used=self.usage()
            if used+amount+65536 > limit:
                raise CapacityError("重要な研究根拠の保存枠も不足しています。診断情報を保持して一時停止しました。")

    def image(self, image: Image.Image, critical: bool = False) -> str:
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        data = buf.getvalue()
        digest = hashlib.sha256(data).hexdigest()
        with self.lock:
            path = self.root / "evidence" / (digest + ".png")
            if not path.exists():
                try:
                    self.reserve(len(data), critical=critical)
                except CapacityError:
                    if critical:raise
                    self.reserve(0,critical=True)
                    return ""  # A summarized action is still counted; never invent an image.
                atomic_write(path, data)
                self._usage_bytes += len(data)
                with self.db:
                    self.db.execute("INSERT OR IGNORE INTO image_inventory VALUES(?,?,?)",(digest,len(data),time.time()))
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
            self.db.execute("UPDATE events SET after_hash=?,observed=?,change=?,cost=?,state=CASE WHEN before_hash<>'' AND ?<>'' THEN 'complete' ELSE 'summarized' END WHERE id=?",
                            (after, time.time(), change, cost, after, event))

    def rows(self, limit: int = 512, pin_reason: str | None = None) -> list[dict]:
        with self.lock:
            rows=[dict(r) for r in reversed(self.db.execute(
                "SELECT * FROM events WHERE state='complete' ORDER BY id DESC LIMIT ?", (limit,)).fetchall())]
            if pin_reason:self.pin([r['id'] for r in rows],pin_reason)
            return rows

    def regression_rows(self, before: int, pin_reason: str | None = None) -> list[dict]:
        with self.lock:
            recent = self.db.execute("SELECT * FROM events WHERE state='complete' AND id<=? ORDER BY id DESC LIMIT 128", (before,)).fetchall()
            successful = self.db.execute("""SELECT * FROM events WHERE state='complete' AND id<=? AND prediction>=0
              AND (prediction-CASE WHEN change>.01 THEN 1 ELSE 0 END)*(prediction-CASE WHEN change>.01 THEN 1 ELSE 0 END)<.04
              ORDER BY id DESC LIMIT 128""", (before,)).fetchall()
            rows=sorted({row['id']:dict(row) for row in [*recent,*successful]}.values(),key=lambda row:row['id'])
            if pin_reason:self.pin([r['id'] for r in rows],pin_reason)
            return rows

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

    def revision_number(self, revision: str) -> int:
        key='revision-number:'+revision
        with self.lock,self.db:
            old=self.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
            if old:return int(old[0])
            number=1+self.db.execute("SELECT coalesce(max(CAST(value AS INTEGER)),0) FROM meta WHERE key LIKE 'revision-number:%'").fetchone()[0]
            self.db.execute('INSERT INTO meta VALUES (?,?)',(key,str(number)))
            return number

    def counts(self) -> dict:
        with self.lock:
            result={"experience":self.db.execute("SELECT count(*) FROM events").fetchone()[0],
                    "improvements":self.db.execute("SELECT count(*) FROM reports WHERE adopted=1").fetchone()[0]}
            for name,condition in {"discoveries":"kind='observation'", "hypotheses":"kind='hypothesis'",
                "supported":"validation IN ('supported','reproduced')", "refuted":"validation='refuted'", "skills":"kind='skill'"}.items():
                result[name]=self.db.execute("SELECT count(*) FROM knowledge WHERE "+condition).fetchone()[0]
            return result

    def note_rows(self, category: str, limit: int = 80) -> list[dict]:
        clauses={"発見":"kind='observation'", "仮説":"kind='hypothesis'", "支持":"validation IN ('supported','reproduced')", "反証":"validation='refuted'"}
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM knowledge WHERE "+clauses.get(category,'1')+" ORDER BY id DESC LIMIT ?",(limit,))]

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
            self.pin(self.metric_evidence(metrics),"evaluation-"+revision)
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
        from .diagnostics import create_diagnostics
        create_diagnostics(self.account_root, store=self, destination=destination)

    def close(self) -> None:
        with self.lock:
            self.db.close()
