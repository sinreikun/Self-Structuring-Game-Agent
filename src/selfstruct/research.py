from __future__ import annotations

import json
import hashlib
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


class ResearchInputLimit(InferenceUnavailable):
    """A deterministic context limit; restarting inference cannot repair it."""


MAX_REPAIR_FAILURES = 3
MAX_IDENTICAL_FAILURES = 2


PROPOSAL_SCHEMA={
    'type':'object','additionalProperties':False,
    'required':['findings','actions','problem','reason','wat'],
    'properties':{
        'findings':{'type':'array','maxItems':8,'items':{'type':'object','additionalProperties':False,
            'required':['text','evidence'],'properties':{
                'id':{'type':'string','maxLength':40},
                'text':{'type':'string','maxLength':2000},
                'evidence':{'type':'array','minItems':1,'maxItems':64,'items':{'type':'integer'}},
                'action':{'type':'integer','minimum':0},
                'change_probability':{'type':'number','minimum':0,'maximum':1}}}},
        'actions':{'type':'array','maxItems':4,'items':{'type':'object'}},
        'visual_requests':{'type':'array','maxItems':4,'items':{'type':'object'}},
        'problem':{'type':'string'},'reason':{'type':'string'},'wat':{'type':'string'}}}


from .research_schema import action_schema, response_schema, repair_kind
PROPOSAL_SCHEMA['properties']['actions']['items']=action_schema()
PROPOSAL_SCHEMA['properties']['visual_requests']['items']=action_schema(legacy=True)

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
{{"findings":[], "visual_requests":[], "problem":"", "reason":"", "wat":""}}
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
featureは量子化された画像ハッシュです。その大小・ビット位置に、画面上の位置や色・形の意味はありません。画面構造は画素Evidenceで調べてください。
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
    prompt='画面Evidenceを調べ、分かったこととunknownを区別してください。初期Before/Afterは低解像度の概観です。情報不足ならactions（旧visual_requestsも可）で領域・解像度・処理・比較・実験を選び、取得結果を見て続行してください。WATは任意の最終手段で、必要な場合はcandidateで実装計画を要求してください。採用評価では変化あり・なしを独立比較し、定数0/0.5/1・過去頻度・現行Agentの基準を維持します。\n'
    prompt+='ゲーム名や攻略を補完せず、分からない意味はunknownです。\n'
    prompt+='経験の略記：a=入力番号、f=画像特徴、d=画面変化。\n'
    prompt+='許可入力（0始まり。キー名は識別子で、ゲーム内の意味は未確認）：'+json.dumps(dict(enumerate(keys)),ensure_ascii=False,separators=(',',':'))+'\n'
    prompt+='過去評価：'+json.dumps(history,ensure_ascii=False,separators=(',',':'))+'\n'
    # World research does not request a full program; implementation has its own phase.
    from .research_actions import INSTRUCTIONS
    prompt+=INSTRUCTIONS+'\n経験:\n'+json.dumps(summaries,separators=(',',':'))
    # UTF-8 byte budget is conservative for the 8192-token managed context.
    # Never silently cut an Agent program or invent an answer to fit the budget.
    if len(prompt.encode('utf-8'))>20000:
        raise ResearchInputLimit('研究入力がコンテキスト予算を超えています。Agentの自己記述を短くしてください。')
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
            arm=row.get('arm','unrecorded')
            live.setdefault('behavior_samples',[]).append({'id':row['id'],'arm':arm,'feature':row['feature'],'after_feature':after,'action':row['action'],'change':row['change'],'seconds':row.get('cost')})
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
            behavior={}
            for arm in ('base','agent'):
                observed=[r for r in live.get('behavior_samples',[]) if r['arm']==arm]
                seconds=sum(r.get('seconds') or 0 for r in observed)
                states={r['after_feature'] for r in observed}
                behavior[arm]={'n':len(observed),'distinct_visual_hashes':len(states),'changed':sum(r['change']>.01 for r in observed),
                               'distinct_states_per_second':len(states)/seconds if seconds else None,'seconds':seconds,
                               'repeated_action_state_pairs':len(observed)-len({(r['feature'],r['action']) for r in observed})}
            behavior['limitation']='Alternating observational comparison; hashes are visual proxies, not game states or causal proof. Adoption still requires predictive and regression gates.'
            self.store.report(live['revision'], adopted, summary, {'replay':live['replay'],'live':live['metrics'],'reason':reason,'evaluation_schema':2,'behavior':behavior})
            payload=json.loads(self.store.get_job(live['job'])['payload'])
            sufficient=all(cm['classes'][k]['n']>=4 for k in ('changed','unchanged'))
            payload.setdefault('progress',{})['unknown_reduction']=bm['unknown']-cm['unknown']
            payload.update(revision=live['revision'],adopted=adopted,implementation_verified=adopted,
                           outcome='adopted' if adopted else ('evaluated_not_adopted' if sufficient else 'insufficient_evidence'),evaluation_reason=reason)
            state='complete'
            if not adopted and sufficient and payload.get('behavior_repairs',0)<2:
                source=self.revisions.source(live['revision'])
                payload.pop('source',None)
                payload.update(behavior_repairs=payload.get('behavior_repairs',0)+1,
                               repair_kind='implementation',repair_response=json.dumps({'findings':[],'wat':source},ensure_ascii=False),
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
                payload.update(repair_kind='implementation',repair_response=json.dumps({'findings':[],'wat':source}),repair_error=reason,retained_live=self.live['metrics'])
                self.store.set_job(self.live['job'],'pending',payload)
                self.live = None
                self.store.unpin('live-trial')
                self.activity.update(comparison='保存して修正待ち',candidate='')

    def expire_live_time(self):
        with self.lock:
            if not self.live:return
            live=self.live;payload=json.loads(self.store.get_job(live['job'])['payload'])
            if not self.research_time_expired(payload):return
            payload.update(source=self.revisions.source(live['revision']),retained_live=live['metrics'])
            self.live=None
            self.store.unpin('live-trial')
            self.finish_research_time(live['job'],payload)

    def research_time_expired(self,payload):
        from .research_actions import MAX_SECONDS
        return 'research_started' in payload and time.time()-payload['research_started']>=MAX_SECONDS

    def finish_research_time(self,jid,payload):
        payload.update(research_limit='time',outcome='research_limit',implementation_verified=False)
        self.store.set_job(jid,'complete',payload)
        for prefix in ('research-job-','pending-','suspended-'):self.store.unpin(prefix+jid)
        self.activity.record('RESEARCH',f'limit job={jid} reason=time phase={payload.get("research_phase","evaluation")}')
        self.activity.update(llm='待機',experiment='研究時間上限・根拠と候補を保存',comparison='未確認のまま終了')

    def finish_failed_research(self, jid, payload, error, outcome="repair_exhausted"):
        payload.update(outcome=outcome, implementation_verified=False, repair_error=str(error)[:1000],
                       response="候補を採用せず保存しました。修正上限または入力制約のため、新しい経験で研究を進めます。")
        self.store.report("candidate", False, "修正を打ち切り、根拠と候補を保存して新しい研究へ進みます。",
                          {"job": jid, "error": str(error)[:1000], "outcome": outcome,
                           "repair_failures": payload.get("repair_failures", 0), "implementation_verified": False})
        for prefix in ("research-job-", "pending-", "suspended-"):
            self.store.unpin(prefix+jid)
        self.store.set_job(jid, "complete", payload)
        self.activity.update(llm="待機", comparison="候補を保存・新しい研究へ", experiment="修正上限に達した研究を保存")
        self.activity.emit("research_exhausted")
        self.notify("同じ候補の修正を終了しました。既存の個体を保持し、新しい画面Evidenceで次の研究を行います。")

    def restore_repair_budget(self, jid, payload):
        """Upgrade queued pre-fix jobs using their existing failure reports."""
        if "repair_failures" not in payload:
            failures=[]
            with self.store.lock:
                records=self.store.db.execute("SELECT metrics FROM reports ORDER BY id DESC LIMIT 256").fetchall()
            for record in reversed(records):
                try:metrics=json.loads(record[0])
                except (ValueError, TypeError):continue
                if isinstance(metrics,dict) and metrics.get("job")==jid and metrics.get("error") and metrics.get("outcome")!="repair_exhausted":
                    failures.append(str(metrics["error"]))
            payload["repair_failures"]=len(failures)
            previous=None;consecutive=0
            for error in failures:
                fingerprint=hashlib.sha256(error[:4000].encode()).hexdigest()
                consecutive=consecutive+1 if fingerprint==previous else 1
                previous=fingerprint
            payload.update(last_repair_fingerprint=previous, same_error_failures=consecutive)
            if failures:
                payload["repair_error"]=failures[-1][:1000]
                self.store.set_job(jid,"running",payload)
        if payload.get("repair_failures",0)>=MAX_REPAIR_FAILURES or payload.get("same_error_failures",0)>=MAX_IDENTICAL_FAILURES:
            self.finish_failed_research(jid,payload,payload.get("repair_error","修正回数の上限"))
            return True
        return False

    def repair_or_finish(self, jid, error):
        payload=json.loads(self.store.get_job(jid)["payload"])
        source=payload.pop("source",None)
        if source is not None:
            payload["repair_kind"]="implementation"
            payload["repair_response"]=json.dumps({"findings":[],"wat":source},ensure_ascii=False)
        elif not payload.get("repair_response") and payload.get("stream_draft"):
            payload["repair_response"]=payload["stream_draft"]
        payload.setdefault("repair_kind",repair_kind(payload))
        text=str(error)[:4000]
        fingerprint=hashlib.sha256(text.encode()).hexdigest()
        payload["same_error_failures"]=payload.get("same_error_failures",0)+1 if payload.get("last_repair_fingerprint")==fingerprint else 1
        payload["last_repair_fingerprint"]=fingerprint
        payload["repair_failures"]=payload.get("repair_failures",0)+1
        payload["repair_error"]=text[:1000]
        payload["repair_history"]=[*payload.get("repair_history",[])[-2:],
                                   {"attempt":payload["repair_failures"],"error":text[:1000],"at":time.time()}]
        self.store.report("candidate",False,"候補の検査に失敗しました。有限回の修正を行います。",
                          {"job":jid,"error":text,"repair_failures":payload["repair_failures"]})
        if payload["repair_failures"]>=MAX_REPAIR_FAILURES or payload["same_error_failures"]>=MAX_IDENTICAL_FAILURES:
            self.finish_failed_research(jid,payload,text)
            return False
        self.store.set_job(jid,"pending",payload)
        self.activity.update(llm="待機",comparison=f"修正待ち（失敗{payload['repair_failures']}/{MAX_REPAIR_FAILURES}）")
        self.activity.emit("llm_saved")
        self.notify("候補の不適合と下書きを保存しました。上限内で修正し、解決しなければ次の研究へ進みます。")
        return True

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
            self.expire_live_time()
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
                if self.research_time_expired(payload):
                    self.finish_research_time(jid,payload);continue
                if self.restore_repair_budget(jid,payload):
                    continue
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
            except ResearchInputLimit as error:
                payload=json.loads(self.store.get_job(jid)["payload"])
                self.finish_failed_research(jid,payload,error,"input_budget_exceeded")
            except ResearchRepairNeeded as error:
                if self.repair_or_finish(jid,error):
                    self.stop_event.wait(min(30,self.retry_base_delay))
            except (ValueError, TimeoutError, MemoryError) as error:
                log.warning('Candidate failed finite repair: %s',error)
                if self.repair_or_finish(jid,error):
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
            if budget<512:raise ResearchInputLimit('入力が大きいため作業を保存して待機します。')
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

    def initial_visual_evidence(self, jid, payload):
        if payload.get("initial_visual_evidence"):
            return
        rows=payload.get("research_rows",[])
        if not rows:
            return
        # Prioritize predictive errors/large changes, tie-breaking by recency.
        def priority(row):
            outcome=int(row.get('change',0)>.01);prediction=row.get('prediction',-1)
            return (abs(outcome-prediction) if type(prediction) in (float,int) and prediction>=0 else .5,row.get('change',0),row['id'])
        event=max(rows,key=priority)['id']
        with self.store.lock:
            present=self.store.db.execute("SELECT 1 FROM events WHERE id=? AND state='complete'",(event,)).fetchone()
        if not present:
            return
        history=payload.setdefault("visual_history",[])
        native=bool(getattr(self.client,"vision",False))
        for phase in ("before","after"):
            request={"op":"observe","event":event,"phase":phase,"mode":"RGB",
                     "size":[128,128] if native else [8,8],"output":"image" if native else "pixels","limit":192}
            try:
                result,attachment=self.visual.handle(request,pin="research-job-"+jid,native=native)
                history.append({"kind":"initial_evidence","request":request,"result":result,"native":bool(attachment)})
            except CapacityError:
                raise
            except (ValueError,OSError,KeyError,TypeError) as error:
                history.append({"kind":"initial_evidence","request":request,"error":str(error)[:240]})
        payload["initial_visual_evidence"]=True
        self.store.set_job(jid,"running",payload)

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
        from .research_actions import (MAX_CALLS,MAX_SECONDS,MAX_IMAGE_BYTES,INSTRUCTIONS,
            record_findings,run_actions,needs_followup,history_context,progress,allowed_evidence,context_entry,hypothesis_context,experiment_context)
        payload.setdefault('research_started',time.time());payload['research_job']=jid
        if payload.get('pending_actions'):
            run_actions(self,jid,payload,payload['pending_actions']);payload.pop('pending_actions',None)
        self.initial_visual_evidence(jid,payload)
        for _ in range(MAX_CALLS+1):
            payload['experiment_context']=experiment_context(self,jid)
            payload['hypothesis_context']=hypothesis_context(self,payload)
            if payload.get('research_calls',0)>=MAX_CALLS or time.time()-payload['research_started']>=MAX_SECONDS or payload.get('research_limit'):
                payload.setdefault('research_limit','calls' if payload.get('research_calls',0)>=MAX_CALLS else 'time')
                self.activity.record('RESEARCH',f'limit job={jid} reason={payload["research_limit"]}')
                self.store.set_job(jid,'running',payload)
                return json.dumps({'findings':[],'wat':'','reason':'研究上限。成果と未確認事項は保存済み。'},ensure_ascii=False)
            if self.stop_event.is_set():
                raise InterruptedError("研究を中断しました。")
            images = []
            # Earlier numeric payloads stay durably journaled and can be requested
            # again by exact ID. Keep current context focused on the latest turn.
            visible = []
            if len(history)>8:payload['context_compactions']=payload.get('context_compactions',0)+1
            for i, entry in enumerate(history[-8:],start=max(0,len(history)-8)):
                item=context_entry(entry,older=i<len(history)-4)
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
            issued_step=payload.get('research_calls',0)+1
            new_ids=[f'r{issued_step}_{i}' for i in range(1,9) if f'r{issued_step}_{i}' not in payload.get('hypotheses',{})]
            payload['new_hypothesis_ids']=new_ids
            prefix = prompt+"\n"+VISUAL_INSTRUCTIONS+("" if INSTRUCTIONS in prompt else "\n"+INSTRUCTIONS)+"\nモデル画像入力: "+str(bool(getattr(self.client,"vision",False)))
            if payload.get('research_phase')=='implementation':
                prefix='実装フェーズ。根拠と必要挙動を満たす完全な候補だけを作成してください。新しい世界仮説は不要。\n計画：'+json.dumps(payload['implementation_plan'],ensure_ascii=False)+'\n'+PROMPT[PROMPT.index('Agent ABI:'):].format(source=self.revisions.source(),rows='[]')
            implementation_repair=bool(payload.get('repair_response') and repair_kind(payload)=='implementation')
            if implementation_repair:
                prefix='修正フェーズ。新しい世界理解・仮説・追加実験は不要。保存された候補の構文・ABI・評価エラーだけを修正してください。\n検査：'+payload.get('repair_error','')+'\n候補：'+payload['repair_response'][:12000]+'\n'+PROMPT[PROMPT.index('Agent ABI:'):PROMPT.index('featureは')]+ '\nJSON形式 {"findings":[],"actions":[],"wat":"完全なモジュール"}'
                visible=[];images=[]
            elif payload.get('repair_response'):
                prefix+='\n研究JSONの形式修正。WAT生成・ABI修正へ移行しないでください。操作名はopの列挙値だけ、パラメータは別のJSONフィールドです。\n検査：'+payload.get('repair_error','')+'\n不完全な応答：'+payload['repair_response'][:3000]
            prefix+='\n根拠IDはExperience IDのみ。observation IDは観測参照用で、findings.evidenceへ混ぜません。許可Evidence IDs：'+json.dumps(sorted(allowed_evidence(payload)))
            prefix+='\n参照中の仮説（保存された主張と事前登録。変更は別IDへ）：'+json.dumps(payload['hypothesis_context'],ensure_ascii=False,separators=(',',':'))
            prefix+='\n今回の新規findings.id候補：'+json.dumps(new_ids)+'。既存仮説を使うだけならfindingsを空にしてactions.hypothesisへ既存の別名／IDを指定する。主張・事前登録を変更するなら新規IDで別仮説を定義する。'
            prefix+='\n研究履歴：'+history_context(payload)
            def render():
                return prefix+"\n追加観測結果（attachment_indexは画像添付の0始まり番号。古い結果はJob履歴へ保持し必要なら再要求）:"+json.dumps(visible, ensure_ascii=False, separators=(",", ":"))
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
                while images and self.client.count_tokens(text)+len(images)*1024+256+512 > getattr(self.client,"context",8192):
                    images.pop(0)
                    for item in visible:
                        if "attachment_index" not in item:
                            continue
                        if item["attachment_index"] == 0:
                            item.pop("attachment_index")
                            item["attachment_omitted"] = "Image exceeds available context; request fewer images or a smaller crop"
                        else:
                            item["attachment_index"] -= 1
                    text = render()

            output_budget = budget
            if isinstance(self.client, LocalLLM) and self.client.managed:
                output_budget = min(budget, getattr(self.client, "context", 8192)-self.client.count_tokens(text)-256-len(images)*1024)
                if output_budget < 512:
                    raise ResearchInputLimit("追加視覚観測がモデルの入力枠を超えました。保存済み結果を保持します。")
            if len(text.encode()) > 100000:
                raise ResearchInputLimit("視覚研究の入力予算を超えました。")
            options = {"max_tokens": output_budget, "json_mode": True, "schema": response_schema(PROPOSAL_SCHEMA,self.keys,allowed_evidence(payload),new_ids,payload.get('hypotheses',{})), "on_progress": journal}
            if images:
                options["images"] = images
            image_bytes=sum(len(i.encode()) for i in images)
            if payload.get('image_bytes',0)+image_bytes>MAX_IMAGE_BYTES:
                payload['research_limit']='image_bytes';continue
            payload['image_bytes']=payload.get('image_bytes',0)+image_bytes
            payload['research_calls']=payload.get('research_calls',0)+1
            step={'step':payload['research_calls'],'phase':('repair' if implementation_repair else 'protocol_repair') if payload.get('repair_response') else payload.get('research_phase','observation'),
                  'started_at':time.time(),'consumed_actions':[r['action_id'] for r in payload.get('action_history',[]) if r['state'] in ('done','duplicate','error')], 'image_count':len(images)}
            payload.setdefault('research_steps',[]).append(step)
            self.store.set_job(jid,'running',payload)
            self.activity.update(experiment=f'研究Job {jid[:8]}：取得結果を再分析（step {payload["research_calls"]}）' if payload.get('action_history') else f'研究Job {jid[:8]}：観測・仮説を分析（step {payload["research_calls"]}）',next='必要な観測・実験をLLMが選択します')
            self.activity.record('RESEARCH',f'resume job={jid} step={payload["research_calls"]} results={len(payload.get("action_history",[]))}')
            response = self.complete(text, **options)
            step['finished_at']=time.time()
            self.store.set_job(jid,'running',payload)
            try:
                value = json.loads(response.strip().removeprefix("```json").removesuffix("```").strip())
            except (ValueError, TypeError):
                return response  # Existing proposal repair retains invalid output.
            if not isinstance(value,dict):return response
            try:record_findings(self,jid,payload,value)
            except (ValueError,TypeError,KeyError,ResearchRepairNeeded) as error:
                payload['repair_response']=response;payload['repair_kind']='protocol';payload['repair_error']=str(error)[:500]
                self.store.proposal(jid,response)
                self.store.set_job(jid,'pending',payload)
                raise ResearchRepairNeeded(str(error)) from error
            if not implementation_repair:
                payload.pop('repair_response',None);payload.pop('repair_error',None);payload.pop('repair_kind',None)
            payload.pop('stream_draft',None)
            payload['last_research_response']=value
            actions=value.get('actions',[]);legacy=value.get('visual_requests',[])
            if isinstance(actions,list) and any(not isinstance(a,dict) or not isinstance(a.get('op'),str) for a in actions):
                raise ResearchRepairNeeded('actionsには明示的なopを持つオブジェクトを指定してください。')
            requests=actions+[{"op":"observe",**r} if isinstance(r,dict) else r for r in legacy] if isinstance(actions,list) and isinstance(legacy,list) else None
            if requests is None or len(requests)>4:raise ResearchRepairNeeded('actions/visual_requestsは合計最大4件の配列にしてください。')
            if not requests:
                payload['experiment_context']=experiment_context(self,jid)
                payload['hypothesis_context']=hypothesis_context(self,payload)
                if value.get('wat'):
                    progress(payload)['candidates']+=1
                    payload['candidate_sha256']=hashlib.sha256(value['wat'].encode()).hexdigest()
                    self.activity.record('CANDIDATE',f'generated job={jid} sha256={payload["candidate_sha256"]}')
                    self.store.set_job(jid,'running',payload)
                    return response
                with self.store.lock:
                    previous=self.store.db.execute("SELECT value FROM meta WHERE key='research-no-action-streak'").fetchone()
                streak=int(previous[0]) if previous else 0
                if not payload.get('repair_response') and not payload.get('redirected') and (needs_followup(value) or streak>=2):
                    payload['redirected']=True
                    payload['stagnation_detected']=True
                    payload['redirect']='追加観測・実験はまだ実行されていません。仮説は未検証です。次に何が不明で、どのEvidence・領域・解像度・入力を確認するかactionsで明示してください。不要なら根拠を述べて完了してください。Hostは観測場所や入力を選びません。'
                    self.activity.record('RESEARCH',f'redirect job={jid} step={payload["research_calls"]} reason=no_concrete_action')
                    self.store.set_job(jid,'running',payload)
                    continue
                with self.store.lock,self.store.db:
                    self.store.db.execute("INSERT INTO meta VALUES('research-no-action-streak',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(streak+1 if not payload.get('action_history') else 0),))
                from .research_actions import made_progress
                payload['stalled']=bool((payload.get('stagnation_detected') or payload.get('action_history')) and not made_progress(payload))
                self.store.set_job(jid,'running',payload)
                return response
            if implementation_repair:raise ResearchRepairNeeded('修正フェーズではactionsを空にし、保存候補だけを修正してください。')
            self.store.proposal(jid,response)
            payload['pending_actions']=requests
            self.store.set_job(jid,'running',payload)
            run_actions(self,jid,payload,requests)
            turn=[entry for entry in payload.get('action_history',[]) if entry.get('step')==payload['research_calls']]
            payload['duplicate_only_rounds']=payload.get('duplicate_only_rounds',0)+1 if turn and all(entry['state']=='duplicate' for entry in turn) else 0
            if payload['duplicate_only_rounds']>=2:
                payload['duplicate_redirects']=payload.get('duplicate_redirects',0)+1
                payload['redirect']='同一の結果は既に返却済みです。同じpixelsの再要求だけでは追加範囲を取得できません。next_offsetがあるならoffsetで別の区間を要求できます。必要なら別Evidence・領域・解像度・解析を選ぶか、今の根拠と未確認事項を保存して完了してください。Hostは観測先や入力を決定しません。'
                self.activity.record('RESEARCH',f'redirect job={jid} reason=repeated_cached_results')
            payload.pop('pending_actions',None)
            payload['visual_rounds']=rounds+1
            payload.pop('stream_draft',None)
            self.store.set_job(jid,'running',payload)
        payload['research_limit']='calls'
        return '{"findings":[],"wat":""}'

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
        if payload.get('repair_response') and repair_kind(payload)=='implementation':prompt='修正フェーズ'
        budget=4096
        if isinstance(self.client,LocalLLM) and self.client.managed:
            count=self.client.count_tokens(prompt)
            budget=min(4096,getattr(self.client,'context',8192)-count-256)
            if budget<512:raise ResearchInputLimit('研究の入力枠が不足しています。下書きを保存して待機します。')
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
            payload['repair_response']=response;payload.setdefault('repair_kind',repair_kind(payload));payload['repair_error']='出力上限で途切れています。説明は短くし、実行可能な完全な候補を返してください。'
            self.store.set_job(jid,'pending',payload)
            raise ResearchRepairNeeded('途中応答を保存しました。') from None
        except (InferenceUnavailable,InterruptedError):
            draft=getattr(self.client,'last_response','') or payload.get('stream_draft','')
            if draft:
                self.store.proposal(jid,draft)
                payload['repair_response']=draft;payload.setdefault('repair_kind',repair_kind(payload));payload['repair_error']='接続または停止操作で途中までになりました。保存した内容から完全な応答へ修正してください。'
            self.store.set_job(jid,'pending',payload)
            raise
        self.store.proposal(jid,response)
        from .research_actions import allowed_evidence
        allowed=allowed_evidence(payload)
        for entry in payload.get('visual_history', []):
            allowed.update(evidence_ids(entry.get('result', {}).get('evidence', {})))
        try:proposal=parse_proposal(response,allowed,self.keys)
        except (ValueError,TypeError,KeyError,ResearchRepairNeeded) as error:
            payload['repair_response']=response;payload['repair_kind']='protocol';payload['repair_error']=str(error)[:500]
            self.store.set_job(jid,'pending',payload)
            raise ResearchRepairNeeded(str(error)) from error
        source = proposal.get("wat", "")
        if not source:
            from .research_actions import made_progress
            self.store.set_job(jid, "complete",{**payload,"outcome":"research_limit" if payload.get("research_limit") else "stalled" if payload.get("stalled") else "research_progress" if made_progress(payload) else "observations_only","implementation_verified":False})
            self.activity.update(experiment='研究上限・成果を保存' if payload.get('research_limit') else '研究停滞・未確認事項を保存' if payload.get('stalled') else '観測・実験結果を保存（実装改善なし）')
            self.activity.record('RESEARCH',f'complete job={jid} calls={payload.get("research_calls",0)} progress={payload.get("progress",{})}')
            return
        latest=self.store.rows(1)
        cutoff=max(rows[-1]['id'],payload.get('after',0),latest[-1]['id'] if latest else 0)
        self.evaluate_proposal(source,cutoff,jid)

    def evaluate_proposal(self, source: str, after_id: int, jid: str):
        # Persist the proposal before execution; resume it rather than regenerating it.
        payload=json.loads(self.store.get_job(jid)["payload"])
        payload={**payload,"source":source,"after":after_id}
        if self.research_time_expired(payload):self.finish_research_time(jid,payload);return
        self.store.set_job(jid,"running",payload)
        digest=hashlib.sha256(source.encode()).hexdigest()
        if payload.get("validated_source_digest")!=digest:
            self.activity.update(llm="評価中",comparison="候補の構文・ABIを先に検査")
            evaluate(source,self.revisions.source(),[],self.store.root/"evidence",self.keys)
            payload["validated_source_digest"]=digest
            self.store.set_job(jid,"running",payload)
        self.notify("改善候補を作成しました。新しい経験を集めてから独立した比較評価を行います。")
        from .research_actions import MAX_SECONDS
        remaining=MAX_SECONDS-(time.time()-payload.get('research_started',time.time()))
        deadline = time.monotonic() + min(300,max(0,remaining))
        future = []
        while not self.stop_event.wait(.5) and time.monotonic() < deadline:
            future = [r for r in self.store.rows(128,pin_reason='pending-'+jid) if r["id"] > after_id]
            if len(future)>=48 and (all(sum((r['change']>.01)==y for r in future)>=4 for y in (False,True)) or len(future)>=128):
                break
        if self.research_time_expired(payload):self.finish_research_time(jid,payload);return
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
                payload.update(behavior_repairs=attempts+1,repair_kind='implementation',repair_response=json.dumps({'findings':[],'wat':source},ensure_ascii=False),repair_error='動作比較の結果：'+reason,after=future[-1]['id'],outcome='behavior_repair',implementation_verified=False)
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
