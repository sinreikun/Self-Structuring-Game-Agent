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

from .activity import Activity
from .config import Config, InstanceLock, RootFiles, atomic_write, write_json
from .research import LocalLLM, Scientist
from .sandbox import Revisions, difference, features
from .store import CapacityError, Store
from .windows import WindowMinimized, GameUnavailable, WindowsGame

log = logging.getLogger(__name__)


class Controller:
    def record_sent(self,eid,timestamp,key,duration):
        self.store.sent(eid,timestamp)
        self.activity.record('ACT',f'{key} hold={duration*1000:.0f}ms')
        self.activity.emit('action',key=key)

    def __init__(self, config: Config, game=None):
        self.config = config
        self.root = Path(config.root).resolve()
        (self.root / "host").mkdir(parents=True, exist_ok=True)
        self.instance_lock = InstanceLock(self.root / "host" / "experiment.lock")
        self.closed = False
        self.messages: queue.Queue[str] = queue.Queue(maxsize=100)
        self.telemetry = {}
        self.latest: Image.Image | None = None
        self.latest_before: Image.Image | None = None
        self.status = "停止中"
        self.activity = Activity()
        self.started = time.monotonic()
        self.steps = 0
        self.last_progress = time.monotonic()
        self.stopped = threading.Event()
        self.paused = threading.Event()
        self.cancel_input = threading.Event()
        self.thread: threading.Thread | None = None
        self.monitor_thread = None
        self.game = game
        self.restarts = 0
        self.recovering=threading.Event()
        self.recovery_delay=2.
        self.error = ""
        self.protect_until = 0.
        try:
            self.experiment = self._experiment()
        except Exception:
            self.instance_lock.close()
            raise
        self.store = Store(self.experiment, config.quota_bytes)
        self.store.account_root = self.root
        self.revisions = Revisions(self.experiment / "agents" / "revisions")
        self.agent = self.revisions.load()
        self.agent_id = self.revisions.active
        self.generation_number=self.store.revision_number(self.agent_id)
        self.last_eid = 0
        self.files = RootFiles(self.experiment / "agents" / "workspace", min(config.quota_bytes // 10, 256 * 1024**2))
        self.confirm_steps = 0
        self.session = self.store.session(config.mode, config.resume)
        self._restore_learning()
        self.scientist = Scientist(self.store, self.revisions, LocalLLM(config.llm_url, config.llm_model),
                                   config.keys, config.research_every, self.notify, self.activity)
        from .vision import Eyes, VisualAccess
        self.visual_lock = threading.Lock()
        self.visual_frame = None
        self.visual_version = 0
        self.visual_condition = threading.Condition(self.visual_lock)
        self.visual = VisualAccess(Eyes(self.store, self.activity, self.current_visual_frame, self.fresh_visual_frame))
        self.scientist.visual = self.visual
        self.visual.eyes.cancel = self.scientist.stop_event
        self.scientist.client.context = config.llm_context
        handler = RotatingFileHandler(self.experiment / "host" / "host.log", maxBytes=2 * 1024**2, backupCount=2, encoding="utf-8")
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
        from .vision import ReplayEyes, ReplayFiles
        rows = self.store.rows(512)
        eyes = ReplayEyes(self.store.root/"evidence", rows)
        files = ReplayFiles()
        for row in rows:
            self.last_eid = row["id"]
            if row["id"] <= restored:
                eyes.revealed.add(row["id"])
                continue
            if row["action"] not in self.config.keys:
                continue
            with Image.open(self.store.root / "evidence" / (row["before_hash"] + ".png")) as image:
                before, raw = features(image)
                eyes.frame = image.copy()
                eyes.current_id = row["id"]
            with Image.open(self.store.root / "evidence" / (row["after_hash"] + ".png")) as image:
                after, after_raw = features(image)
            self.agent.pixels(raw)
            self.agent.requests(files, files.deny, lambda request: eyes.query(request)[0], eyes.dispatch)
            self.agent.pixels(after_raw, 1024)
            self.agent.call("learn", before, self.config.keys.index(row["action"]), after, int(row["change"] > .01))
            eyes.revealed.add(row["id"])

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
        from .individuals import Individuals
        repository=Individuals(self.root)
        repository.migrate(self.config,locked=True)
        identifier=self.config.agent_id or (repository.active() if self.config.resume or self.config.mode=='Experienced' else '')
        if identifier:
            repository.save_settings(identifier,self.config,locked=True)
        else:
            if self.config.mode=='Experienced': raise ValueError('引き継ぐ個体がありません。NEWBORNで作成してください。')
            identifier=repository.create(self.config,parent=repository.active() or None,locked=True)
        from dataclasses import replace
        self.config=replace(self.config,agent_id=identifier)
        return repository.path(identifier)

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
        self.monitor_thread=threading.Thread(target=self.monitor, name="telemetry", daemon=True)
        self.monitor_thread.start()
        self.thread = threading.Thread(target=self.run, name="player", daemon=True)
        self.thread.start()
        threading.Thread(target=self.watchdog, name="watchdog", daemon=True).start()

    def pause(self):
        self.paused.set()
        self.cancel_input.set()
        if self.game:
            self.game.release()
        self.status = "一時停止中"
        self.activity.update(phase=self.status)

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
        if self.monitor_thread:self.monitor_thread.join(timeout=15)
        from dataclasses import asdict
        try:
            self.store.reserve(8192)
            write_json(self.store.root/'host'/'activity.json',asdict(self.activity.snapshot()))
        except OSError:log.exception('Final activity snapshot could not be saved')
        self.checkpoint()
        from .individuals import Individuals
        Individuals(self.root).finish(self.config.agent_id,self.config,time.monotonic()-self.started,locked=True)
        self.store.close()
        logging.getLogger("selfstruct").removeHandler(self.log_handler)
        self.log_handler.close()
        self.instance_lock.close()
        self.closed = True

    def monitor(self):
        while not self.stopped.is_set():
            try:
                old_stage=self.store.storage_state
                self.store.collect()
                if self.store.storage_state!=old_stage and self.store.storage_state!='通常':self.activity.emit('gc')
                with self.store.lock:
                    report=self.store.db.execute('SELECT metrics FROM reports ORDER BY id DESC LIMIT 1').fetchone()
                self.telemetry={'counts':self.store.counts(),'storage':self.store.storage_snapshot(),'evaluation':json.loads(report[0]) if report else {}}
            except Exception: log.exception('Telemetry failed')
            if self.stopped.wait(2):break

    def watchdog(self):
        while not self.stopped.wait(.01):
            try:
                if self.game:
                    if hasattr(self.game, "release_due"):
                        self.game.release_due()
                    if time.monotonic() - self.last_progress > 5:
                        self.game.release()
                    if time.monotonic() - self.last_progress > 30 and not self.paused.is_set() and not self.recovering.is_set() and self.activity.snapshot().connection=="接続済み":
                        self.pause()
                        self.notify("観測が応答しないため入力を解放しました。接続状態を確認してください。")
            except Exception:
                log.exception("Watchdog release failed")

    def run(self):
        try:
            if self.game is None:
                self.config.validate()
                self.game = WindowsGame(self.config.game_exe, self.config.game_args, self.config.window_title, self.config.keys,
                                        mode=self.config.connection_mode,target_pid=self.config.target_pid,
                                        target_created=self.config.target_created,target_exe=self.config.target_exe,
                                        target_hwnd=self.config.target_hwnd,on_wait=lambda:setattr(self,'last_progress',time.monotonic()),
                                        cancel=self.stopped)
            self.status = "ゲームへ接続中"
            self.activity.update(phase=self.status, connection="接続中",revision=self.agent_id)
            try:self.game.start()
            except (GameUnavailable,OSError):self.recover_game('ゲームを起動し直して再接続しました。')
            if self.stopped.is_set():return
            if isinstance(self.game,WindowsGame):
                state_path=self.root/'host'/'experiment.json'
                state=json.loads(state_path.read_text(encoding='utf-8'))
                if state.get('target_exe') and state['target_exe'].casefold()!=self.game.target_exe.casefold():
                    raise ValueError('前回と異なるゲーム本体が見つかりました。NEWBORNで開始してください。')
                state['target_exe']=self.game.target_exe;write_json(state_path,state)
            self.activity.update(connection="接続済み")
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
                        self.recover_game("画面変化が長時間確認できないため、有限の復帰試行を行いました。",restart=True)
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
                    self.store.protect_recent("runtime-error")
                    self.protect_until = time.time()+30
                    self.activity.emit("error")
                    self.game.release()
                    self.scientist.abort_trial(repr(exc))
                    self.revisions.rollback()
                    self.agent = self.revisions.load()
                    self.agent_id = self.revisions.active
                    self.generation_number=self.store.revision_number(self.agent_id)
                    self.notify("内部処理の異常を記録し、前の安定世代に戻しました。")
                    self.restarts += 1
                    if self.restarts > self.config.max_restarts:
                        raise RuntimeError("復旧回数の上限に達しました。診断情報を保存して停止しました。") from exc
        except Exception as exc:
            log.exception("Host stopped")
            self.store.protect_recent("host-error")
            self.activity.emit("error")
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
            self.activity.update(phase=self.status,connection="切断")
            self.stopped.set()

    def recover_game(self, message: str, restart: bool=False):
        self.recovering.set()
        self.activity.update(recovery='接続を復旧中',phase='復旧中',connection='再接続待ち')
        self.activity.emit('recovery')
        self.store.protect_recent('recovery');self.protect_until=time.time()+30
        self.game.release()
        self.scientist.suspend_trial('ゲーム接続の復旧後に比較を再開します。',self.last_eid)
        # An input without a matching after-frame must never become evidence of
        # the next process generation, even when the process restarts quickly.
        with self.store.lock,self.store.db:
            self.store.db.execute("UPDATE events SET state='interrupted' WHERE session=? AND state IN ('pending','sent')",(self.session,))
        attempt=0
        try:
            while not self.stopped.is_set():
                attempt+=1;self.last_progress=time.monotonic()
                self.status=f'ゲームを復旧中（{attempt}回目）'
                self.activity.update(phase=self.status,recovery='起動・再接続を試行中')
                try:
                    if isinstance(self.game,WindowsGame) and attempt>1:self.game.restart(force=True)
                    elif restart:self.game.restart()
                    else:
                        try:self.game.connect()
                        except (GameUnavailable,OSError):self.game.restart()
                    if self.stopped.is_set():return
                    self.game.capture()  # Rebinding a HWND alone does not prove capture recovered.
                    if self.stopped.is_set():return
                    self.latest=None;self.latest_before=None
                    self.notify(message)
                    self.activity.update(recovery='',connection='接続済み',phase='観測を再開')
                    return
                except WindowMinimized as error:
                    self.status=str(error)
                    self.activity.update(phase=self.status,recovery='最小化の解除待ち')
                    restart=False;attempt=max(0,attempt-1)
                    self.stopped.wait(.5)
                except (GameUnavailable,OSError) as error:
                    log.warning('Game recovery attempt %d: %s',attempt,error)
                    self.game.release()
                    delay=min(30,self.recovery_delay*2**min(attempt-1,4))
                    self.status=f'ゲームの復旧待ち（{delay:g}秒後に再試行）'
                    self.activity.update(phase=self.status,recovery='起動元・ゲーム本体を再検出中')
                    self.stopped.wait(delay)
        finally:self.recovering.clear()

    def step(self) -> float:
        started = time.perf_counter()
        if self.revisions.active != self.agent_id:
            self.agent = self.revisions.load()
            self.agent_id = self.revisions.active
            self.generation_number=self.store.revision_number(self.agent_id)
            self.confirm_steps = 0
            self._restore_learning()
        before = self.game.capture()
        self.activity.record("OBS",f"frame={self.steps} region=full")
        feature, raw = features(before)
        self.agent.pixels(raw)
        trial = self.scientist.trial_agent(self.steps)
        revision, agent = trial if trial else (self.agent_id, self.agent)
        agent.pixels(raw)
        self.activity.update(phase="観測中",revision=revision)
        with self.visual_condition:
            self.visual_frame = before.copy()
            self.visual_version += 1
            self.visual_condition.notify_all()
        def observe(request):
            # A fresh call here is already on the sole game-I/O thread.
            frame = before
            if request.get("fresh"):
                frame = self.game.capture()
                request = {**request, "fresh": False}
            return self.visual.eyes.query(request, current=frame)[0]
        agent.requests(self.files, lambda n:self.store.reserve(n,critical=False), observe, lambda request:self.dispatch(request,revision))
        if "research_due" in agent.exports:
            self.scientist.automatic = bool(agent.call("research_due", self.steps))
        index, duration, wait, pred = agent.action(feature, len(self.config.keys))
        self.activity.update(phase="入力中", action=self.config.keys[index],duration=duration,prediction=pred,
                             next="入力後の画面を観測します")
        self.activity.record("PRED",f"p(change)={pred:.4f}")
        before_hash = self.store.image(before,critical=time.time()<self.protect_until or bool(self.scientist.live) or self.revisions.state["trial"])
        eid = self.store.begin(self.session, before_hash, self.config.keys[index], duration,
                               self.game.generation, revision, feature, pred)
        try:
            self.game.act(self.config.keys[index], duration, self.cancel_input, lambda timestamp: self.record_sent(eid,timestamp,self.config.keys[index],duration))
        except BaseException:
            self.store.interrupt(eid)
            raise
        if self.stopped.is_set() or self.paused.is_set():
            self.store.interrupt(eid)
            return 0
        self.cancel_input.wait(wait)
        after_image = self.game.capture()
        after, after_raw = features(after_image)
        after_hash = self.store.image(after_image,critical=time.time()<self.protect_until or bool(self.scientist.live) or self.revisions.state["trial"])
        change = difference(before, after_image)
        self.store.finish(eid, after_hash, change, time.perf_counter() - started)
        self.activity.record("DIFF",f"score={change:.4f}")
        self.activity.record("STORE",f"event={eid}")
        if time.time()<self.protect_until:self.store.pin([eid],"error-after")
        self.activity.update(phase="結果を確認中",change=change,last_observed=time.time(),next="次の入力と予測を選びます")
        self.activity.emit("change" if change>.01 else "unchanged")
        self.agent.pixels(after_raw, 1024)
        if not self.scientist.live:
            self.agent.call("learn", feature, index, after, int(change > .01))
            self.activity.record("LEARN",f"feature={feature}")
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

    def current_visual_frame(self):
        with self.visual_lock:
            return self.visual_frame.copy() if self.visual_frame is not None else None

    def fresh_visual_frame(self):
        # Research waits for Player's next capture, never competes for the game
        # handle or injects input. Pause/minimization produce a bounded timeout.
        with self.visual_condition:
            version = self.visual_version
            available = self.visual_condition.wait_for(
                lambda: self.visual_version > version or self.stopped.is_set(), timeout=5)
            if not available or self.stopped.is_set() or self.visual_frame is None:
                raise ValueError("Fresh frame unavailable while paused/stopped/minimized")
            return self.visual_frame.copy()

    def dispatch(self, request: dict, revision: str | None = None) -> dict:
        op = request["op"]
        from .vision import VISUAL_OPS
        if op in VISUAL_OPS:
            if op in {"analyzer_evaluate", "library_request"}:
                if len(self.store.jobs()) >= 8:
                    raise ValueError("Pending job budget exceeded")
                jid = self.store.job("visual", {"request": request})
                self.scientist.tick()
                return {"job": jid}
            return self.visual.handle(request)[0]
        if op == "describe":
            from .capabilities import validate_description
            description=validate_description(request,revision or self.agent_id)
            self.store.reserve(len(json.dumps(description).encode())+4096)
            write_json(self.revisions.root/(revision or self.agent_id)/'capabilities.json',description)
            return {"recorded":True}
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
