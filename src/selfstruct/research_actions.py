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
actionsは最大4件。opには操作名だけを入れ、event等は別のJSONプロパティへ入れる。opの中にJSONやカンマ区切りコマンドを書くことは禁止。
正しい追加観測例: {"op":"observe","event":10582,"phase":"before","box":[1040,0,1600,320],"size":[128,64],"mode":"RGB","output":"pixels","limit":64}
正しい比較例: {"op":"compare","event":10582,"phase":"before","compare":{"event":10582,"phase":"after"},"size":[64,32],"output":"statistics"}
正しい実験例: {"op":"experiment","hypothesis":"h1","action":4,"duration":60,"wait":100,"repeat":3}
例のID・入力は説明用。必ず今回提示されたID・許可入力へ置き換える。
各opのフィールド:
observe: event,phase=before/after/diff,box=[左,上,右,下],size=[幅,高さ],mode=RGB/L,output=pixels/statistics/histogram/components/image,operations,offset,limit。event省略は現在画面。
compare: observeと同じ指定にcompare={event,phase,box}を追加。2つのEvidenceの同一領域を比較。
analyze: observeと同じ指定にoperations=[{op:edge/threshold/grayscale/blur/invert,...}]を追加。解析器APIもactionsから直接利用可。
experiment: hypothesis=h1または既存仮説ID（必須。先にfindingsへid/text/evidenceを定義）,action=入力番号,duration=10..500ミリ秒,wait=10..2000ミリ秒,repeat=1..8。最大4計画/32入力。condition={box,size,mode,operations,mean_min:0..255,mean_max:0..255,max_wait_steps:1..128}で画素平均範囲を満たす状態を待てる。意味ラベルは禁止。conditionは実行状態の選別条件で、観測フォーマット指定ではない。指定時はmean_min/mean_maxの少なくとも1つが必須。無条件ならcondition自体を省略する。入力結果は別observe/compareで確認する。experiment_context.hypothesesのevaluation_requirementsには事前登録の有無・同じ登録入力の標本数・不足数・判定不能の理由がある。単なるcompleted合計を統計標本数と混同しない。未登録の結果へ後から確率を付けて支持・反証してはいけない。PAUSE中は入力しない。STOP/中断は再送しない。
evaluate: hypothesis=既存の別名またはID,change_probability=0..1。画面変化確率を検証する場合は実験より先にfindings.actionとchange_probabilityを登録する。未登録や事後に変更した確率は判定不能。実験の独立した完了経験16件以上で画面変化確率のみ統計比較。少数・異なる入力混在・意味推測は判定不能。
candidate: problem, evidence=[取得済みIDを2件以上], behavior, evaluationを指定すると実装フェーズへ。根拠・必要挙動・評価条件を揃えてから要求。
findingsにactionとchange_probabilityを仮説作成時に指定すると事前登録となる（無条件の指定入力後の画面変化確率のみ）。条件付き仮説の意味・因果は未確認のまま。新規findingsのidはその推論ステップで提示された未使用ID候補から選び、experiment/evaluateで参照する。同じidを別の仮説や変更した主張に再割当しない。新しい仮説にはh2/h3等の新しいidを使う。各結果は同じJobへ返され、次の推論で使える。新しい根拠、仮説、検証、解析器、候補は進展として記録する。観測・仮説だけの完了も正当。ただし次の行動が必要なら実行せず完了としない。
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
    plan=[];batch={}
    with scientist.store.lock:
        def matching_target(kid,finding):
            claim=scientist.store.db.execute('SELECT action,probability FROM claims WHERE knowledge=?',(kid,)).fetchone()
            if 'action' in finding and 'change_probability' in finding:
                return bool(claim and claim['action']==scientist.keys[finding['action']] and claim['probability']==finding['change_probability'])
            return not claim or (('action' not in finding or claim['action']==scientist.keys[finding['action']]) and ('change_probability' not in finding or claim['probability']==finding['change_probability']))
        # Validate every reserved alias before writing any new Knowledge or claim.
        for finding in proposal['findings']:
            ids=sorted(set(finding['evidence']));ref=finding.get('id');existing=None
            if ref in batch:
                if batch[ref]!=finding:raise ValueError('Hypothesis reference cannot be rebound; choose a supplied new findings.id')
                continue
            if ref:batch[ref]=finding
            if ref in mappings:
                old=scientist.store.db.execute("SELECT id,text FROM knowledge WHERE id=? AND kind='hypothesis'",(mappings[ref],)).fetchone()
                if not old or old['text']!=finding['text'] or not matching_target(old['id'],finding):
                    raise ValueError('Hypothesis reference cannot be rebound; keep existing claims unchanged and use a supplied new findings.id')
                existing=old['id']
            else:
                for old in scientist.store.db.execute("SELECT id FROM knowledge WHERE kind='hypothesis' AND text=? AND evidence=?",(finding['text'],json.dumps(ids))):
                    if matching_target(old['id'],finding):existing=old['id'];break
            plan.append((finding,ids,ref,existing))
        for finding,ids,ref,existing in plan:
            kid=existing if existing is not None else scientist.store.knowledge('hypothesis',finding['text'],'inferred','hypothesis',ids)
            if existing is None:
                progress(payload)['new_hypotheses']+=1
                action,probability=finding.get('action'),finding.get('change_probability')
                if type(action)==int and type(probability) in (float,int):
                    latest=scientist.store.db.execute('SELECT coalesce(max(id),0) FROM events').fetchone()[0]
                    scientist.store.claim(kid,scientist.keys[action],probability,max(max(ids),latest))
                scientist.activity.record('HYPOTHESIS',f'job={jid} id={kid} pending evidence={ids}')
            else:
                evidence=json.loads(scientist.store.db.execute('SELECT evidence FROM knowledge WHERE id=?',(kid,)).fetchone()[0])
                with scientist.store.db:scientist.store.db.execute('UPDATE knowledge SET evidence=? WHERE id=?',(json.dumps(sorted(set(evidence+ids))),kid))
            if isinstance(ref,str) and 1<=len(ref)<=40:mappings[ref]=kid
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
    from .research_schema import action_schema,validate
    validate(action_schema(len(scientist.keys)),request)
    op=request.get('op','observe');p=progress(payload)
    payload['research_phase']={'experiment':'experiment','evaluate':'evaluation','analyzer_create':'analyzer_creation','analyzer_evaluate':'evaluation','analyze':'analysis'}.get(op,'observation')
    if op=='experiment':
        if not getattr(scientist,'experiments',None):raise ValueError('Player experiment executor unavailable')
        hypothesis=hypothesis_id(payload,request)
        if hypothesis is None:raise ValueError('Experiment requires an explicit hypothesis ID or findings.id reference; Host will not infer one')
        identifier=scientist.experiments.submit(jid,token,request,hypothesis)
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
    if op=='compare' and not isinstance(request.get('compare'),dict):raise ValueError('compare requires a second Evidence reference')
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
    for entry in payload.get('action_history',[])[-4:]:
        result=entry.get('result',{})
        safe={k:v for k,v in result.items() if k in ('state','error','detail','duplicate','completed','hypothesis','status','n','observed_probability','expected_probability','interval95','id','events','phase','original_step','pre_registered','required_samples','reasons','registered_target')}
        if isinstance(safe.get('events'),list):safe['events']=[{k:v for k,v in row.items() if k in ('id','action','change','state')} for row in safe['events']]
        history.append({k:entry[k] for k in ('action_id','step','state','request') if k in entry}|{'result':safe})
    return json.dumps({'job':payload.get('research_job'),'step':payload.get('research_calls',0),'phase':payload.get('research_phase','observation'),'hypotheses':payload.get('hypotheses',{}),'progress':progress(payload),'actions':history,'experiment_context':payload.get('experiment_context',{}),'older_actions_omitted':max(0,len(payload.get('action_history',[]))-4),'redirect':payload.get('redirect','')},ensure_ascii=False,separators=(',',':'))


def made_progress(payload):
    p=progress(payload)
    return any((p.get(k) or 0)>0 for k in ('new_evidence','hypotheses_evaluated','experiments','analysis_methods','candidates'))


def context_entry(entry,older=False):
    """Omit replayable payloads, never fabricate summaries or alter the journal."""
    def without_hashes(value):
        if isinstance(value,dict):return {k:without_hashes(v) for k,v in value.items() if k not in ('hash','hashes')}
        if isinstance(value,list):return [without_hashes(v) for v in value]
        return value
    item=without_hashes(entry)
    if older and isinstance(item.get('result'),dict):
        item['result']={k:v for k,v in item['result'].items() if k in ('evidence','box','size','mode','id','status')}
        item['request']={k:v for k,v in item.get('request',{}).items() if k in ('op','event','observation','phase','box','size','mode','output','offset','limit','id')}
        item['payload_omitted']='Older full result retained in this Job journal. Request exact Evidence/offset again to inspect cached results.'
    return item


def hypothesis_context(scientist,payload):
    """Keep actively referenced claims in context; an alias alone is not a memory."""
    aliases=payload.get('hypotheses',{});active=[]
    for entry in payload.get('action_history',[])[-4:]:
        ref=entry.get('request',{}).get('hypothesis')
        kid=aliases.get(ref) if isinstance(ref,str) else ref
        if type(kid)==int:active.append(kid)
    tested=[entry['hypothesis'] for entry in payload.get('experiment_context',{}).get('experiments',[]) if type(entry.get('hypothesis'))==int]
    ids=list(dict.fromkeys(tested+active+list(aliases.values())[-4:]))[:8]
    result=[]
    with scientist.store.lock:
        for kid in ids:
            row=scientist.store.db.execute("SELECT id,text,validation,evidence FROM knowledge WHERE id=? AND kind='hypothesis'",(kid,)).fetchone()
            if not row:continue
            claim=scientist.store.db.execute('SELECT action,probability,after_id FROM claims WHERE knowledge=?',(kid,)).fetchone()
            item={'id':kid,'aliases':[k for k,v in aliases.items() if v==kid],'text':row['text'][:240],
                  'text_truncated':len(row['text'])>240,'validation':row['validation'],'evidence':json.loads(row['evidence'])[-16:]}
            if claim:item['preregistered']={'action':scientist.keys.index(claim['action']),'change_probability':claim['probability'],'after_id':claim['after_id']}
            result.append(item)
    return result


def experiment_context(scientist,jid):
    """Compact factual ledger for this Job, independent of recent action history."""
    result={'scope':'current_job','experiments':[],'hypotheses':[],'plans_used':0,'plan_limit':4,'requested_inputs':0,'input_limit':32,'required_evaluation_samples':16}
    with scientist.store.lock:
        rows=scientist.store.db.execute('SELECT * FROM research_experiments WHERE job=? ORDER BY created',(jid,)).fetchall()
        for row in rows:
            request=json.loads(row['request']);ids=json.loads(row['events'])
            events=[dict(r) for r in scientist.store.db.execute('SELECT id,action,change,state FROM events WHERE id IN ('+','.join('?' for _ in ids)+') ORDER BY id',ids)] if ids else []
            complete=[r for r in events if r['state']=='complete']
            item={'id':row['id'],'hypothesis':row['hypothesis'],'state':row['state'],'action':request['action'],
                  'requested':request['repeat'],'completed':len(complete),'evidence':[r['id'] for r in complete],
                  'changed':sum(r['change']>.01 for r in complete),'unchanged':sum(r['change']<=.01 for r in complete)}
            result['experiments'].append(item);result['plans_used']+=1;result['requested_inputs']+=request['repeat']
        groups={}
        for row in rows:
            kid=row['hypothesis'];group=groups.setdefault(kid,{'hypothesis':kid,'experiment_ids':[],'evidence':[]})
            group['experiment_ids'].append(row['id']);group['evidence'].extend(json.loads(row['events']))
        for group in groups.values():
            ids=sorted(set(group['evidence']))
            complete=[dict(r) for r in scientist.store.db.execute("SELECT id,action,change FROM events WHERE state='complete' AND id IN ("+','.join('?' for _ in ids)+') ORDER BY id',ids)] if ids else []
            group['evidence']=[r['id'] for r in complete];group['completed']=len(complete)
            group['actual_inputs']=sorted({r['action'] for r in complete})
            group['changed']=sum(r['change']>.01 for r in complete);group['unchanged']=sum(r['change']<=.01 for r in complete)
            # Counts for different inputs describe observations, not pooled samples.
            group['by_action']=[{'action':scientist.keys.index(key),'input':key,
                'completed':sum(r['action']==key for r in complete),
                'changed':sum(r['action']==key and r['change']>.01 for r in complete)} for key in group['actual_inputs']]
            claim=scientist.store.db.execute('SELECT action,probability,after_id FROM claims WHERE knowledge=?',(group['hypothesis'],)).fetchone()
            reasons=[]
            if not claim:reasons.append('not_preregistered')
            conditional=any((json.loads(row['request']).get('condition') or {}).get('mean_min',0)>0 or
                            (json.loads(row['request']).get('condition') or {}).get('mean_max',255)<255
                            for row in rows if row['hypothesis']==group['hypothesis'])
            if conditional:reasons.append('conditional_selection')
            if claim and any(r['action']!=claim['action'] for r in complete):reasons.append('action_mismatch')
            eligible=[r for r in complete if claim and r['action']==claim['action'] and r['id']>claim['after_id']]
            if claim and len(eligible)<len([r for r in complete if r['action']==claim['action']]):reasons.append('before_registration')
            if len(eligible)<16:reasons.append('insufficient_samples')
            group['evaluation_requirements']={'scope':'current_job_only','preregistered':bool(claim),
                'registered_target':{'action':scientist.keys.index(claim['action']),'change_probability':claim['probability'],'after_id':claim['after_id']} if claim else None,
                'matching_post_registration_samples':len(eligible),'missing_samples':max(0,16-len(eligible)),
                'blocking_reasons':reasons,
                'note':'現在Jobの集計であり評価結果ではない。未登録の過去経験は標本へ転用できない。evaluateは同じ仮説に紐づく全Jobを再検査する。'}
            result['hypotheses'].append(group)
    result['plans_remaining']=max(0,result['plan_limit']-result['plans_used'])
    result['inputs_remaining']=max(0,result['input_limit']-result['requested_inputs'])
    result['note']='completed/changedは入力別内訳を持つ観測数で統計的な支持・反証ではない。完了経験だけを計数。未送信・中断は成功扱いしない。未登録なら新しい仮説と今後の実験が必要。異なる仮説・入力の標本は混ぜない。'
    return result
