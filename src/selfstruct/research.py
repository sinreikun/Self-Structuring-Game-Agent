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
from .evaluation import sample, summarize, assessment, regression_safe
from .vision import Eyes, VisualAccess, VISUAL_INSTRUCTIONS, resolved_request, evidence_ids

log = logging.getLogger(__name__)


class InferenceUnavailable(RuntimeError):
    """No candidate was evaluated: the inference request itself failed."""


class IncompleteInference(ValueError):
    """A draft was returned and must be retained, never executed unfinished."""


class ResearchRepairNeeded(InferenceUnavailable):
    pass


PROPOSAL_SCHEMA={
    'type':'object','additionalProperties':False,
    'required':['findings','problem','reason','wat'],
    'properties':{
        'findings':{'type':'array','maxItems':8,'items':{'type':'object','additionalProperties':False,
            'required':['text','evidence'],'properties':{
                'text':{'type':'string','maxLength':2000},
                'evidence':{'type':'array','maxItems':64,'items':{'type':'integer'}},
                'action':{'type':'integer','minimum':0},
                'change_probability':{'type':'number','minimum':0,'maximum':1}}}},
        'visual_requests':{'type':'array','maxItems':4,'items':{'type':'object'}},
        'problem':{'type':'string'},'reason':{'type':'string'},'wat':{'type':'string'}}}


def parse_proposal(response,allowed,keys):
    text=response.strip()
    if text.startswith('```'):text=text.split('\n',1)[1].rsplit('```',1)[0]
    value=json.loads(text)
    if not isinstance(value,dict):raise ResearchRepairNeeded('研究応答はJSONオブジェクトにしてください。')
    findings=value.get('findings',[])
    if isinstance(findings,dict) and 'text' in findings:findings=[findings]
    if not isinstance(findings,list) or len(findings)>8:raise ResearchRepairNeeded('findingsは最大8件のオブジェクト配列にしてください。')
    for finding in findings:
        if not isinstance(finding,dict) or not isinstance(finding.get('text'),str) or not 1<=len(finding['text'])<=2000:raise ResearchRepairNeeded('各findingにはtext文字列とevidence整数配列が必要です。')
        evidence=finding.get('evidence')
        if not isinstance(evidence,list) or not evidence or len(evidence)>64 or any(type(e)!=int or e not in allowed for e in evidence):
            raise ResearchRepairNeeded('evidenceには今回提示した経験IDの整数だけを指定してください。')
        if 'action' in finding and (type(finding['action'])!=int or not 0<=finding['action']<len(keys)):
            raise ResearchRepairNeeded('actionは許可入力の番号で指定してください。')
        if 'change_probability' in finding and (type(finding['change_probability']) not in (int,float) or not 0<=finding['change_probability']<=1):
            raise ResearchRepairNeeded('change_probabilityは0〜1の数値にしてください。')
    if not isinstance(value.get('wat',''),str):raise ResearchRepairNeeded('watは完全なWAT文字列か空文字にしてください。')
    return {**value,'findings':findings,'wat':value.get('wat','')}


class LocalLLM:
    def __init__(self, url: str, model: str):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("LLM must be local")
        self.url, self.model = url.rstrip("/"), model
        self.managed = parsed.port==8766
        self.vision = False
        self.capabilities = {"input": "text", "status": "unverified"}
        # A loopback inference engine must not send prompts through an external proxy.
        self.opener = build_opener(ProxyHandler({}))

    def health(self) -> None:
        with self.opener.open(self.url + "/models", timeout=3) as response:
            models=json.loads(response.read(65536)).get("data")
        if not models:
            raise OSError("LLM has no loaded model")
        selected = next((m for m in models if m.get("id") == self.model), models[0])
        self.loaded_model = selected.get("id", self.model)
        # Use explicit runtime metadata, never a model-name heuristic. llama.cpp
        # reports modalities in /props; other local OpenAI servers may use /models.
        metadata = selected
        modalities = metadata.get("modalities", (metadata.get("meta") or {}).get("modalities", {}))
        if not modalities:
            try:
                with self.opener.open(self.url.removesuffix("/v1") + "/props", timeout=3) as response:
                    metadata = json.loads(response.read(65536))
                    modalities = metadata.get("modalities", {})
            except (OSError, ValueError):
                modalities = {}
        advertised = (isinstance(modalities, dict) and
                      (modalities.get("vision") is True or "image" in (modalities.get("input") or []))) or (
                      isinstance(modalities, list) and "image" in modalities)
        self.vision = bool(advertised)
        self.capabilities = {"input": "vision" if self.vision else "text", "status": "runtime_reported" if modalities else "unverified",
                             "model": self.loaded_model, "checked_at": time.time()}

    def count_tokens(self,prompt):
        req=Request(self.url.removesuffix('/v1')+'/tokenize',data=json.dumps({'content':prompt,'add_special':True}).encode(),headers={'Content-Type':'application/json'})
        with self.opener.open(req,timeout=3) as response:
            return len(json.loads(response.read(1024**2))['tokens'])

    def complete(self, prompt: str, max_tokens: int = 2048, json_mode: bool = False,
                 *, allow_partial=False, timeout=600, cancel=None, schema=None, on_progress=None, images=None) -> str:
        import http.client
        import io
        import select
        import socket
        from urllib.parse import urlparse
        cancel=cancel or getattr(self,'cancel_event',None)
        deadline=time.monotonic()+timeout
        def check():
            if cancel and cancel.is_set():raise InterruptedError('推論を中断しました。')
            if time.monotonic()>=deadline:raise TimeoutError('推論の制限時間を超えました。')
        check()
        self.last_response=''
        self.last_completion={'status':'started','max_output_tokens':max_tokens,'deadline_seconds':timeout}
        parsed=urlparse(self.url)
        connection=http.client.HTTPConnection(parsed.hostname,parsed.port or 80,timeout=3)
        raw_socket=None;response=None
        class Reader(io.RawIOBase):
            def readable(self):return True
            def readinto(self,buffer):
                while True:
                    check()
                    if select.select([raw_socket],[],[],min(.1,max(.001,deadline-time.monotonic())))[0]:
                        try:return raw_socket.recv_into(buffer)
                        except BlockingIOError:continue
        class ResponseSocket:
            def makefile(self,*args,**kwargs):return io.BufferedReader(Reader())
            # HTTP/1.0 hands ownership to the response after headers. The actual
            # socket is kept alive until the finally block, without makefile refs.
            def close(self):pass
        content = prompt
        if images:
            if not self.vision:
                raise ValueError("Native image input has not been confirmed by the runtime")
            if len(images) > 4 or any(not isinstance(value, str) or not value.startswith("data:image/png;base64,") or len(value) > 2*1024**2 for value in images):
                raise ValueError("Image input budget exceeded")
            content = [{"type": "text", "text": prompt}] + [
                {"type": "image_url", "image_url": {"url": value}} for value in images]
        payload={'model':self.model,'messages':[{'role':'user','content':content}],
                 'temperature':.2,'max_tokens':max_tokens}
        if self.managed:payload['chat_template_kwargs']={'enable_thinking':False}
        if on_progress:payload['stream']=True
        if json_mode:payload['response_format']={'type':'json_object'}
        if schema and self.managed:payload['response_format']={'type':'json_schema','json_schema':{'name':'research_proposal','schema':schema}}
        try:
            connection.connect();raw_socket=connection.sock
            check()
            connection.request('POST',parsed.path+'/chat/completions',json.dumps(payload).encode(),{'Content-Type':'application/json'})
            raw_socket.setblocking(False)
            connection.sock=ResponseSocket()
            response=connection.getresponse()
            if response.status!=200:raise OSError(f'Local inference HTTP {response.status}')
            if on_progress and 'text/event-stream' in response.getheader('Content-Type',''):
                buffer=b'';size=0;finish=None;usage={};done=False
                while True:
                    chunk=response.read1(8192);check()
                    if not chunk:break
                    size+=len(chunk)
                    if size>2*1024**2:raise ValueError('LLM stream too large')
                    buffer+=chunk
                    while b'\n' in buffer:
                        line,buffer=buffer.split(b'\n',1)
                        if not line.startswith(b'data:'):continue
                        value=line[5:].strip()
                        if value==b'[DONE]':done=True;break
                        item=json.loads(value)
                        usage=item.get('usage') or usage
                        for part in item.get('choices',[]):
                            if part.get('finish_reason'):finish=part['finish_reason']
                            delta=part.get('delta',{}).get('content') or ''
                            self.last_response+=delta
                            if delta:on_progress(self.last_response)
                    if done:break
                result={'usage':usage,'choices':[{'finish_reason':finish,'message':{'content':self.last_response}}]}
                if finish is None:raise IncompleteInference('推論ストリームが途中で切断されました。')
            else:
                data=response.read(512*1024+1);check()
                if len(data)>512*1024:raise ValueError('LLM response too large')
                result=json.loads(data)
            choice=result['choices'][0]
            self.last_completion={'finish_reason':choice.get('finish_reason'),'usage':result.get('usage',{}),'elapsed':timeout-(deadline-time.monotonic())}
            self.last_response=choice['message'].get('content') or choice['message'].get('reasoning_content','')
            if choice.get('finish_reason')=='length' and not allow_partial:
                raise IncompleteInference('推論の出力が途中で終了しました。未完成の提案は実行しません。')
            return choice['message'].get('content') or (choice['message'].get('reasoning_content','') if allow_partial else '')
        finally:
            if response:response.close()
            connection.close()
            if raw_socket:
                try:raw_socket.shutdown(socket.SHUT_RDWR)
                except OSError:pass
                raw_socket.close()


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
        passed,reason=assessment(base,candidate)
        output.put({"ok": True, "baseline": base, "candidate": candidate, "passed": passed,"reason":reason,"evaluation_schema":2,"evidence_sufficient":all(candidate["classes"][k]["n"]>=4 for k in ("changed","unchanged"))})
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
                    for line in status.read_text(encoding="utf-8").splitlines():
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
状態条件を付けない入力全体の変化確率を検証する仮説だけにaction（整数）とchange_probability（0..1）を追加できます。特定の特徴fや領域に限定する仮説ではこの2項目を省略して検証待ちにしてください。
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
op=observeはbox=[左,上,右,下]、size=[幅,高さ]またはnative、mode=L/RGBで原画像を調べます。eventとphase=before/after/diffで過去画像、未指定は現在、fresh=trueで再観測。pixelsはoffset/limitで分割取得し、grayは互換用です。ゲーム意味は与えません。
op=describe,nodes=[{{id,description,role,input,output,uses,active,effectiveness,created_generation,status}}],edges=[[id,id]]で自分が構成した能力と接続を日本語で報告できます。権限やゲーム意味は追加しません。未報告の能力をHostが推測して表示することはありません。
op=research,prompt=研究指示で非同期LLM作業を依頼し、返されたjobをop=job,id=jobで確認できます。op=candidate,path=作業領域のWATファイルで自分の改善候補を提出できます。任意のresearch_due(経験数:i32)->i32をexportし0を返すと初期の自動研究役を停止し、独自の役割分割・研究指示で内部構造を置き換えられます。
評価器は時間順Replayでlearnを実行し、未確認予測はBrier損失0.25。
失敗・過剰一般化・unknownを隠さないでください。
現在のモジュール:\n{source}\n経験:\n{rows}
"""


def research_prompt(source,rows,keys,reports):
    summaries=[{'id':r['id'],'a':keys.index(r['action']),'f':r['feature'],
                'd':round(r['change'],4)} for r in rows[-16:]]
    history=[]
    for report in reports[:2]:
        entry={'summary':report['summary'][:180]}
        metrics=report.get('metrics',{})
        if isinstance(metrics,str):
            try:metrics=json.loads(metrics)
            except ValueError:metrics={}
        if isinstance(metrics,dict):
            if metrics.get('reason'):entry['reason']=str(metrics['reason'])[:400]
            if metrics.get('error'):entry['error']=str(metrics['error'])[:300]
            if metrics.get('live'):
                metrics={**metrics,'baseline':metrics['live'].get('base',{}),'candidate':metrics['live'].get('agent',{})}
            for name in ('baseline','candidate'):
                measured=metrics.get(name,{})
                if isinstance(measured,dict) and isinstance(measured.get('loss'),(int,float)):
                    entry[name+'_loss']=measured['loss']
                    for key in ('balanced_loss','classes','references','prediction_range'):
                        if key in measured:entry[name+'_'+key]=measured[key]
        history.append(entry)
    prompt='構文修正だけでは改善ではありません。learnが空、使わない変数、現行と同一・定数の予測では、経験に応じた改善を証明できません。watなしは観測・仮説のみの完了です。改善を提案する場合は説明に対応した完全な実装を返してください。評価は変化あり・なし各4例以上で、両群を均等に扱い、定数0/0.5/1・因果的な過去頻度と比較します。ゲームの意味は与えません。\n'
    prompt+='経験の略記：a=入力番号、f=画像特徴、d=画面変化。\n'
    prompt+='過去評価：'+json.dumps(history,ensure_ascii=False,separators=(',',':'))+'\n'
    prompt+=PROMPT.format(source=source,rows=json.dumps(summaries,separators=(',',':')))
    # UTF-8 byte budget is conservative for the 8192-token managed context.
    # Never silently cut an Agent program or invent an answer to fit the budget.
    if len(prompt.encode('utf-8'))>20000:
        raise InferenceUnavailable('研究入力がコンテキスト予算を超えています。Agentの自己記述を短くしてください。')
    return prompt


class Scientist:
    def __init__(self, store: Store, revisions: Revisions, client: LocalLLM, keys: list[str], every: int, notify, activity=None):
        self.store, self.revisions, self.client = store, revisions, client
        self.keys, self.every, self.notify = keys, every, notify
        from .activity import Activity
        self.activity = activity or Activity()
        self.stop_event = threading.Event()
        if isinstance(client,LocalLLM):client.cancel_event=self.stop_event
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.live: dict | None = None
        self.last_event = 0
        self.failed_calls = 0
        self.current_job = None
        self.retry_base_delay = 5
        self.automatic = True
        self.recover_llm = None
        self.visual = VisualAccess(Eyes(store, self.activity))
        self.visual.eyes.cancel = self.stop_event
        self.thread = threading.Thread(target=self.run, name="research", daemon=True)

    def complete(self, *args, **kwargs):
        self.activity.update(llm='推論中')
        self.activity.emit('llm_start')
        try:
            result=self.client.complete(*args,**kwargs)
            self.activity.update(llm='待機')
            return result
        except IncompleteInference:
            self.activity.update(llm='待機')
            raise
        except InterruptedError:
            self.activity.update(llm='待機')
            raise
        except TimeoutError as error:
            self.activity.update(llm='エラー');self.activity.emit('llm_timeout')
            raise InferenceUnavailable('LLM timeout') from error
        except Exception as error:
            self.activity.update(llm='エラー');self.activity.emit('llm_error')
            raise InferenceUnavailable('LLM request failed') from error

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        self.wake.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise TimeoutError("Research is still stopping")
        self.activity.update(llm="待機")

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
            if live:self.store.pin([row['id']],'live-trial')
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
                metrics['samples'].append(sample(row['id'],pred,outcome,row['action']))
                agent.pixels(after_raw, 1024)
                agent.call("learn", row["feature"], action, after, outcome)
            if len(live["metrics"]["base"]["samples"]) < 32:
                return
            samples=live['metrics']['agent']['samples']
            if min(sum(s['outcome']==y for s in samples) for y in (0,1))<4 and len(samples)<128:return
            for name in ('base','agent'):
                live['metrics'][name]=summarize(live['metrics'][name]['samples'])
            adopted,reason=assessment(live["metrics"]["base"],live["metrics"]["agent"])
            summary = "画面変化の予測方法を比較し、改善を確認して採用しました。" if adopted else "実際の入力でも比較しましたが、確かな改善を確認できず候補を棄却しました。"
            bm, cm = live["metrics"]["base"], live["metrics"]["agent"]
            summary += " 判定："+reason+"。"
            summary += f" 比較した経験は{cm['n']}件。予測誤差は{bm['loss']:.3f}から{cm['loss']:.3f}、未確認率は{bm['unknown']:.0%}から{cm['unknown']:.0%}でした。"
            if adopted:
                self.revisions.promote(live["revision"])
            self.store.report(live["revision"], adopted, summary, {"replay": live["replay"], "live": live["metrics"],"reason":reason,"evaluation_schema":2})
            payload=json.loads(self.store.get_job(live['job'])['payload'])
            sufficient=all(cm['classes'][k]['n']>=4 for k in ('changed','unchanged'))
            payload.update(revision=live['revision'],adopted=adopted,implementation_verified=adopted,
                           outcome='adopted' if adopted else ('evaluated_not_adopted' if sufficient else 'insufficient_evidence'),evaluation_reason=reason)
            state='complete'
            if not adopted and sufficient and payload.get('behavior_repairs',0)<2:
                source=self.revisions.source(live['revision'])
                payload.pop('source',None)
                payload.update(behavior_repairs=payload.get('behavior_repairs',0)+1,
                               repair_response=json.dumps({'findings':[],'wat':source},ensure_ascii=False),
                               repair_error='実機比較の結果：'+reason,after=row['id'],outcome='behavior_repair')
                state='pending'
            self.store.set_job(live['job'],state,payload)
            self.store.pin([r['id'] for r in bm['samples']], 'evaluation-'+live['revision'])
            self.store.unpin('live-trial')
            self.live = None
            self.activity.update(llm='待機',comparison='完了',candidate='')
            self.activity.emit('adopted' if adopted else 'rejected')
            if adopted:self.activity.update(adoption=summary)
            self.notify(summary)

    def suspend_trial(self,reason,after_id):
        with self.lock:
            if self.live:
                live=self.live
                source=(self.revisions.root/live['revision']/'agent.wat').read_text(encoding='utf-8')
                payload=json.loads(self.store.get_job(live['job'])['payload'])
                self.store.set_job(live['job'],'pending',{**payload,'source':source,'after':after_id,'suspended_live':live['metrics'],'reason':reason})
                self.store.pin([r['id'] for r in live['metrics']['base']['samples']],'suspended-'+live['job'])
                self.live=None;self.store.unpin('live-trial')
                self.activity.update(comparison='保存して復旧待ち',candidate='')

    def abort_trial(self, reason: str) -> None:
        with self.lock:
            if self.live:
                self.store.report(self.live["revision"], False, "候補の動作異常を検出し、安定版の探索へ戻りました。", {"error": reason,"live":self.live["metrics"],"replay":self.live["replay"]})
                payload=json.loads(self.store.get_job(self.live['job'])['payload'])
                source=(self.revisions.root/self.live['revision']/'agent.wat').read_text(encoding='utf-8')
                payload.pop('source',None)
                payload.update(repair_response=json.dumps({'findings':[],'wat':source}),repair_error=reason,retained_live=self.live['metrics'])
                self.store.set_job(self.live['job'],'pending',payload)
                self.live = None
                self.store.unpin('live-trial')
                self.activity.update(comparison='保存して修正待ち',candidate='')

    def run(self):
        while not self.stop_event.is_set():
            try:
                if self.current_job:
                    if self.store.get_job(self.current_job)['state']=='running':self.store.set_job(self.current_job,'pending')
                    self.current_job=None
                self._run()
                return
            except CapacityError:
                self.activity.update(llm='待機',comparison='保存領域の回復待ち')
                self.notify('研究内容を保持し、保存領域の回復を待って再試行します。')
            except Exception:
                log.exception('Research supervisor will retry without discarding jobs')
                self.activity.update(llm='復旧中')
                self.notify('研究処理の異常を記録しました。保存済みの作業を保持して再開します。')
            self.stop_event.wait(5)

    def _run(self):
        while not self.stop_event.is_set():
            self.wake.wait(timeout=5)
            self.wake.clear()
            if self.stop_event.is_set() or self.live or self.revisions.state["trial"]:
                continue
            with self.store.lock:
                rows = self.store.rows(256)
                self.store.pin([r['id'] for r in rows],'research-current')
            self.store.validate_claims()
            pending = self.store.jobs()
            every=min(48,self.every) if self.store.storage_state!='通常' else self.every
            if not pending and (not self.automatic or len(rows) < max(48, every) or rows[-1]["id"] - self.last_event < every):
                self.store.unpin('research-current')
                continue
            if rows:
                self.last_event = rows[-1]["id"]
            jid = pending[0]["id"] if pending else self.store.job("research", {"last_event": self.last_event})
            self.current_job=jid
            self.store.set_job(jid, "running")
            try:
                payload = json.loads(pending[0]["payload"]) if pending else {}
                if pending and pending[0]["kind"] == "visual":
                    try:
                        result, _ = self.visual.handle(payload["request"], pin="research-job-"+jid)
                    except CapacityError:
                        raise
                    except (ValueError, OSError, TypeError, KeyError) as error:
                        result = {"ok": False, "error": str(error)[:500]}
                    self.store.set_job(jid, "complete", {**payload, "response": json.dumps(result, ensure_ascii=False)})
                elif pending and pending[0]["kind"] == "agent_llm":
                    self.agent_request(jid,payload)
                elif payload.get("source"):
                    self.evaluate_proposal(payload["source"], payload["after"], jid)
                else:
                    self.study(rows, jid)
                self.failed_calls = 0
            except ResearchRepairNeeded as error:
                self.store.set_job(jid,'pending')
                self.activity.update(llm='待機',comparison='保存して修正待ち')
                self.activity.emit('llm_saved')
                self.notify('研究応答を保存しました。形式・途中出力を修正して同じ研究を再試行します。')
                self.stop_event.wait(min(30,self.retry_base_delay))
            except (ValueError, TimeoutError, MemoryError) as error:
                log.warning('Candidate retained for repair: %s',error)
                self.store.report('candidate',False,'候補の不適合を記録しました。内容を保持し、修正を再試行します。',{'job':jid,'error':str(error)[:4000]})
                payload=json.loads(self.store.get_job(jid)['payload'])
                draft=payload.pop('source','')
                payload['repair_response']=json.dumps({'findings':[],'wat':draft},ensure_ascii=False)
                payload['repair_error']=str(error)[:500]
                self.store.set_job(jid,'pending',payload)
                self.activity.update(llm='待機',comparison='保存して修正待ち')
                self.stop_event.wait(min(30,self.retry_base_delay))
            except CapacityError:
                raise
            except Exception:
                if self.stop_event.is_set():
                    self.store.set_job(jid,"pending")
                    continue
                log.exception("Research failed")
                self.failed_calls += 1
                self.store.set_job(jid, "pending")
                if self.recover_llm and self.failed_calls >= 2:
                    try:
                        self.activity.update(llm='復旧中')
                        self.recover_llm()
                        self.activity.update(llm='待機')
                        self.notify("管理中のローカル推論を再起動しました。研究を再開します。")
                    except Exception:
                        log.exception("Managed inference recovery failed")
                self.notify("研究用の推論を再試行します。画面と入力の経験収集は継続しています。" if self.failed_calls < 5 else
                            "ローカル推論が繰り返し失敗しています。モデルの稼働状態を確認してください。経験収集は継続中です。")
                self.stop_event.wait(min(60,self.retry_base_delay*2**min(self.failed_calls,4)))

            finally:
                self.store.unpin('research-current')
                job_state=self.store.get_job(jid)['state']
                if job_state=='complete':self.store.unpin('research-job-'+jid)
                if job_state!='running' or self.live:self.current_job=None

    def agent_request(self,jid,payload):
        prompt=payload['prompt']
        draft=payload.get('repair_response') or payload.get('stream_draft')
        if draft:prompt+='\n前回の途中経過は保存されています。内容を引き継いで完全な応答を返してください。\n'+draft[:6000]
        budget=4096
        if isinstance(self.client,LocalLLM) and self.client.managed:
            budget=min(4096,getattr(self.client,'context',8192)-self.client.count_tokens(prompt)-256)
            if budget<512:raise InferenceUnavailable('入力が大きいため作業を保存して待機します。')
        saved=[0.]
        def journal(text):
            if time.monotonic()-saved[0]>=.5:
                payload['stream_draft']=text;self.store.set_job(jid,'running',payload);saved[0]=time.monotonic()
        try:response=self.visual_dialogue(prompt,jid,payload,budget,journal)
        except (IncompleteInference,InferenceUnavailable,InterruptedError) as error:
            draft=getattr(self.client,'last_response','') or payload.get('stream_draft','')
            if draft:
                self.store.proposal(jid,draft);payload['repair_response']=draft
            self.store.set_job(jid,'pending',payload)
            if isinstance(error,IncompleteInference):raise ResearchRepairNeeded('途中応答を保存しました。') from error
            raise
        self.store.proposal(jid,response)
        self.store.set_job(jid,'complete',{**payload,'response':response})

    def visual_dialogue(self, prompt, jid, payload, budget, journal):
        """Research stays on its worker; every extra observation is explicit/bounded.

        Store requests and bounded results, not transient crops or image base64.
        Resume native attachments from the exact pinned observation IDs.
        """
        history = payload.setdefault("visual_history", [])
        pin = "research-job-"+jid
        if isinstance(self.client, LocalLLM):
            # Refresh on each research job so changing a server's model cannot leave
            # stale vision privileges on the client or erase individual analyzers.
            try:
                self.client.health()
            except OSError:
                # Some text endpoints implement completions without model/props
                # metadata. Capability discovery cannot make text inference fail.
                self.client.vision = False
                self.client.capabilities = {"input": "text", "status": "unverified", "checked_at": time.time()}
            from .config import write_json
            write_json(self.store.root/"host"/"model-capabilities.json", self.client.capabilities)
        for _ in range(7):
            if self.stop_event.is_set():
                raise InterruptedError("研究を中断しました。")
            images = []
            # Earlier numeric payloads stay durably journaled and can be requested
            # again by exact ID. Keep current context focused on the latest turn.
            visible = []
            for i, entry in enumerate(history):
                item = json.loads(json.dumps(entry))
                if i < len(history)-4 and "result" in item:
                    for key in ("pixels", "gray", "histogram", "components"):
                        if key in item["result"]:
                            item["result"].pop(key)
                            item["payload_omitted"] = "Older array retained in job journal; request the same observation/offset to inspect again"
                item.pop("native", None)
                if entry.get("native") and i >= len(history)-4 and getattr(self.client,"vision",False):
                    try:
                        _, attachment = self.visual.handle(entry["request"], pin=pin, native=True)
                        if attachment:
                            item["attachment_index"] = len(images)
                            images.append(attachment)
                    except (ValueError, OSError):
                        item["attachment_error"] = "Source image unavailable"
                visible.append(item)
            rounds = payload.get("visual_rounds", 0)
            prefix = prompt+"\n"+VISUAL_INSTRUCTIONS+"\nモデル画像入力: "+str(bool(getattr(self.client,"vision",False)))
            def render():
                return prefix+"\n追加観測結果（attachment_indexは画像添付の0始まり番号）:"+json.dumps(visible, ensure_ascii=False, separators=(",", ":"))
            text = render()
            # Context pressure must be recoverable, never cause an endless retry
            # with the same oversized pixels. Expose exact replayable references.
            if isinstance(self.client, LocalLLM) and self.client.managed:
                available = getattr(self.client, "context", 8192)-1024-256-len(images)*1024
                for item in visible:
                    if self.client.count_tokens(text) <= available:
                        break
                    if "result" in item:
                        removed = False
                        for key in ("pixels", "gray", "histogram", "components"):
                            if key in item["result"]:
                                item["result"].pop(key); removed = True
                        if removed:
                            item["payload_omitted"] = "Array exceeds current context; request same source with pixels limit<=64, or statistics/crop"
                            text = render()
            if rounds >= 6:
                text += "\n追加観測の予算終了。visual_requestsを空配列にし、現時点の根拠と未確認事項だけを返してください。"
            output_budget = budget
            if isinstance(self.client, LocalLLM) and self.client.managed:
                output_budget = min(budget, getattr(self.client, "context", 8192)-self.client.count_tokens(text)-256-len(images)*1024)
                if output_budget < 512:
                    raise InferenceUnavailable("追加視覚観測がモデルの入力枠を超えました。保存済み結果を保持します。")
            if len(text.encode()) > 100000:
                raise InferenceUnavailable("視覚研究の入力予算を超えました。")
            options = {"max_tokens": output_budget, "json_mode": True, "schema": PROPOSAL_SCHEMA, "on_progress": journal}
            if images:
                options["images"] = images
            response = self.complete(text, **options)
            try:
                value = json.loads(response.strip().removeprefix("```json").removesuffix("```").strip())
            except (ValueError, TypeError):
                return response  # Existing proposal repair retains invalid output.
            requests = value.get("visual_requests", []) if isinstance(value, dict) else []
            if not requests:
                return response
            if rounds >= 6:
                payload["visual_budget_exhausted"] = True
                return json.dumps({"findings": [], "problem": "追加観測の予算終了", "reason": "研究結果を保存し、次の経験を待ちます", "wat": ""}, ensure_ascii=False)
            if not isinstance(requests, list) or len(requests) > 4:
                raise ResearchRepairNeeded("visual_requestsは最大4件の配列にしてください。")
            self.store.proposal(jid, response)
            for request in requests:
                try:
                    if isinstance(request, dict) and request.get("op", "observe") == "observe" and request.get("output", "pixels") in {"pixels", "image"}:
                        request = {"limit": 128, **request}
                    # Large individual arrays remain accessible by paging. An
                    # oversized result never silently becomes a fabricated summary.
                    result, attachment = self.visual.handle(request, pin=pin, native=bool(getattr(self.client,"vision",False)))
                    if len(json.dumps(result).encode()) > 12000:
                        result = {"error": "VisualResultTooLarge", "detail": "Use a smaller region/size or pixels with limit <= 512"}
                        attachment = None
                    ids = sorted(evidence_ids(result.get("evidence", {})))
                    if ids:
                        observed = {k: result[k] for k in ("evidence", "box", "size", "mode", "mean", "component_count", "coordinates") if k in result}
                        note = "視覚APIで取得した観測（ゲーム意味は未確認）："+json.dumps(observed, ensure_ascii=False, separators=(",", ":"))
                        with self.store.lock:
                            exists = self.store.db.execute("SELECT 1 FROM knowledge WHERE kind='observation' AND text=?", (note,)).fetchone()
                        if not exists:
                            self.store.knowledge("observation", note, "observed", "unknown", ids)
                    resolved = resolved_request(request, result["evidence"]) if "evidence" in result else dict(request)
                    history.append({"request": resolved, "result": result, "native": bool(attachment)})
                except CapacityError:
                    raise
                except (ValueError, OSError, KeyError, TypeError) as error:
                    history.append({"request": request, "error": str(error)[:240]})
            payload["visual_rounds"] = rounds+1
            payload["visual_history"] = history
            payload.pop("stream_draft", None)
            self.store.set_job(jid, "running", payload)
        raise ResearchRepairNeeded("追加観測の応答を確認できませんでした。")

    def study(self, rows: list[dict], jid: str):
        self.activity.update(experiment="未確認の変化条件と観測方法を研究",next="仮説を形成して独立の経験で検証")
        self.notify("過去の経験を比較し、未確認の規則と観測方法を研究しています。")
        # Deterministic summaries are observations, not labels for game objects.
        for key in self.keys:
            selected = [r for r in rows if r["action"] == key]
            with self.store.lock:
                existing=self.store.db.execute("SELECT 1 FROM knowledge WHERE kind='observation' AND text LIKE ?",(f"入力候補{self.keys.index(key)+1}の後%",)).fetchone()
            if len(selected) >= 8 and not existing:
                index = self.keys.index(key) + 1
                self.store.knowledge("observation", f"入力候補{index}の後、画面変化を{len(selected)}回中{sum(r['change'] > .01 for r in selected)}回観測しました。意味と因果関係は未確認です。",
                                     "observed", "unknown", [r["id"] for r in selected[-32:]])
        payload=json.loads(self.store.get_job(jid)['payload'])
        if payload.get('research_rows'):rows=payload['research_rows']
        else:
            rows=rows[-16:]
            payload['research_rows']=rows
            self.store.set_job(jid,'running',payload)
            self.store.pin([r['id'] for r in rows],'research-job-'+jid)
        prompt=research_prompt(self.revisions.source(),rows,self.keys,self.store.reports()[:2])
        if payload.get('stream_draft') and not payload.get('repair_response'):
            payload['repair_response']=payload['stream_draft'];payload['repair_error']='保存された途中出力を完全なJSONに修正してください。'
        if payload.get('repair_response'):
            prompt+='\n前の出力を次の検査結果に従って修正し、完全なJSONを返してください。未完成コードの実行は禁止です。\n検査：'+payload.get('repair_error','')+'\n保存済み下書きの抜粋：'+payload['repair_response'][:6000]
        budget=4096
        if isinstance(self.client,LocalLLM) and self.client.managed:
            count=self.client.count_tokens(prompt)
            budget=min(4096,getattr(self.client,'context',8192)-count-256)
            if budget<512:raise InferenceUnavailable('研究の入力枠が不足しています。下書きを保存して待機します。')
        saved_at=[0.]
        def journal(text):
            if time.monotonic()-saved_at[0]>=.5:
                payload['stream_draft']=text
                self.store.set_job(jid,'running',payload)
                saved_at[0]=time.monotonic()
        try:
            response=self.visual_dialogue(prompt,jid,payload,budget,journal)
        except IncompleteInference:
            response=getattr(self.client,'last_response','')
            self.store.proposal(jid,response)
            payload['repair_response']=response;payload['repair_error']='出力上限で途切れています。説明は短くし、実行可能な完全な候補を返してください。'
            self.store.set_job(jid,'pending',payload)
            raise ResearchRepairNeeded('途中応答を保存しました。') from None
        except (InferenceUnavailable,InterruptedError):
            draft=getattr(self.client,'last_response','') or payload.get('stream_draft','')
            if draft:
                self.store.proposal(jid,draft)
                payload['repair_response']=draft;payload['repair_error']='接続または停止操作で途中までになりました。保存した内容から完全な応答へ修正してください。'
            self.store.set_job(jid,'pending',payload)
            raise
        self.store.proposal(jid,response)
        allowed={r['id'] for r in rows}
        for entry in payload.get('visual_history', []):
            allowed.update(evidence_ids(entry.get('result', {}).get('evidence', {})))
        try:proposal=parse_proposal(response,allowed,self.keys)
        except (ValueError,TypeError,KeyError,ResearchRepairNeeded) as error:
            payload['repair_response']=response;payload['repair_error']=str(error)[:500]
            self.store.set_job(jid,'pending',payload)
            raise ResearchRepairNeeded(str(error)) from error
        for finding in proposal.get("findings", [])[:8]:
            evidence = sorted(set(finding["evidence"]))
            if evidence and set(evidence) <= allowed:
                with self.store.lock:
                    existing=self.store.db.execute("SELECT id FROM knowledge WHERE kind='hypothesis' AND text=? AND evidence=?",(finding['text'],json.dumps(evidence))).fetchone()
                if existing:continue
                kid = self.store.knowledge("hypothesis", finding["text"], "inferred", "hypothesis", evidence)
                action, probability = finding.get("action"), finding.get("change_probability")
                if isinstance(action, int) and 0 <= action < len(self.keys) and isinstance(probability, (int, float)) and 0 <= probability <= 1:
                    self.store.claim(kid, self.keys[action], probability, rows[-1]["id"])
        source = proposal.get("wat", "")
        if not source:
            self.store.set_job(jid, "complete",{**payload,"outcome":"observations_only","implementation_verified":False})
            self.activity.update(experiment="観測・仮説を保存（実装改善なし）")
            return
        latest=self.store.rows(1)
        cutoff=max(rows[-1]['id'],payload.get('after',0),latest[-1]['id'] if latest else 0)
        self.evaluate_proposal(source,cutoff,jid)

    def evaluate_proposal(self, source: str, after_id: int, jid: str):
        # Persist the proposal before execution; resume it rather than regenerating it.
        payload=json.loads(self.store.get_job(jid)["payload"])
        self.store.set_job(jid,"running",{**payload,"source":source,"after":after_id})
        self.notify("改善候補を作成しました。新しい経験を集めてから独立した比較評価を行います。")
        deadline = time.monotonic() + 300
        future = []
        while not self.stop_event.wait(.5) and time.monotonic() < deadline:
            future = [r for r in self.store.rows(128,pin_reason='pending-'+jid) if r["id"] > after_id]
            if len(future)>=48 and (all(sum((r['change']>.01)==y for r in future)>=4 for y in (False,True)) or len(future)>=128):
                break
        if len(future) < 48:
            self.store.set_job(jid, "pending")
            return
        self.store.reserve(len(source.encode()) + 1024**2)
        self.activity.update(llm='評価中',comparison='過去経験で比較',experiment='予測方法の改善を検証')
        self.activity.emit('replay')
        self.store.pin([r['id'] for r in future],'evaluation-'+jid)
        self.store.unpin('pending-'+jid)
        result = evaluate(source, self.revisions.source(), future, self.store.root / "evidence", self.keys)
        historical = self.store.regression_rows(after_id,pin_reason='evaluation-'+jid)
        if historical:
            self.store.pin([r['id'] for r in historical],'evaluation-'+jid)
            regression = evaluate(source, self.revisions.source(), historical, self.store.root / "evidence", self.keys)
            base, candidate = regression["baseline"], regression["candidate"]
            regression_ok=regression_safe(base,candidate)
            result['regression']=regression
            if not regression_ok:result.update(passed=False,reason='過去経験のクラス別損失が悪化しました')
        if not result["passed"]:
            reason=result.get('reason','改善不足')
            self.store.report('candidate',False,'候補を保存しました。判定：'+reason,result)
            payload=json.loads(self.store.get_job(jid)['payload'])
            attempts=payload.get('behavior_repairs',0)
            if result.get('evidence_sufficient') is False:
                self.store.set_job(jid,'complete',{**payload,'outcome':'insufficient_evidence','implementation_verified':False,'evaluation_reason':reason})
                self.activity.update(llm='待機',comparison='変化あり・なしの比較不足')
                self.activity.emit('llm_saved')
            elif attempts<2:
                payload.pop('source',None)
                payload.update(behavior_repairs=attempts+1,repair_response=json.dumps({'findings':[],'wat':source},ensure_ascii=False),repair_error='動作比較の結果：'+reason,after=future[-1]['id'],outcome='behavior_repair',implementation_verified=False)
                self.store.set_job(jid,'pending',payload)
                self.activity.update(llm='待機',comparison='動作の改善を修正待ち')
                self.activity.emit('llm_saved')
            else:
                self.store.set_job(jid,'complete',{**payload,'outcome':'evaluated_not_adopted','implementation_verified':False,'evaluation_reason':reason})
                self.activity.update(llm='待機',comparison='改善未確認・記録を保存')
                self.activity.emit('rejected')
            return
        revision = self.revisions.create(source)
        with self.lock:
            self.live = {"revision": revision, "agent": Sandbox(source), "base": self.revisions.load(), "job": jid,
                         "replay": result, "metrics": {"base": {"samples": []}, "agent": {"samples": []}}}
        self.activity.update(llm='待機',candidate=revision,comparison='実機で交互比較')
        self.activity.emit('comparison')
        self.notify("保存経験で改善を確認しました。実際の入力で短い交互比較を行っています。")
