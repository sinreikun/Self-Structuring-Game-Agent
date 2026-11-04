"""Bounded, journaled research actions; no natural-language-to-key translation."""
import hashlib
import json
import re
import time

from .store import CapacityError
from .vision import evidence_ids, resolved_request

MAX_CALLS=20
MAX_ACTIONS=24
MAX_SECONDS=600
MAX_IMAGE_BYTES=4*1024*1024

INSTRUCTIONS='''実験の登録形式はschema参照。States/samples: index.state_transitions。findings.state=trueで状態を保存。
研究応答JSON: {"findings":[{"id":"供給された新規ID","text":"主張","evidence":[ID]}],"actions":[],"problem":"","reason":"","wat":""}。WATは任意。actionsは最大4件、opは操作名だけで引数は別フィールド。
例 {"op":"observe","event":1,"phase":"before","size":[32,32],"mode":"RGB","output":"pixels","limit":64}。例のIDは今回の索引・許可入力へ置換する。
索引から必要な記録・画像を要求。型はschemaに従う。
memory_search: collection=experience/knowledge/experiment/visual/research/note/report,query（文字列検索）またはref（既知ID）,limit=1..8,cursor,watermark。索引にref/revisionを返す。
memory_read: collection,ref,revision,offset,limit=1..1600文字。本文はnext_offsetで続く。選択版は最大32件。source_changedなら正本更新、staleなら再検索しoffset=0から。異なる版を結合しない。unavailableは内容不明。
memory_note: title,body,evidenceで短い課題・計画・不明点を外部保存。更新はref/revision必須。別Jobや再起動後も検索可。メモは未検証。
目的ループ：観察→仮説ID→experiment→evaluate→research_report。loop_gapsの不足段へ進む。評価は自動添付。未達は未確認。
research_report: title,target_goal,goal_status=unknown/hypothesis/observed,goal_evidence,findings=[{text,status:observation/hypothesis,evidence}],unknowns,next_steps。日本語で報告。
observe/compare/analyze: eventは実ID（行番号ではない）。索引外はmemory_search(collection=experience)で取得。eventまたはobservation,phase,box=[左,上,右,下],size,mode,output,operations,offset,limit。必要な場所・方法は自分で選ぶ。compareに第2参照を指定。詳細は視覚API説明。
experiment: mature unevaluated hypotheses block new experiments/findings/claims without budget charge; evaluate first. hypothesis=ID/alias,action=allowed input index,duration=10..500ms,wait=10..2000ms,repeat=1..8. 4 plans/32 inputs. condition=mean_min/mean_max(optional). Review returned Experience IDs before next experiment; unknown is valid. No PAUSE or STOP resend.
evaluate: hypothesis,任意のchange_probability=0..1（省略=登録値）。登録後・同一入力・同一セッション・16標本以上が必要。未登録・事後変更・条件選別・入力混在・不足は保留。成熟未評価を先に評価。記述の意味は未検証。
candidate: problem,evidence（取得済みID2件以上）,behavior,evaluation。
新規findings.idは毎回提示された候補から選ぶ。同じidへ別の主張や確率を再割当しない。既存仮説を使うだけならfindings=[]でactions.hypothesisへ既存ID/別名を指定する。目的・意味が未確認ならそのまま記録する。
結果は同じJobへ。index.input_feedbackは実験結果の参照と配信状況。古い画像を新入力の結果と混同しない。1 Job最大10推論/24要求/600秒/画像4MiB。必要なら具体的actionsで続行、不要なら根拠・未確認事項を残して終了。'''



def model_instructions(vision, example_event=None):
    """Describe the representation this runtime can actually deliver, without forcing actions."""
    if not vision:
        return INSTRUCTIONS.replace('"event":1,', '"event":'+str(example_event)+',' if example_event is not None else '')
    example='例 {"op":"observe","event":1,"phase":"before","size":[32,32],"mode":"RGB","output":"pixels","limit":64}。例のIDは今回の索引・許可入力へ置換する。'
    native='このRuntimeは画像を直接見られます。画面の文字・UI・配置や目的を読むにはobserveのoutput="image"で画像を要求できます。pixelsは画素値の数列、statisticsは要約であり、画面そのものの画像入力ではありません。見る方法・領域・解像度は自分で選びます。\n例 {"op":"observe","event":1,"phase":"before","size":[512,512],"mode":"RGB","output":"image"}。例のID・解像度は今回の索引と目的に合わせて選ぶ。sizeの積は262144以下。'
    return INSTRUCTIONS.replace(example,native).replace('"event":1,', '"event":'+str(example_event)+',' if example_event is not None else '')


def allowed_evidence(payload):
    ids={r['id'] for r in payload.get('research_rows',[])}
    ids.update(c['frames'][0] for c in payload.get('situation_candidates',[]))
    for h in payload.get('visual_history',[]):ids.update(evidence_ids(h.get('result',{}).get('evidence',{})))
    for h in payload.get('action_history',[]):
        for row in h.get('result',{}).get('events',[]):
            if row.get('state')=='complete':ids.add(row['id'])
    ids.update(payload.get('memory_evidence',[]))
    return ids


def progress(payload):
    return payload.setdefault('progress',{'new_evidence':0,'new_hypotheses':0,'hypotheses_evaluated':0,'experiments':0,'analysis_methods':0,'candidates':0,'candidate_plans':0,'unknown_reduction':None})



def completed_feedback(payload):
    result={}
    for item in payload.get('action_history',[]):
        row=item.get('result',{})
        if item.get('state')!='done' or item.get('request',{}).get('op')!='experiment' or item.get('step',0)>=payload.get('research_calls',0):continue
        ids=[r['id'] for r in row.get('events',[]) if r.get('state')=='complete']
        if ids:result[row['experiment']]=ids
    return result


def record_feedback_statement(payload, ids, collection, ref):
    completed={i for ids in completed_feedback(payload).values() for i in ids}
    for event in set(ids)&completed:
        payload.setdefault('feedback_statements',{})[str(event)]={'collection':collection,'ref':str(ref),'step':payload.get('research_calls',0)}


def record_findings(scientist,jid,payload,value):
    from .research import parse_proposal
    proposal=parse_proposal(json.dumps(value,ensure_ascii=False),allowed_evidence(payload),scientist.keys)
    from . import transitions
    state_findings=[f for f in proposal['findings'] if f.get('state') is True]
    for f in state_findings:
        if scientist.experiments:scientist.experiments.require_evaluation(jid)
        aliases=payload.setdefault('state_aliases',{})
        prior=aliases.get(f.get('id'))
        if prior and prior['finding']!=f:raise ValueError('State reference cannot be rebound')
        if prior:sid=prior['state']
        elif 'candidate_id' in f:
            from .situation_candidates import adopt
            sid=adopt(scientist.store,payload,f)
        else:sid=transitions.register_state(scientist.store,f['text'],f['evidence'],f.get('phase','before'),f.get('state_id'))
        if f.get('id'):aliases[f['id']]={'state':sid,'finding':f}
        payload.setdefault('defined_states',[]).append(sid)
        record_feedback_statement(payload,f['evidence'],'knowledge',sid)
    proposal['findings']=[f for f in proposal['findings'] if f not in state_findings]
    mappings=payload.setdefault('hypotheses',{})
    plan=[];batch={}
    with scientist.store.lock:
        def matching_target(kid,finding):
            if 'transition' in finding:
                c=scientist.store.db.execute('SELECT * FROM transition_claims WHERE knowledge=?',(kid,)).fetchone()
                t=finding['transition']
                return bool(c and all(c[k]==t[k] for k in ('from_state','to_state','probability')) and c['action']==scientist.keys[t['action']])
            if scientist.store.db.execute('SELECT 1 FROM transition_claims WHERE knowledge=?',(kid,)).fetchone():return False
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
            if existing is None and getattr(scientist,'experiments',None):
                pending=scientist.experiments.mature_unevaluated(jid)
                if pending:
                    from .experiments import EvaluationRequired
                    error=EvaluationRequired(pending)
                    kind='claim' if 'action' in finding and 'change_probability' in finding or 'transition' in finding else 'finding'
                    record_evaluation_rejection(payload,kind,error,{'finding':finding})
                    continue
            kid=existing if existing is not None else scientist.store.knowledge('hypothesis',finding['text'],'inferred','hypothesis',ids)
            if 'transition' in finding:
                defined={r['state'] for r in transitions.representatives(scientist.store)}
                if any(finding['transition'][k] not in defined for k in ('from_state','to_state')):
                    if ref:mappings[ref]=kid
                    route_registration(scientist,jid,payload,None,'finding-'+str(kid),kid,{'claim_type':'transition',**finding['transition']})
                    continue
            if existing is None:
                progress(payload)['new_hypotheses']+=1
                if 'transition' in finding:
                    transitions.register_claim(scientist.store,kid,scientist.keys[finding['transition']['action']],finding['transition'])
                action,probability=finding.get('action'),finding.get('change_probability')
                if type(action)==int and type(probability) in (float,int):
                    latest=scientist.store.db.execute('SELECT coalesce(max(id),0) FROM events').fetchone()[0]
                    scientist.store.claim(kid,scientist.keys[action],probability,max(max(ids),latest))
                    if payload.get('preregistration_gate'):
                        payload.setdefault('registrations_after_rejections',[]).append({'hypothesis':kid,'registered_at':time.time(),'after_rejected_hypotheses':sorted(int(k) for k in payload['preregistration_gate'])})
                scientist.activity.record('HYPOTHESIS',f'job={jid} id={kid} pending evidence={ids}')
            else:
                evidence=json.loads(scientist.store.db.execute('SELECT evidence FROM knowledge WHERE id=?',(kid,)).fetchone()[0])
                with scientist.store.db:scientist.store.db.execute('UPDATE knowledge SET evidence=? WHERE id=?',(json.dumps(sorted(set(evidence+ids))),kid))
            if isinstance(ref,str) and 1<=len(ref)<=40:mappings[ref]=kid
            record_feedback_statement(payload,ids,'knowledge',kid)
    refresh_preregistration_gate(scientist,payload)
    refresh_evaluation_gate(scientist,jid,payload)
    scientist.store.set_job(jid,'running',payload)



def record_evaluation_rejection(payload,kind,error,request):
    gate=payload.setdefault('evaluation_gate',{'rejections':{},'events':[]})
    gate['rejections'][kind]=gate['rejections'].get(kind,0)+1
    gate['events'].append({'kind':kind,'at':time.time(),'step':payload.get('research_calls',0),
                           'request':request,'pending':error.pending,'reasons':error.reasons,'evaluated_after':[]})
    payload['mature_unevaluated']=error.pending


def refresh_evaluation_gate(scientist,jid,payload):
    if not getattr(scientist,'experiments',None):return
    payload['mature_unevaluated']=scientist.experiments.mature_unevaluated(jid)
    with scientist.store.lock:
        for event in payload.get('evaluation_gate',{}).get('events',[]):
            event['evaluated_after']=[dict(r) for item in event['pending'] for r in scientist.store.db.execute(
                'SELECT id,hypothesis,created FROM research_evaluations WHERE job=? AND hypothesis=? AND created>=?',
                (jid,item['hypothesis'],event['at']))]


def refresh_preregistration_gate(scientist,payload):
    with scientist.store.lock:
        for kid,entry in payload.get('preregistration_gate',{}).items():
            claim=scientist.store.db.execute('SELECT action,probability,after_id FROM claims WHERE knowledge=?',(int(kid),)).fetchone()
            if claim and not entry.get('registered_after_rejection'):
                entry.update(registered_after_rejection=True,registration_observed_at=time.time(),claim=dict(claim))


def hypothesis_id(payload,request):
    value=request.get('hypothesis')
    if isinstance(value,str):
        if value not in payload.get('hypotheses',{}):raise ValueError('Unknown hypothesis reference: '+repr(request.get('hypothesis')))
        value=payload['hypotheses'][value]
    if value is not None and type(value)!=int:raise ValueError('Unknown hypothesis reference: '+repr(request.get('hypothesis')))
    return value


def procedure_view(scientist,payload,route):
    claim=route.get('claim') or {}
    kind=claim.get('claim_type')
    fields=['probability']+(['from_state','to_state'] if kind=='transition' else [])
    missing=[k for k in fields if k not in claim]
    stage=route.get('stage') or ('claim_type' if not route.get('type_confirmed') else (missing[0] if missing else 'registration'))
    form={'op':'registration','route':route['id'],'decision':['complete','withdraw'],
          'action':route['action'],'submitted':dict(route.get('submitted_values',claim)),
          'required':['claim_type'] if stage=='claim_type' else missing[:1]}
    if stage=='claim_type':form['claim_type']=kind or ['change_rate','transition']
    if route.get('field_error'):form['required']=[route['field_error']['field']]
    if stage=='state_definition':
        form['required']=[route['invalid_field']]
        form['state_definition_fields']=['state=true','text','evidence','phase']
    result={'route':route['id'],'state':'registration_required','stage':stage,
            'hypothesis':route['hypothesis'],'original_request':route.get('request'),
            'form':form,'submitted_claim':dict(claim),
            'next':'Confirm claim_type, then fill only the next required field; complete resumes experiment. withdraw cancels.'}
    if route.get('field_error'):result['field_error']=route['field_error']
    if kind=='transition':result.update(candidate_frames=route['candidates'],existing_states=route['states'])
    return result


def route_registration(scientist,jid,payload,request,token,hypothesis,claim=None):
    routes=payload.setdefault('registration_routes',{})
    route=next((r for r in routes.values() if r['status']=='pending' and r['hypothesis']==hypothesis and r.get('request')==request),None)
    if route is None and request:
        route=next((r for r in routes.values() if r['status']=='pending' and r['hypothesis']==hypothesis and r.get('request') is None),None)
        if route:route.update(request=request,token=token)
    if route is None:
        rid='registration-'+str(len(routes)+1)
        candidates=[]
        for event in sorted(allowed_evidence(payload),reverse=True)[:8]:
            for phase in ('before','after'):
                try:scientist.store.observed_image(event,phase)
                except (ValueError,OSError):continue
                candidates.append({'event':event,'phase':phase})
        states=[dict(r) for r in scientist.store.db.execute("SELECT id,text,evidence FROM knowledge WHERE kind='state'")]
        route={'id':rid,'hypothesis':hypothesis,'request':request,'token':token,'action':request['action'] if request else claim['action'],
               'status':'pending','created':time.time(),'candidates':candidates,'states':states}
        routes[rid]=route
        scientist.store.pin([r['event'] for r in candidates],'registration-'+jid+'-'+rid)
    if claim is not None:
        route['claim']=dict(claim)
        route['submitted_values']=dict(claim)
        if 'from_state' in claim or 'to_state' in claim:route['claim']['claim_type']='transition'
    if claim is not None:
        route['unvalidated_claim']=dict(route['claim'])
        route['claim']={k:v for k,v in route['claim'].items() if k not in ('from_state','to_state')}
    route.setdefault('stage','claim_type')
    route['presentations']=route.get('presentations',0)+1
    scientist.store.set_job(jid,'running',payload)
    return procedure_view(scientist,payload,route)


def complete_registration(scientist,jid,payload,request,token):
    from . import transitions
    route=payload.get('registration_routes',{}).get(request['route'])
    if not route:raise ValueError('Unknown registration route')
    if route['status']!='pending':return {'state':route['status'],'route':route['id']},None,None
    if request['decision']=='withdraw':
        route.update(status='withdrawn',finished=time.time())
        scientist.store.unpin('registration-'+jid+'-'+route['id'])
        return {'state':'withdrawn','route':route['id'],'experiment_executed':False},None,None
    scientist.experiments.require_evaluation(jid)
    import math
    claim=dict(route.get('claim') or {})
    supplied={k:request[k] for k in ('claim_type','probability','from_state','to_state') if k in request}
    supplied={**route.pop('unvalidated_claim',{}),**supplied}
    route.setdefault('submitted_values',{}).update(supplied)
    route.pop('field_error',None)
    def pending(stage,field=None,reason=None):
        route.update(claim=claim,stage=stage)
        if field:route['invalid_field']=field
        if reason:
            route['field_error']={'field':field,'reason':reason}
            route['field_returns']=route.get('field_returns',0)+1
        route['presentations']=route.get('presentations',0)+1
        scientist.store.set_job(jid,'running',payload)
        return procedure_view(scientist,payload,route),None,None
    mixed=supplied.get('claim_type')=='change_rate' and any(k in supplied for k in ('from_state','to_state'))
    if mixed:
        route['unvalidated_claim']={**supplied,'claim_type':'transition'}
        claim['claim_type']='transition'
        route['type_confirmed']=False
        return pending('claim_type','claim_type','State fields require transition; confirm type or withdraw')
    if 'claim_type' in request:
        kind=supplied['claim_type']
        if kind not in ('transition','change_rate'):return pending('claim_type','claim_type','Unknown claim type')
        if kind=='change_rate' and any(k in claim for k in ('from_state','to_state')):
            return pending('claim_type','claim_type','State fields require transition; confirm transition or withdraw')
        claim['claim_type']=kind;route['type_confirmed']=True
    if any(k in supplied for k in ('from_state','to_state')) and claim.get('claim_type')!='transition':
        route['unvalidated_claim']={**supplied,'claim_type':'transition'}
        claim['claim_type']='transition';route['type_confirmed']=False
        return pending('claim_type','claim_type','Confirm transition or withdraw')
    if not route.get('type_confirmed'):
        route['unvalidated_claim']=supplied
        return pending('claim_type')
    kind=claim['claim_type']
    # Validate each submitted value now, retaining other accepted fields.
    errors=[]
    for field,value in supplied.items():
        if field=='claim_type':continue
        if field=='probability':
            if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1:
                errors.append(('probability',field,'Expected a number from 0 to 1'));continue
        elif field in ('from_state','to_state'):
            defined={r['state'] for r in transitions.representatives(scientist.store)}
            if isinstance(value,dict):
                try:
                    from .research_schema import STATE_CHOICE,validate
                    validate(STATE_CHOICE,value)
                    if value.get('state') is not True or not value.get('evidence') or not value.get('text'):raise ValueError('State definition requires model text and evidence')
                    if any({'event':eid,'phase':value['phase']} not in route['candidates'] for eid in value['evidence']):raise ValueError('Choose a presented evidence frame')
                    sid=transitions.register_state(scientist.store,value['text'],value['evidence'],value['phase'])
                except (ValueError,KeyError,TypeError) as error:
                    errors.append(('state_definition',field,str(error)));continue
                value=sid
                route.setdefault('defined_states',[]).append(sid)
                payload.setdefault('defined_states',[]).append(sid)
                route['states']=[dict(r) for r in scientist.store.db.execute("SELECT id,text,evidence FROM knowledge WHERE kind='state'")]
            elif type(value)!=int or value not in defined:
                errors.append(('state_definition',field,'Undefined state; choose an existing state or define it'));continue
        claim[field]=value
    if errors:
        route['field_returns']=route.get('field_returns',0)+len(errors)-1
        route['field_errors']=[{'field':f,'reason':r} for _,f,r in errors]
        stage,field,reason=errors[0]
        return pending(stage,field,reason)
    for field in ['probability']+(['from_state','to_state'] if kind=='transition' else []):
        if field not in claim:return pending(field)
    if kind=='transition':
        transitions.register_claim(scientist.store,route['hypothesis'],scientist.keys[route['action']],{**claim,'action':route['action']})
    else:
        latest=scientist.store.db.execute('SELECT coalesce(max(id),0) FROM events').fetchone()[0]
        scientist.store.claim(route['hypothesis'],scientist.keys[route['action']],claim['probability'],latest)
    route.update(status='registered',claim=claim,finished=time.time())
    scientist.store.set_job(jid,'running',payload)
    scientist.store.unpin('registration-'+jid+'-'+route['id'])
    if route.get('request'):
        result=execute(scientist,jid,payload,route['request'],route['token']+'-registered')
        route['experiment_result']=result[0]
        # Keep resumed input feedback in the same experiment journal format.
        payload.setdefault('action_history',[]).append({'action_id':route['token']+'-registered','fingerprint':'resumed-'+route['id'],
            'request':route['request'],'state':'done','step':payload.get('research_calls',0),'result':result[0]})
        return result
    return {'state':'registered','route':route['id'],'hypothesis':route['hypothesis']},None,None


def execute(scientist,jid,payload,request,token):
    if not isinstance(request,dict):raise ValueError('Research action must be an object')
    if len(json.dumps(request).encode())>12000:raise ValueError('Research action too large')
    from .research_schema import action_schema,validate
    if request.get('op')=='registration':
        # Keep the envelope strict; field errors belong to the persistent form.
        envelope={k:v for k,v in request.items() if k not in ('probability','from_state','to_state','claim_type')}
        validate(action_schema(len(scientist.keys)),envelope)
    elif request.get('op')=='experiment':validate(action_schema(len(scientist.keys),experiment_required=False),{k:v for k,v in request.items() if k!='claim'})
    else:validate(action_schema(len(scientist.keys)),request)
    op=request.get('op','observe');p=progress(payload)
    if op=='registration':return complete_registration(scientist,jid,payload,request,token)
    if op in ('memory_search','memory_read','memory_note','research_report'):
        from .research_memory import search,read,note,report
        result={'memory_search':search,'memory_read':read,'memory_note':note,'research_report':report}[op](scientist,jid,payload,request)
        if op=='memory_note' and result.get('status')=='saved':record_feedback_statement(payload,request.get('evidence',[]),'note',result['ref'])
        if op=='research_report':record_feedback_statement(payload,request.get('goal_evidence',[])+[i for f in request.get('findings',[]) for i in f.get('evidence',[])],'report',result['ref'])
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
        if getattr(scientist,'experiments',None):scientist.experiments.require_evaluation(jid)
        try:hypothesis=hypothesis_id(payload,request)
        except ValueError as error:
            return {'state':'claim_required','fields':[{'field':'hypothesis','reason':str(error)}]},None,None
        if not getattr(scientist,'experiments',None):raise ValueError('Player experiment executor unavailable')
        discussed={int(i) for i in payload.get('feedback_statements',{})}
        pending={plan:ids for plan,ids in completed_feedback(payload).items() if not set(ids)&discussed}
        if pending:
            error=ValueError('Review completed input results before another experiment. Cite their actual event IDs in findings, a memory_note or research_report; unknown is valid. Observation methods remain your choice.')
            error.constraints={'completed_results_without_statement':pending,'required':'Record what is known or unknown with result Evidence IDs. Choose observe/compare if needed; old images are not new input results.'}
            raise error
        from .inline_claims import accept
        receipt=accept(scientist,jid,payload,request,token,hypothesis)
        if receipt and receipt.get('state')=='claim_required':return receipt,None,None
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
        if receipt:result={**result,'registration':receipt}
        if result.get('completed'):p['experiments']+=1;p['new_evidence']+=result['completed']
        payload['research_phase']='evaluation'
        return result,None,None
    if op=='evaluate':
        if not getattr(scientist,'experiments',None):raise ValueError('Experiment evaluator unavailable')
        kid=hypothesis_id(payload,request)
        context=experiment_context(scientist,jid)
        eligible=sorted({r['hypothesis'] for r in context['experiments'] if r['completed']>0 and r['hypothesis'] is not None})
        if kid not in eligible:
            error=ValueError('evaluate requires completed experiment evidence for this hypothesis in this Job; use the returned hypothesis IDs, not a new finding.')
            error.constraints={'evaluation_hypotheses':eligible,'aliases':{k:v for k,v in payload.get('hypotheses',{}).items() if v in eligible}}
            raise error
        result=scientist.experiments.evaluate(kid,request.get('change_probability'),job=jid)
        p['hypotheses_evaluated']+=1
        if result['status'] in ('supported','refuted') and result['status']!=result['previous_status']:
            p['hypotheses_updated']=p.get('hypotheses_updated',0)+1
        return result,None,None
    if op=='candidate':
        ids=request.get('evidence')
        if not isinstance(ids,list) or len(set(ids))<2 or any(type(i)!=int or i not in allowed_evidence(payload) for i in ids):raise ValueError('Implementation requires at least 2 inspected Evidence IDs')
        if any(not isinstance(request.get(k),str) or not 1<=len(request[k])<=1000 for k in ('problem','behavior','evaluation')):raise ValueError('Implementation requires problem, behavior and evaluation')
        payload['implementation_plan']={k:request[k] for k in ('problem','evidence','behavior','evaluation')};payload['research_phase']='implementation';p['candidate_plans']+=1
        return {'phase':'implementation','plan':payload['implementation_plan']},None,None
    # Bind visual lookups to real references exposed to this Job. Searching the
    # archive explicitly grants old IDs; never remap an ordinal to a seed event.
    available=allowed_evidence(payload)
    for ref in (request,request.get('compare') or {}):
        event=ref.get('event')
        if event is not None and event not in available:
            error=ValueError('Unresolved Experience ID; event is a real ID, not a row number. Use an available ID or memory_search collection=experience first.')
            error.constraints={'requested_event':event,'available_event_ids':sorted(available),
                               'lookup':{'op':'memory_search','collection':'experience','ref':str(event)}}
            raise error
    translated=dict(request)
    if op=='compare' and not isinstance(request.get('compare'),dict):raise ValueError('compare requires a second Evidence reference')
    if op in ('compare','analyze'):translated['op']='observe'
    if translated.get('op','observe')=='observe' and translated.get('output','pixels') in ('pixels','image'):translated={'limit':128,**translated}
    scientist.activity.update(experiment=f'研究Job {jid[:8]}：{op} Experience {request.get("event","現在")} {request.get("phase","before")} 領域 {request.get("box","全体")} 解像度 {request.get("size","既定")}',next='取得結果を同じ研究Jobへ返して再推論')
    result,attachment=scientist.visual.handle(translated,pin='research-job-'+jid,native=bool(getattr(scientist.client,'vision',False)),allowed=available)
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


def action_budget_used(payload):
    """Bill the full journal except experiment refusals from gates A and B."""
    def exempt(record):
        result=record.get('result',{})
        if record.get('request',{}).get('op')=='experiment' and result.get('state') in ('registration_required','claim_required'):return True
        return (record.get('request',{}).get('op')=='experiment'
                and record.get('state')=='error' and result.get('state')=='rejected'
                and any(reason.get('code') in ('not_preregistered','evaluation_required')
                        for reason in result.get('reasons',[])))
    return sum(not exempt(record) for record in payload.get('action_history',[]))


def run_actions(scientist,jid,payload,requests):
    ledger=payload.setdefault('action_history',[])
    for index,request in enumerate(requests):
        if not isinstance(request,dict):raise ValueError('Research action must be an object')
        if action_budget_used(payload)>=MAX_ACTIONS or time.time()-payload.get('research_started',time.time())>MAX_SECONDS:
            payload['research_limit']='actions' if action_budget_used(payload)>=MAX_ACTIONS else 'time';break
        token=f'{payload.get("research_calls",0)}:{index}'
        completed=next((r for r in ledger if r['action_id']==token and r['state'] in ('done','duplicate','error')),None)
        if completed:continue
        fingerprint=hashlib.sha256(json.dumps(canonical_request(request),sort_keys=True,separators=(',',':')).encode()).hexdigest()
        old=next((r for r in ledger if r['fingerprint']==fingerprint and r['state']=='done'),None)
        record={'action_id':token,'fingerprint':fingerprint,'request':request,'state':'running','at':time.time(),'step':payload.get('research_calls',0)}
        ledger.append(record);scientist.store.set_job(jid,'running',payload)
        if old and not request.get('fresh') and not request.get('resend') and request.get('op') not in ('experiment','registration','memory_search','memory_read','memory_note','research_report'):
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
                if getattr(error,'pending',None):
                    record['result'].update(state='rejected',pending=error.pending,reasons=error.reasons)
                    record_evaluation_rejection(payload,'experiment',error,request)
                elif getattr(error,'reasons',None):
                    record['result'].update(state='rejected',hypothesis=error.hypothesis,reasons=error.reasons)
                    gate=payload.setdefault('preregistration_gate',{}).setdefault(str(error.hypothesis),{'rejections':0,'registered_after_rejection':False})
                    gate['rejections']+=1
                    gate.setdefault('first_rejected_at',time.time())
                    gate['last_rejected_at']=time.time()
                if getattr(error,'constraints',None):record['result']['constraints']=error.constraints
                scientist.activity.record('RESEARCH',f'error job={jid} step={record["step"]} op={request.get("op","observe")} {str(error)[:100]}')
        refresh_preregistration_gate(scientist,payload)
        refresh_evaluation_gate(scientist,jid,payload)
        record['finished_at']=time.time();payload.pop('stream_draft',None)
        scientist.store.set_job(jid,'running',payload)


def needs_followup(value):
    prose=json.dumps({k:value.get(k,[]) for k in ('findings','problem','reason')},ensure_ascii=False)
    return bool(re.search(r'したい|次の実験|高解像度.{0,16}(?:必要|確認|調べ|見|不足)|情報不足|判断できない|確認が必要|need.*(?:observe|inspect|compar|sample)|want to',prose,re.I))



def input_feedback(payload, current_views=()):
    """Small, persistent result references; delivered views are not conclusions."""
    previous={k:set() for k in ('before','after','comparison')};current={k:set() for k in previous}
    def collect(view, target):
        if not (view.get('image_attached') or view.get('payload_fields')):return
        def source(ref):
            if not isinstance(ref,dict):return
            if type(ref.get('event'))==int:
                phase=ref.get('phase','before')
                target['comparison' if phase=='diff' else phase].add(ref['event'])
            for side in ('left','right'):
                item=ref.get(side)
                if isinstance(item,dict) and type(item.get('event'))==int:target['comparison'].add(item['event'])
        source(view.get('evidence',{}))
    for step in payload.get('research_steps',[]):
        if step.get('finished_at'):
            for view in step.get('delivered_visual_evidence',[]):collect(view,previous)
    for view in current_views:collect(view,current)
    result=[]
    for plan in payload.get('experiment_context',{}).get('experiments',[]):
        ids=plan.get('evidence',[])
        if plan.get('state') not in ('complete','interrupted','expired') or not ids:continue
        result.append({'experiment':plan['id'],'hypothesis':plan['hypothesis'],'action':plan['action'],'state':plan['state'],'event_ids':ids,
                       'before_delivered':sorted(set(ids)&(previous['before']|current['before'])),
                       'after_delivered':sorted(set(ids)&(previous['after']|current['after'])),
                       'comparison_delivered':sorted(set(ids)&(previous['comparison']|current['comparison'])),
                       'before_in_this_input':sorted(set(ids)&current['before']),
                       'after_in_this_input':sorted(set(ids)&current['after']),
                       'discussed_event_ids':sorted(set(ids)&{int(i) for i in payload.get('feedback_statements',{})})})
    return result


def experiment_prompt_context(summary):
    """One factual model view; full per-plan and per-hypothesis ledgers stay stored."""
    result={k:v for k,v in summary.items() if k not in ('hypotheses','experiments','note')}
    result['experiments']=[{k:v for k,v in row.items() if k in ('id','hypothesis','state','action','requested','completed')} for row in summary.get('experiments',[])]
    result['hypotheses']=[]
    for group in summary.get('hypotheses',[]):
        item={k:v for k,v in group.items() if k in ('hypothesis','evidence','by_action')}
        item['evaluation_requirements']={k:v for k,v in group.get('evaluation_requirements',{}).items() if k!='note'}
        result['hypotheses'].append(item)
    result['note']='現在Jobのみ。入力別観測数は支持・反証ではない。未登録結果の事後転用禁止。evaluateは現在Jobを検査。'
    return result


# Model views are disposable. The payload remains the complete audit journal.
HISTORY_VIEW_CHARS = 6000


def rejection_view(gate):
    events=gate.get('events',[])
    latest={}
    for event in events:
        key=json.dumps({'pending':event.get('pending'), 'reasons':event.get('reasons')},sort_keys=True,ensure_ascii=False)
        group=latest.pop(key,{'count':0})
        latest[key]=group
        latest[key].update({k:v for k,v in event.items() if k in ('kind','step','pending','reasons')})
        latest[key]['count']+=1
    return {'rejections':gate.get('rejections',{}),'latest':list(latest.values())[-1:]}


def model_history_payload(payload):
    # Select latest success and latest gate rejection separately; repeated errors
    # must not displace the successful action that produced the evidence.
    view=dict(payload)
    ledger=payload.get('action_history',[])
    selected=[]
    for entry in reversed(ledger):
        if entry.get('state')=='done':selected.append(entry);break
    errors={}
    for entry in ledger:
        result=entry.get('result',{})
        if result.get('state')=='rejected':
            key='B' if any(r.get('code')=='evaluation_required' for r in result.get('reasons',[])) else 'A'
            if key not in errors:errors[key]=[entry,0]
            errors[key]=[entry,errors[key][1]+1]
    for entry,count in list(errors.values())[-2:]:
        item=dict(entry);item['result']=dict(entry['result'],repeated=count)
        selected.append(item)
    for entry in ledger[-2:]:
        if not any(e.get('action_id')==entry.get('action_id') for e in selected):selected.append(entry)
    view['action_history']=selected[-5:]
    view['evaluation_gate']=rejection_view(payload.get('evaluation_gate',{}))
    # Compute from the full ledger, not the selected model view.
    view['_loop_gaps']=loop_gaps(payload)
    view['_progress']=progress(payload)
    return view


def history_context(payload,compact=False,max_chars=HISTORY_VIEW_CHARS):
    view=model_history_payload(payload)
    result=json.loads(_history_context(view,compact))
    result['inline_claim_receipts']=payload.get('inline_claim_receipts',[])
    result['loop_gaps']=view['_loop_gaps']
    result['progress']=view['_progress']
    result['latest_success']=[json.loads(_history_context(dict(view,action_history=[entry]),True))['actions'][-1] for entry in view['action_history'] if entry.get('state')=='done'][-1:]
    # Latest gate A guidance is separate from the action tail.
    rejected=[e for e in view['action_history'] if e.get('result',{}).get('state')=='rejected']
    result['latest_action_rejection']=[{k:e.get(k) for k in ('request','result')} for e in rejected if not any(r.get('code')=='evaluation_required' for r in e.get('result',{}).get('reasons',[]))][-1:]
    encode=lambda:json.dumps(result,ensure_ascii=False,separators=(',',':'))
    for key in ('actions','index','hypotheses'):
        if len(encode())<=max_chars:break
        result[key]=[] if key=='actions' else {}
    result['older_actions_omitted']=max(0,len(payload.get('action_history',[]))-len(result.get('actions',[])))
    return encode()


def _history_context(payload,compact=False):
    if payload.get('memory_mode')=='pull':
        recent=[]
        for entry in payload.get('action_history',[])[-2:]:
            result=entry.get('result',{});request={k:v for k,v in entry.get('request',{}).items() if k!='body' and (entry.get('request',{}).get('op')!='research_report' or k in ('op','title'))}
            view={k:v for k,v in result.items() if k in ('state','error','detail','constraints','completed','hypothesis','status','n','interval95','pre_registered','reasons','registered_target','records','collection','ref','revision','next_offset','experiment')}
            if 'events' in result:
                view['event_ids']=[r['id'] for r in result['events']]
                if not compact:view['events']=[{k:v for k,v in r.items() if k in ('id','action','change','state')} for r in result['events']]
            recent.append({'action_id':entry['action_id'],'request':request,'state':entry['state'],'result':view})
        return json.dumps({'job':payload.get('research_job'),'step':payload.get('research_calls',0),'phase':payload.get('research_phase'),'index':payload.get('memory_index',{}),'actions':recent,'experiment_context':experiment_prompt_context(payload.get('experiment_context',{})),'evaluation_gate':payload.get('evaluation_gate',{}),'loop_gaps':loop_gaps(payload),'redirect':payload.get('redirect','')},ensure_ascii=False,separators=(',',':'))
    history=[]
    count=2 if compact else 4
    for entry in payload.get('action_history',[])[-count:]:
        result=entry.get('result',{})
        safe={k:v for k,v in result.items() if k in ('state','error','detail','constraints','duplicate','completed','hypothesis','status','n','observed_probability','expected_probability','interval95','id','events','phase','original_step','pre_registered','required_samples','reasons','registered_target','repeated')}
        if compact:safe.pop('events',None)
        elif isinstance(safe.get('events'),list):safe['events']=[{k:v for k,v in row.items() if k in ('id','action','change','state')} for row in safe['events']]
        request={k:v for k,v in entry.get('request',{}).items() if k!='body'}
        history.append({k:entry[k] for k in ('action_id','step','state') if k in entry}|{'request':request,'result':safe})
    return json.dumps({'job':payload.get('research_job'),'step':payload.get('research_calls',0),'phase':payload.get('research_phase','observation'),'hypotheses':({k:v for k,v in payload.get('hypotheses',{}).items() if v in {h['id'] for h in payload.get('hypothesis_context',[])}} if compact else payload.get('hypotheses',{})),'progress':progress(payload),'actions':history,'experiment_context':experiment_prompt_context(payload.get('experiment_context',{})),'older_actions_omitted':max(0,len(payload.get('action_history',[]))-count),'evaluation_gate':payload.get('evaluation_gate',{}),'loop_gaps':loop_gaps(payload),'redirect':payload.get('redirect','')},ensure_ascii=False,separators=(',',':'))


def made_progress(payload):
    p=progress(payload)
    if not ((p.get('experiments') or 0)>0 and (p.get('hypotheses_evaluated') or 0)>0):return False
    history=payload.get('action_history',[])
    experiments=[a['result'] for a in history if a.get('state')=='done' and a.get('request',{}).get('op')=='experiment' and a.get('result',{}).get('hypothesis') is not None and a.get('result',{}).get('completed',0)>0]
    evaluations=[a['result'] for a in history if a.get('state')=='done' and a.get('request',{}).get('op')=='evaluate']
    return any(e.get('hypothesis')==r.get('hypothesis') and r.get('experiment') in e.get('experiments',[]) and
               bool(e.get('evidence')) and {x['id'] for x in r.get('events',[]) if x.get('state')=='complete'}<=set(e['evidence'])
               for r in experiments for e in evaluations)


def loop_gaps(payload):
    """Procedural reminders only: never choose observations, keys or claims."""
    history=payload.get('action_history',[])
    experiments=[a['result'] for a in history if a.get('state')=='done' and a.get('request',{}).get('op')=='experiment' and a.get('result',{}).get('hypothesis') is not None and a.get('result',{}).get('completed',0)>0]
    evaluations=[a['result'] for a in history if a.get('state')=='done' and a.get('request',{}).get('op')=='evaluate']
    pending=[r['hypothesis'] for r in experiments if not any(e.get('hypothesis')==r['hypothesis'] and set(x['id'] for x in r.get('events',[]) if x.get('state')=='complete')<=set(e.get('evidence',[])) for e in evaluations)]
    gaps=[]
    if payload.get('mature_unevaluated'):gaps.append('成熟した未評価仮説（evaluateが未実行）：'+str(payload['mature_unevaluated'])+'。先にevaluate。全件の評価記録後に新規実験・仮説・claimの制限解除。')
    missing=[int(k) for k,v in payload.get('preregistration_gate',{}).items() if not v.get('registered_after_rejection')]
    if missing:gaps.append('事前登録不足で実験拒否：仮説ID '+str(sorted(missing))+'。findingsにactionとchange_probabilityを登録してから再要求する。既存IDに追加できない場合は新しいfindings.idで登録する。')
    if not experiments:gaps.append('仮説IDに紐付く実機experimentが未完了。観測に基づき仮説と入力計画を自分で選ぶ。')
    if pending:gaps.append('実験結果のevaluateが未実行：仮説ID '+str(sorted(set(pending)))+'。未登録・標本不足は保留として理由を保存し、支持・反証を作らない。')
    if evaluations and not any(a.get('state')=='done' and a.get('request',{}).get('op')=='research_report' and a.get('result',{}).get('evaluation_ids') for a in history):
        gaps.append('評価を含むresearch_reportが未生成。評価結果と未確認事項を報告する。')
    return gaps


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
    from .context_snapshot import snapshot, scientist_view
    view = snapshot(scientist.store, jid)
    try:
        return _experiment_context(scientist_view(scientist, view), jid)
    finally:
        view.db.close()


def _experiment_context(scientist,jid):
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
                'note':'現在Jobの集計であり評価結果ではない。未登録の過去経験は標本へ転用できない。evaluateは現在Jobの同じ仮説に紐づく実験だけを検査する。'}
            transition=scientist.store.db.execute('SELECT * FROM transition_claims WHERE knowledge=?',(group['hypothesis'],)).fetchone()
            if transition and getattr(scientist,'experiments',None):
                selected,conditional,_=scientist.experiments.evaluation_samples(group['hypothesis'],jid)
                group['claim_type']='transition'
                group['evaluation_requirements']={'scope':'current_job_only','preregistered':True,'registered_target':dict(transition),'matching_post_registration_samples':len(selected),'missing_samples':max(0,16-len(selected)),'from_state_absent_or_unknown':not selected}
            result['hypotheses'].append(group)
    from .transitions import context as transition_context
    with scientist.store.lock:
        result['state_transitions']=transition_context(scientist.store,getattr(scientist,'experiments',None),jid)
    result['plans_remaining']=max(0,result['plan_limit']-result['plans_used'])
    result['inputs_remaining']=max(0,result['input_limit']-result['requested_inputs'])
    result['note']='completed/changedは入力別内訳を持つ観測数で統計的な支持・反証ではない。完了経験だけを計数。未送信・中断は成功扱いしない。未登録なら新しい仮説と今後の実験が必要。異なる仮説・入力の標本は混ぜない。'
    return result
