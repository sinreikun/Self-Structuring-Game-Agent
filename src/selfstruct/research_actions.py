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

INSTRUCTIONS='''研究応答JSON: {"findings":[{"id":"供給された新規ID","text":"主張","evidence":[ID]}],"actions":[],"problem":"","reason":"","wat":""}。WATは任意。actionsは最大4件、opは操作名だけで引数は別フィールド。
例 {"op":"observe","event":1,"phase":"before","size":[32,32],"mode":"RGB","output":"pixels","limit":64}。例のIDは今回の索引・許可入力へ置換する。
最初は索引のみ。保存全履歴や原画像を入力へ集積せず、自分で必要な情報を要求する。各操作の型・詳細な引数は生成用schemaと以下の視覚API説明に従う。
memory_search: collection=experience/knowledge/experiment/visual/research/note/report,query（文字列検索）またはref（既知ID）,limit=1..8,cursor,watermark。索引にref/revisionを返す。
memory_read: collection,ref,revision,offset,limit=1..1600文字。本文JSONを取得。next_offsetで続き、選択版は同じJobに最大32件保持。source_changedは正本の更新を示す。最新が必要なら再検索。staleなら新しいrevisionでoffset=0へ。異なる版を結合しない。unavailableは内容不明。
memory_note: title,body,evidenceで短い課題・計画・不明点を外部保存。更新はref/revision必須。別Jobや再起動後も検索可。メモは未検証。
research_report: title,target_goal,goal_status=unknown/hypothesis/observed,goal_evidence,findings=[{text,status:observation/hypothesis,evidence}],unknowns,next_steps。日本語の人間向け副目的。報告だけを量産しない。
observe/compare/analyze: eventまたはobservation,phase,box=[左,上,右,下],size,mode,output,operations,offset,limit。必要な場所・方法は自分で選ぶ。compareに第2参照を指定。詳細は視覚API説明。
experiment: hypothesis（既存ID/別名、必須）,action（0始まり許可入力）,duration=10..500ms,wait=10..2000ms,repeat=1..8。最大4計画/32入力。conditionは画素mean_min/mean_maxによる状態選別。無条件は省略。意味ラベルではない。完了Experienceを返す。PAUSE中は入力せずSTOP後は再送しない。
evaluate: hypothesis,change_probability=0..1。画像変化率を検証するなら実験前にfindings.actionとchange_probabilityを登録する。未登録・事後変更・条件選別・異なる入力混在・16件未満は判定不能。意味や目標達成は検証しない。
candidate: problem,evidence（取得済みID2件以上）,behavior,evaluationで持続する挙動の実装へ。根拠と評価条件を揃える。
新規findings.idは毎回提示された候補から選ぶ。同じidへ別の主張や確率を再割当しない。既存仮説を使うだけならfindings=[]でactions.hypothesisへ既存ID/別名を指定する。目的・意味が未確認ならそのまま記録する。
取得結果は同じJobへ返る。同じ過去Evidence/処理を再要求しても新情報ではない。1 Job最大10推論/24要求/600秒/画像4MiB。必要なら具体的actionsで続行、不要なら根拠・未確認事項を残して終了。'''


def allowed_evidence(payload):
    ids={r['id'] for r in payload.get('research_rows',[])}
    for h in payload.get('visual_history',[]):ids.update(evidence_ids(h.get('result',{}).get('evidence',{})))
    for h in payload.get('action_history',[]):
        for row in h.get('result',{}).get('events',[]):
            if row.get('state')=='complete':ids.add(row['id'])
    ids.update(payload.get('memory_evidence',[]))
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
    if op in ('memory_search','memory_read','memory_note','research_report'):
        from .research_memory import search,read,note,report
        result={'memory_search':search,'memory_read':read,'memory_note':note,'research_report':report}[op](scientist,jid,payload,request)
        payload['research_phase']={'memory_note':'planning','research_report':'reporting'}.get(op,'retrieval')
        label={'memory_search':'記録を検索','memory_read':'必要な記録を取得','memory_note':'課題・計画を保存','research_report':'人間向け研究結果を保存'}[op]
        scientist.activity.update(experiment=f'研究Job {jid[:8]}：{label}',next='研究対象の目的に沿う記録・観測・操作をLLMが選びます')
        if op=='memory_note' and result.get('status')=='saved':scientist.activity.update(purpose=request['title'])
        scientist.activity.record('MEMORY',f'job={jid} op={op} collection={request.get("collection","note")} ref={request.get("ref","")} state={result.get("status","index")}')
        counter={'memory_read':'memory_reads','memory_search':'memory_searches','memory_note':'notes','research_report':'reports'}[op]
        p[counter]=p.get(counter,0)+1
        if 'content' in result:result['content_sha256']=hashlib.sha256(result['content'].encode()).hexdigest()
        payload['memory_results']=payload.get('memory_results',[])+[{'action_id':token,'step':payload.get('research_calls',0),'request':request,'result':result}]
        return result,None,None
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
        if old and not request.get('fresh') and request.get('op') not in ('experiment','memory_search','memory_read','memory_note','research_report'):
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


def experiment_prompt_context(summary):
    """One factual model view; full per-plan and per-hypothesis ledgers stay stored."""
    result={k:v for k,v in summary.items() if k not in ('hypotheses','experiments','note')}
    result['experiments']=[{k:v for k,v in row.items() if k in ('id','hypothesis','state','action','requested','completed')} for row in summary.get('experiments',[])]
    result['hypotheses']=[]
    for group in summary.get('hypotheses',[]):
        item={k:v for k,v in group.items() if k in ('hypothesis','evidence','by_action')}
        item['evaluation_requirements']={k:v for k,v in group.get('evaluation_requirements',{}).items() if k!='note'}
        result['hypotheses'].append(item)
    result['note']='現在Jobのみ。入力別観測数は支持・反証ではない。未登録結果の事後転用禁止。evaluateは全Jobを再検査。'
    return result


def history_context(payload,compact=False):
    if payload.get('memory_mode')=='pull':
        recent=[]
        for entry in payload.get('action_history',[])[-2:]:
            result=entry.get('result',{});request={k:v for k,v in entry.get('request',{}).items() if k!='body' and (entry.get('request',{}).get('op')!='research_report' or k in ('op','title'))}
            view={k:v for k,v in result.items() if k in ('state','error','detail','completed','hypothesis','status','n','interval95','pre_registered','reasons','registered_target','records','collection','ref','revision','next_offset','experiment')}
            if 'events' in result:
                view['event_ids']=[r['id'] for r in result['events']]
                if not compact:view['events']=[{k:v for k,v in r.items() if k in ('id','action','change','state')} for r in result['events']]
            recent.append({'action_id':entry['action_id'],'request':request,'state':entry['state'],'result':view})
        return json.dumps({'job':payload.get('research_job'),'step':payload.get('research_calls',0),'phase':payload.get('research_phase'),'index':payload.get('memory_index',{}),'actions':recent,'redirect':payload.get('redirect','')},ensure_ascii=False,separators=(',',':'))
    history=[]
    count=2 if compact else 4
    for entry in payload.get('action_history',[])[-count:]:
        result=entry.get('result',{})
        safe={k:v for k,v in result.items() if k in ('state','error','detail','duplicate','completed','hypothesis','status','n','observed_probability','expected_probability','interval95','id','events','phase','original_step','pre_registered','required_samples','reasons','registered_target')}
        if compact:safe.pop('events',None)
        elif isinstance(safe.get('events'),list):safe['events']=[{k:v for k,v in row.items() if k in ('id','action','change','state')} for row in safe['events']]
        history.append({k:entry[k] for k in ('action_id','step','state','request') if k in entry}|{'result':safe})
    return json.dumps({'job':payload.get('research_job'),'step':payload.get('research_calls',0),'phase':payload.get('research_phase','observation'),'hypotheses':({k:v for k,v in payload.get('hypotheses',{}).items() if v in {h['id'] for h in payload.get('hypothesis_context',[])}} if compact else payload.get('hypotheses',{})),'progress':progress(payload),'actions':history,'experiment_context':experiment_prompt_context(payload.get('experiment_context',{})),'older_actions_omitted':max(0,len(payload.get('action_history',[]))-count),'redirect':payload.get('redirect','')},ensure_ascii=False,separators=(',',':'))


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
