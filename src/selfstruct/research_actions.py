"""Bounded, journaled research actions; no natural-language-to-key translation."""
import hashlib
import json
import re
import time

from .store import CapacityError
from .vision import evidence_ids, resolved_request

MAX_CALLS=10
MAX_ACTIONS=24
MAX_SECONDS=600
MAX_IMAGE_BYTES=4*1024*1024

INSTRUCTIONS='''研究フェーズではWAT生成は必須ではありません。情報不足なら推測で完了せずactionsを要求してください。
形式: {"findings":[{"id":"h1","text":"仮説","evidence":[ID]}],"actions":[],"problem":"","reason":"","wat":""}
入力番号は0始まり、現在個体の許可入力だけです。次を見たい・試したい場合は文章だけで終わらず具体的actionsへ進んでください。
actionsは最大4件、opを指定:
observe: event,phase=before/after/diff,box=[左,上,右,下],size=[幅,高さ],mode=RGB/L,output=pixels/statistics/histogram/components/image,operations,offset,limit。event省略は現在画面。
compare: observeと同じ指定にcompare={event,phase,box}を追加。2つのEvidenceの同一領域を比較。
analyze: observeと同じ指定にoperations=[{op:edge/threshold/grayscale/blur/invert,...}]を追加。解析器APIもactionsから直接利用可。
experiment: hypothesis=h1または既存仮説ID,action=入力番号,duration=10..500ミリ秒,wait=10..2000ミリ秒,repeat=1..8。最大4計画/32入力。condition={box,size,mode,operations,mean_min:0..255,mean_max:0..255,max_wait_steps:1..128}で画素平均範囲を満たす状態を待てる。意味ラベルは禁止。省略は現在状態で実行。PAUSE中は入力しない。STOP/中断は再送しない。
evaluate: hypothesis=h1またはID,change_probability=0..1。実験の独立した完了経験16件以上で画面変化確率のみ統計比較。少数・異なる入力混在・意味推測は判定不能。
candidate: problem, evidence=[取得済みIDを2件以上], behavior, evaluationを指定すると実装フェーズへ。根拠・必要挙動・評価条件を揃えてから要求。
findingsにactionとchange_probabilityを仮説作成時に指定すると事前登録となる（無条件の指定入力後の画面変化確率のみ）。条件付き仮説の意味・因果は未確認のまま。findingsのidをexperiment/evaluateで参照可能。各結果は同じJobへ返され、次の推論で使える。新しい根拠、仮説、検証、解析器、候補は進展として記録する。観測・仮説だけの完了も正当。ただし次の行動が必要なら実行せず完了としない。
同一の過去Evidence・領域・解像度・処理を繰り返しても新情報にはならない。1 Job最大10推論/24要求/600秒/画像合計4MiB。上限時は成果と未確認事項を保存して終了。'''


def allowed_evidence(payload):
    ids={r['id'] for r in payload.get('research_rows',[])}
    for h in payload.get('visual_history',[]):ids.update(evidence_ids(h.get('result',{}).get('evidence',{})))
    for h in payload.get('action_history',[]):
        for row in h.get('result',{}).get('events',[]):
            if row.get('state')=='complete':ids.add(row['id'])
    return ids


def progress(payload):
    return payload.setdefault('progress',{'new_evidence':0,'new_hypotheses':0,'hypotheses_evaluated':0,'experiments':0,'analysis_methods':0,'candidates':0,'candidate_plans':0,'unknown_reduction':None})


def record_findings(scientist,jid,payload,value):
    from .research import parse_proposal
    proposal=parse_proposal(json.dumps(value,ensure_ascii=False),allowed_evidence(payload),scientist.keys)
    mappings=payload.setdefault('hypotheses',{})
    for finding in proposal['findings']:
        ids=sorted(set(finding['evidence']))
        with scientist.store.lock:
            old=scientist.store.db.execute("SELECT id FROM knowledge WHERE kind='hypothesis' AND text=? AND evidence=?",(finding['text'],json.dumps(ids))).fetchone()
        kid=old[0] if old else scientist.store.knowledge('hypothesis',finding['text'],'inferred','hypothesis',ids)
        if not old:
            progress(payload)['new_hypotheses']+=1
            action,probability=finding.get('action'),finding.get('change_probability')
            if type(action)==int and type(probability) in (float,int):scientist.store.claim(kid,scientist.keys[action],probability,max(ids))
            scientist.activity.record('HYPOTHESIS',f'job={jid} id={kid} pending evidence={ids}')
        ref=finding.get('id')
        if isinstance(ref,str) and 1<=len(ref)<=40:
            if ref in mappings and mappings[ref]!=kid:raise ValueError('Hypothesis reference cannot be rebound')
            mappings[ref]=kid
    scientist.store.set_job(jid,'running',payload)


def hypothesis_id(payload,request):
    value=request.get('hypothesis')
    if isinstance(value,str):
        if value not in payload.get('hypotheses',{}):raise ValueError('Unknown hypothesis reference')
        value=payload['hypotheses'][value]
    if value is not None and type(value)!=int:raise ValueError('Unknown hypothesis reference')
    return value


def execute(scientist,jid,payload,request,token):
    if not isinstance(request,dict):raise ValueError('Research action must be an object')
    if len(json.dumps(request).encode())>12000:raise ValueError('Research action too large')
    op=request.get('op','observe');p=progress(payload)
    payload['research_phase']={'experiment':'experiment','evaluate':'evaluation','analyzer_create':'analyzer_creation','analyzer_evaluate':'evaluation','analyze':'analysis'}.get(op,'observation')
    if op=='experiment':
        if not getattr(scientist,'experiments',None):raise ValueError('Player experiment executor unavailable')
        identifier=scientist.experiments.submit(jid,token,request,hypothesis_id(payload,request))
        scientist.activity.update(experiment=f'研究Job {jid[:8]}：Experiment {identifier[:8]} の結果待ち',next='Playerで許可入力を試し、同じ研究へ結果を返します')
        while not scientist.stop_event.wait(.05):
            row=scientist.experiments.get(identifier)
            if row['state'] not in ('queued','running'):break
            if time.time()-payload['research_started']>MAX_SECONDS or time.time()-row['created']>120:scientist.experiments.finish(identifier,'expired')
        else:
            scientist.experiments.finish(identifier,'interrupted')
            raise InterruptedError('Experiment interrupted; inputs will not be replayed')
        row=scientist.experiments.get(identifier)
        result=json.loads(row['result']) or {'experiment':identifier,'state':row['state'],'events':[]}
        if result.get('completed'):p['experiments']+=1;p['new_evidence']+=result['completed']
        payload['research_phase']='evaluation'
        return result,None,None
    if op=='evaluate':
        if not getattr(scientist,'experiments',None):raise ValueError('Experiment evaluator unavailable')
        result=scientist.experiments.evaluate(hypothesis_id(payload,request),request.get('change_probability'))
        p['hypotheses_evaluated']+=1
        return result,None,None
    if op=='candidate':
        ids=request.get('evidence')
        if not isinstance(ids,list) or len(set(ids))<2 or any(type(i)!=int or i not in allowed_evidence(payload) for i in ids):raise ValueError('Implementation requires at least 2 inspected Evidence IDs')
        if any(not isinstance(request.get(k),str) or not 1<=len(request[k])<=1000 for k in ('problem','behavior','evaluation')):raise ValueError('Implementation requires problem, behavior and evaluation')
        payload['implementation_plan']={k:request[k] for k in ('problem','evidence','behavior','evaluation')};payload['research_phase']='implementation';p['candidate_plans']+=1
        return {'phase':'implementation','plan':payload['implementation_plan']},None,None
    translated=dict(request)
    if op in ('compare','analyze'):translated['op']='observe'
    if translated.get('op','observe')=='observe' and translated.get('output','pixels') in ('pixels','image'):translated={'limit':128,**translated}
    scientist.activity.update(experiment=f'研究Job {jid[:8]}：{op} Experience {request.get("event","現在")} {request.get("phase","before")} 領域 {request.get("box","全体")} 解像度 {request.get("size","既定")}',next='取得結果を同じ研究Jobへ返して再推論')
    result,attachment=scientist.visual.handle(translated,pin='research-job-'+jid,native=bool(getattr(scientist.client,'vision',False)))
    if len(json.dumps(result).encode())>12000:result={'error':'VisualResultTooLarge','detail':'Use smaller size/crop or pixels limit<=512'};attachment=None
    resolved=resolved_request(translated,result['evidence']) if 'evidence' in result else translated
    ids=sorted(evidence_ids(result.get('evidence',{})))
    if result.get('evidence'):
        p['new_evidence']+=max(1,len(ids))
    if ids:
        note='研究Job '+jid+' の追加観測：'+json.dumps({k:result[k] for k in ('evidence','box','size','mode','mean','component_count','coordinates') if k in result},ensure_ascii=False,separators=(',',':'))
        scientist.store.knowledge('observation',note,'observed','unknown',ids)
    if op in ('analyze','analyzer_create','analyzer_run','analyzer_evaluate') or request.get('operations'):p['analysis_methods']+=1
    return result,resolved,attachment


def canonical_request(request):
    value=dict(request)
    if value.get('op','observe') in ('observe','compare','analyze'):
        value={'op':'observe','phase':'before','size':[32,32],'mode':'L','output':'pixels',**value}
        value['op']='observe'
        if value['output'] in ('pixels','image'):value={'offset':0,'limit':128,**value}
        if not value.get('fresh'):value.pop('fresh',None)
        if isinstance(value.get('compare'),dict):value['compare']={'phase':'before',**value['compare']}
    return value


def run_actions(scientist,jid,payload,requests):
    ledger=payload.setdefault('action_history',[])
    for index,request in enumerate(requests):
        if not isinstance(request,dict):raise ValueError('Research action must be an object')
        if len(ledger)>=MAX_ACTIONS or time.time()-payload.get('research_started',time.time())>MAX_SECONDS:
            payload['research_limit']='actions' if len(ledger)>=MAX_ACTIONS else 'time';break
        token=f'{payload.get("research_calls",0)}:{index}'
        completed=next((r for r in ledger if r['action_id']==token and r['state'] in ('done','duplicate','error')),None)
        if completed:continue
        fingerprint=hashlib.sha256(json.dumps(canonical_request(request),sort_keys=True,separators=(',',':')).encode()).hexdigest()
        old=next((r for r in ledger if r['fingerprint']==fingerprint and r['state']=='done'),None)
        record={'action_id':token,'fingerprint':fingerprint,'request':request,'state':'running','at':time.time(),'step':payload.get('research_calls',0)}
        ledger.append(record);scientist.store.set_job(jid,'running',payload)
        if old and not request.get('fresh') and request.get('op')!='experiment':
            record.update(state='duplicate',result={'duplicate':True,'original_step':old['step'],'result':old['result']})
            payload['duplicate_requests']=payload.get('duplicate_requests',0)+1
            scientist.activity.record('RESEARCH',f'duplicate job={jid} step={record["step"]} op={request.get("op","observe")}')
            # Preserve references for the next turn, without executing the duplicate.
            if old.get('visual_index') is not None:
                history=payload['visual_history'];history.append({**history[old['visual_index']],'duplicate':True})
        else:
            try:
                result,resolved,attachment=execute(scientist,jid,payload,request,token)
                record.update(state='done',result=result,returned_at=time.time())
                if resolved is not None:
                    record['visual_index']=len(payload.setdefault('visual_history',[]))
                    payload['visual_history'].append({'request':resolved,'result':result,'native':bool(attachment),'job':jid,'step':record['step']})
                scientist.activity.record('RESEARCH',f'result job={jid} step={record["step"]} op={request.get("op","observe")} state=returned')
            except CapacityError:raise
            except InterruptedError:raise
            except (ValueError,OSError,KeyError,TypeError) as error:
                record.update(state='error',result={'error':type(error).__name__,'detail':str(error)[:240]})
                scientist.activity.record('RESEARCH',f'error job={jid} step={record["step"]} op={request.get("op","observe")} {str(error)[:100]}')
        record['finished_at']=time.time();payload.pop('stream_draft',None)
        scientist.store.set_job(jid,'running',payload)


def needs_followup(value):
    prose=json.dumps({k:value.get(k,[]) for k in ('findings','problem','reason')},ensure_ascii=False)
    return bool(re.search(r'したい|次の実験|高解像度.{0,16}(?:必要|確認|調べ|見|不足)|情報不足|判断できない|確認が必要|need.*(?:observe|inspect|compar|sample)|want to',prose,re.I))


def history_context(payload):
    history=[]
    for entry in payload.get('action_history',[])[-8:]:
        result=entry.get('result',{})
        safe={k:v for k,v in result.items() if k in ('state','error','detail','duplicate','completed','hypothesis','status','n','observed_probability','expected_probability','interval95','id','events','phase','original_step','pre_registered')}
        if isinstance(safe.get('events'),list):safe['events']=[{k:v for k,v in row.items() if k in ('id','action','change','state')} for row in safe['events']]
        history.append({k:entry[k] for k in ('action_id','step','state','request') if k in entry}|{'result':safe})
    return json.dumps({'job':payload.get('research_job'),'step':payload.get('research_calls',0),'phase':payload.get('research_phase','observation'),'hypotheses':payload.get('hypotheses',{}),'progress':progress(payload),'actions':history,'redirect':payload.get('redirect','')},ensure_ascii=False,separators=(',',':'))


def made_progress(payload):
    p=progress(payload)
    return any((p.get(k) or 0)>0 for k in ('new_evidence','hypotheses_evaluated','experiments','analysis_methods','candidates'))
