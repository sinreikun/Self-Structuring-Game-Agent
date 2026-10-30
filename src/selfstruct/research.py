from __future__ import annotations

import json
import logging
import multiprocessing
import queue
import statistics
import threading
import time
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

from .sandbox import Revisions, Sandbox, improves, replay
from .store import CapacityError, Store

log = logging.getLogger(__name__)


class LocalLLM:
    def __init__(self, url: str, model: str):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("LLM must be local")
        self.url, self.model = url.rstrip("/"), model
        # A loopback inference engine must not send prompts through an external proxy.
        self.opener = build_opener(ProxyHandler({}))

    def health(self) -> None:
        with self.opener.open(self.url + "/models", timeout=3) as response:
            if not json.loads(response.read(65536)).get("data"):
                raise OSError("LLM has no loaded model")

    def complete(self, prompt: str, max_tokens: int = 4096, json_mode: bool = False) -> str:
        payload = {"model": self.model, "messages": [{"role": "user", "content": prompt}],
                   "temperature": .2, "max_tokens": max_tokens}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        body = json.dumps(payload).encode()
        req = Request(self.url + "/chat/completions", data=body, headers={"Content-Type": "application/json"})
        with self.opener.open(req, timeout=120) as response:
            data = response.read(512 * 1024 + 1)
        if len(data) > 512 * 1024:
            raise ValueError("LLM response too large")
        choice = json.loads(data)["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("推論の出力が途中で終了しました。未完成の提案は実行しません。")
        return choice["message"]["content"]


def evaluation_worker(source: str, baseline: str, rows: list[dict], folder: str, keys: list[str], output) -> None:
    try:
        # Resource bounds in addition to Wasmtime's per-invocation fuel/memory bounds.
        import os
        if os.name == "posix":
            import resource
            # Wasmtime reserves multi-GB guard regions without committing RAM.
            # Bound address reservations separately from resident memory monitored below.
            resource.setrlimit(resource.RLIMIT_AS, (32 * 1024**3, 32 * 1024**3))
            resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
        else:
            _limit_windows_worker()
        base = replay(baseline, rows, Path(folder), keys)
        candidate = replay(source, rows, Path(folder), keys)
        output.put({"ok": True, "baseline": base, "candidate": candidate, "passed": improves(base, candidate)})
    except BaseException as error:
        output.put({"ok": False, "error": f"{type(error).__name__}: {error}"})


def _limit_windows_worker() -> None:
    import ctypes as c
    from ctypes import wintypes as w
    class Basic(c.Structure):
        _fields_ = [("process_time", c.c_longlong), ("job_time", c.c_longlong), ("flags", w.DWORD),
                    ("min_working", c.c_size_t), ("max_working", c.c_size_t), ("active", w.DWORD),
                    ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
    class IO(c.Structure):
        _fields_ = [(n, c.c_ulonglong) for n in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
    class Extended(c.Structure):
        _fields_ = [("basic", Basic), ("io", IO), ("process_memory", c.c_size_t), ("job_memory", c.c_size_t),
                    ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]
    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    limits = Extended()
    limits.basic.flags = 0x100 | 0x8  # process memory and active process limit
    limits.basic.active = 1
    limits.process_memory = 2 * 1024**3
    if not handle or not kernel.SetInformationJobObject(handle, 9, c.byref(limits), c.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        raise OSError("Worker resource containment unavailable")


def evaluate(source: str, baseline: str, rows: list[dict], folder: Path, keys: list[str]) -> dict:
    ctx = multiprocessing.get_context("spawn")
    output = ctx.Queue(maxsize=1)
    worker = ctx.Process(target=evaluation_worker, args=(source, baseline, rows, str(folder), keys, output), daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                result = output.get(timeout=.1)
                break
            except queue.Empty:
                if not worker.is_alive() or time.monotonic() > deadline:
                    raise TimeoutError("Candidate evaluation exceeded its budget or terminated")
                status = Path(f"/proc/{worker.pid}/status")
                if status.exists():
                    for line in status.read_text().splitlines():
                        if line.startswith("VmRSS:") and int(line.split()[1]) > 512 * 1024:
                            raise MemoryError("Candidate evaluation exceeded resident memory budget")
        worker.join(timeout=2)
        if not result["ok"]:
            raise ValueError(result["error"])
        return result
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=3)
        output.close()


PROMPT = """あなたは未知の画面と入力だけを扱う自律研究Agentです。
ゲーム名・攻略・既存ゲームの意味を補完しないでください。分からない意味はunknownです。
次の経験は出所action_experienceです。画像変化は成功や失敗を意味しません。
仮説は事実と分け、日本語で根拠IDと反証する次の実験を述べてください。
自己改善はゲーム非依存の画像解析、画面変化予測、観測密度、探索効率を対象にします。
JSONオブジェクトだけを返してください。形式:
{{"findings":[], "problem":"", "reason":"", "wat":""}}
実際の根拠がある場合だけfindingsへtext（日本語の仮説と次の実験）、evidence（提供された経験IDの整数配列）を持つ要素を追加します。
入力候補の変化確率を検証する仮説にはaction（整数）とchange_probability（0..1）を追加できます。
改善が必要ならproblemとreasonを実際の根拠と改善内容で埋め、watに実行可能なWebAssemblyモジュール全文を入れてください。
watには説明文やプレースホルダーを絶対に入れないでください。実行可能な改善案が作れなければ空文字のままにします。
Agent ABI: memoryをexport(最低1ページ、最大256ページ)。OSやWASIなどのimportは禁止。
入力前32x32輝度画像はmemoryの0..1023、入力後は1024..2047。
choose(feature:i32,keys:i32)->i32 は0..keys-1。
duration(feature:i32)->i32 は10..500ミリ秒。
wait(feature:i32)->i32 は10..2000ミリ秒。
predict(feature:i32,action:i32)->i32 は画面差分が0.01を超える確率0..1000、未確認は-1。
learn(before:i32,action:i32,after:i32,changed:i32)->void。各呼出しの燃料上限200万。
内部の構造や役割分割、学習用メモリ、解析器は自由に作り変えられます。
再起動後に保持したい状態はmutable globalではなく線形memoryへ格納してください。Hostが定期的に保存し、復旧時には未反映の経験をlearnへ再送します。
featureは量子化された画像ハッシュで意味ラベルではありません。
actionとchange_probabilityは指定入力の後に画像が変化する確率の仮説だけに使い、意味や攻略成功を表しません。該当しない仮説ではこの2項目は省略してください。
任意のrequest()->i32をexportすると、memoryの4096からJSON要求を返せます。0は要求なし、最大8192バイト。
許可操作はop=write/read/delete/mkdir/list、相対path、writeにはtext。応答は16384に長さ4バイトlittle endian、その後JSON。
これはAgent専用作業領域だけを扱います。自分のagent.watを書いても本番世代には直接反映されず、候補評価が必要です。
op=observeでは任意のbox=[左,上,右,下]領域の32x32輝度配列grayと原画面サイズを得られます。fresh=trueで再観測、event=根拠IDとphase=before/afterで過去原画像を再解析できます。ゲームの意味ラベルは与えられません。
op=research,prompt=研究指示で非同期LLM作業を依頼し、返されたjobをop=job,id=jobで確認できます。op=candidate,path=作業領域のWATファイルで自分の改善候補を提出できます。任意のresearch_due(経験数:i32)->i32をexportし0を返すと初期の自動研究役を停止し、独自の役割分割・研究指示で内部構造を置き換えられます。
評価器は時間順Replayでlearnを実行し、未確認予測はBrier損失0.25。
失敗・過剰一般化・unknownを隠さないでください。
現在のモジュール:\n{source}\n経験:\n{rows}
"""


class Scientist:
    def __init__(self, store: Store, revisions: Revisions, client: LocalLLM, keys: list[str], every: int, notify):
        self.store, self.revisions, self.client = store, revisions, client
        self.keys, self.every, self.notify = keys, every, notify
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.live: dict | None = None
        self.last_event = 0
        self.failed_calls = 0
        self.automatic = True
        self.recover_llm = None
        self.thread = threading.Thread(target=self.run, name="research", daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        self.wake.set()
        self.thread.join(timeout=125)
        if self.thread.is_alive():
            raise TimeoutError("Research is still stopping")

    def tick(self):
        self.wake.set()

    def trial_agent(self, step: int) -> tuple[str, Sandbox] | None:
        with self.lock:
            if self.live and step % 2:
                return self.live["revision"], self.live["agent"]
        return None

    def trial_result(self, row: dict, raw: bytes, after_raw: bytes, after: int) -> None:
        with self.lock:
            live = self.live
            if not live:
                return
            action = self.keys.index(row["action"])
            outcome = int(row["change"] > .01)
            for name in ("base", "agent"):
                agent = live[name]
                agent.pixels(raw)
                pred = agent.call("predict", row["feature"], action)
                if not -1 <= pred <= 1000:
                    raise ValueError("Invalid live prediction")
                metrics = live["metrics"][name]
                metrics["samples"].append({"id": row["id"], "loss": .25 if pred == -1 else (pred / 1000 - outcome)**2,
                                            "known": pred >= 0})
                agent.pixels(after_raw, 1024)
                agent.call("learn", row["feature"], action, after, outcome)
            if len(live["metrics"]["base"]["samples"]) < 32:
                return
            for name in ("base", "agent"):
                metrics = live["metrics"][name]
                metrics.update(n=len(metrics["samples"]), loss=statistics.mean(x["loss"] for x in metrics["samples"]),
                               unknown=statistics.mean(not x["known"] for x in metrics["samples"]), seconds=0)
            adopted = improves(live["metrics"]["base"], live["metrics"]["agent"])
            summary = "画面変化の予測方法を比較し、改善を確認して採用しました。" if adopted else "実際の入力でも比較しましたが、確かな改善を確認できず候補を棄却しました。"
            bm, cm = live["metrics"]["base"], live["metrics"]["agent"]
            summary += f" 比較した経験は{cm['n']}件。予測誤差は{bm['loss']:.3f}から{cm['loss']:.3f}、未確認率は{bm['unknown']:.0%}から{cm['unknown']:.0%}でした。"
            if adopted:
                self.revisions.promote(live["revision"])
            self.store.report(live["revision"], adopted, summary, {"replay": live["replay"], "live": live["metrics"]})
            self.store.set_job(live["job"], "complete", {"revision": live["revision"], "adopted": adopted})
            self.live = None
            self.notify(summary)

    def abort_trial(self, reason: str) -> None:
        with self.lock:
            if self.live:
                self.store.report(self.live["revision"], False, "候補の動作異常を検出し、安定版の探索へ戻りました。", {"error": reason})
                self.store.set_job(self.live["job"], "failed", {"error": reason})
                self.live = None

    def run(self):
        try:
            self._run()
        except CapacityError:
            self.notify("研究用の保存容量が不足しました。根拠を保持し、新しい研究処理を停止しました。")
        except Exception:
            log.exception("Research supervisor stopped")
            self.notify("研究装置の異常を記録しました。診断情報を確認してください。")

    def _run(self):
        while not self.stop_event.is_set():
            self.wake.wait(timeout=5)
            self.wake.clear()
            if self.stop_event.is_set() or self.live or self.revisions.state["trial"]:
                continue
            rows = self.store.rows(256)
            self.store.validate_claims()
            pending = self.store.jobs()
            if not pending and (not self.automatic or len(rows) < max(48, self.every) or rows[-1]["id"] - self.last_event < self.every):
                continue
            if rows:
                self.last_event = rows[-1]["id"]
            jid = pending[0]["id"] if pending else self.store.job("research", {"last_event": self.last_event})
            self.store.set_job(jid, "running")
            try:
                payload = json.loads(pending[0]["payload"]) if pending else {}
                if pending and pending[0]["kind"] == "agent_llm":
                    response = self.client.complete(payload["prompt"], max_tokens=2048)
                    self.store.set_job(jid, "complete", {"prompt": payload["prompt"], "response": response})
                elif payload.get("source"):
                    self.evaluate_proposal(payload["source"], payload["after"], jid)
                else:
                    self.study(rows, jid)
                self.failed_calls = 0
            except (ValueError, TimeoutError, MemoryError) as error:
                log.exception("Candidate rejected")
                self.store.report("candidate", False, "候補の実行・出力・資源制限を検証し、不適合のため棄却しました。", {"job": jid, "error": str(error)[:4000]})
                self.store.set_job(jid, "failed")
            except CapacityError:
                raise
            except Exception:
                log.exception("Research failed")
                self.failed_calls += 1
                self.store.set_job(jid, "pending")
                if self.recover_llm and 2 <= self.failed_calls <= 4:
                    try:
                        self.recover_llm()
                        self.notify("管理中のローカル推論を再起動しました。研究を再開します。")
                    except Exception:
                        log.exception("Managed inference recovery failed")
                self.notify("研究用の推論を再試行します。画面と入力の経験収集は継続しています。" if self.failed_calls < 5 else
                            "ローカル推論が繰り返し失敗しています。モデルの稼働状態を確認してください。経験収集は継続中です。")

    def study(self, rows: list[dict], jid: str):
        self.notify("過去の経験を比較し、未確認の規則と観測方法を研究しています。")
        # Deterministic summaries are observations, not labels for game objects.
        for key in self.keys:
            selected = [r for r in rows if r["action"] == key]
            if len(selected) >= 8:
                index = self.keys.index(key) + 1
                self.store.knowledge("observation", f"入力候補{index}の後、画面変化を{len(selected)}回中{sum(r['change'] > .01 for r in selected)}回観測しました。意味と因果関係は未確認です。",
                                     "observed", "unknown", [r["id"] for r in selected[-32:]])
        summaries = [{k: r[k] for k in ("id", "feature", "change", "duration", "cost")} |
                     {"action": self.keys.index(r["action"])} for r in rows[-64:]]
        prompt = "過去の自己改善評価（コードの不備は修正し、同じ失敗を繰り返さないこと）:\n" + json.dumps(self.store.reports()[:2], ensure_ascii=False)[:6000] + "\n"
        prompt += PROMPT.format(source=self.revisions.source(), rows=json.dumps(summaries))
        response = self.client.complete(prompt, json_mode=True)
        self.store.proposal(jid, response)
        if response.strip().startswith("```"):
            response = response.strip().split("\n", 1)[1].rsplit("```", 1)[0]
        proposal = json.loads(response)
        allowed = {r["id"] for r in rows[-64:]}
        for finding in proposal.get("findings", [])[:8]:
            evidence = list(dict.fromkeys(finding["evidence"]))
            if evidence and set(evidence) <= allowed:
                kid = self.store.knowledge("hypothesis", finding["text"], "inferred", "hypothesis", evidence)
                action, probability = finding.get("action"), finding.get("change_probability")
                if isinstance(action, int) and 0 <= action < len(self.keys) and isinstance(probability, (int, float)) and 0 <= probability <= 1:
                    self.store.claim(kid, self.keys[action], probability, rows[-1]["id"])
        source = proposal.get("wat", "")
        if not source:
            self.store.set_job(jid, "complete")
            return
        self.evaluate_proposal(source, rows[-1]["id"], jid)

    def evaluate_proposal(self, source: str, after_id: int, jid: str):
        # Persist the proposal before execution; resume it rather than regenerating it.
        self.store.set_job(jid, "running", {"source": source, "after": after_id})
        self.notify("改善候補を作成しました。新しい経験を集めてから独立した比較評価を行います。")
        deadline = time.monotonic() + 300
        future = []
        while not self.stop_event.wait(.5) and time.monotonic() < deadline:
            future = [r for r in self.store.rows(128) if r["id"] > after_id]
            if len(future) >= 48:
                break
        if len(future) < 48:
            self.store.set_job(jid, "pending")
            return
        self.store.reserve(len(source.encode()) + 1024**2)
        result = evaluate(source, self.revisions.source(), future, self.store.root / "evidence", self.keys)
        historical = self.store.regression_rows(after_id)
        if historical:
            regression = evaluate(source, self.revisions.source(), historical, self.store.root / "evidence", self.keys)
            base, candidate = regression["baseline"], regression["candidate"]
            broken = any(a["known"] and a["loss"] < .04 and b["loss"] > .16
                         for a,b in zip(base["samples"], candidate["samples"]))
            result["regression"] = regression
            result["passed"] = result["passed"] and not broken and candidate["loss"] <= base["loss"] + .02
        if not result["passed"]:
            self.store.report("candidate", False, "新しい経験で予測精度と過去成功例を検証し、改善不足の候補を棄却しました。", result)
            self.store.set_job(jid, "complete")
            return
        revision = self.revisions.create(source)
        with self.lock:
            self.live = {"revision": revision, "agent": Sandbox(source), "base": self.revisions.load(), "job": jid,
                         "replay": result, "metrics": {"base": {"samples": []}, "agent": {"samples": []}}}
        self.notify("保存経験で改善を確認しました。実際の入力で短い交互比較を行っています。")
