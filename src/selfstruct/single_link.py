"""Single unfinished link ownership. Host stores references and facts only."""
import json
import time


def key(link):
    return ('hypothesis:'+str(link['hypothesis_id']) if 'hypothesis_id' in link else 'situation_candidate:'+link['candidate_id'])


def assign(scientist, payload):
    if 'assigned_link' not in payload:
        from .job_agenda import calculate
        payload['job_agenda']=calculate(scientist,payload)
        payload['assigned_link']=dict(payload['job_agenda'][0]) if payload['job_agenda'] else None
        payload['job_mode']='single_link' if payload['assigned_link'] else 'discovery'
    link=payload['assigned_link']
    if link and 'hypothesis_id' in link:
        payload.setdefault('hypotheses',{}).setdefault('assigned',link['hypothesis_id'])
    payload.setdefault('image_delivery_totals',{'count':0,'bytes':0})
    return link


def context(scientist, payload):
    link=payload.get('assigned_link')
    if not link:return None
    store=scientist.store
    result={'assignment':link,'baseline':[]}
    with store.lock:
        kid=link.get('hypothesis_id')
        if kid is not None:
            row=store.db.execute('SELECT id,text,evidence,validation FROM knowledge WHERE id=?',(kid,)).fetchone()
            result['hypothesis']=dict(row) if row else None
            for name,table in [('claim','claims'),('transition_claim','transition_claims')]:
                row=store.db.execute('SELECT * FROM '+table+' WHERE knowledge=?',(kid,)).fetchone()
                result[name]=dict(row) if row else None
            experiments=getattr(scientist,'experiments',None)
            samples,_,_=experiments.evaluation_samples(kid) if experiments else ([],None,None)
            result.update(samples=samples,missing_samples=max(0,16-len(samples)))
            state_ids=[result['transition_claim'][k] for k in ('from_state','to_state')] if result['transition_claim'] else []
        else:
            candidate=next((c for c in payload.get('situation_candidates',[]) if c['id']==link['candidate_id']),None)
            result['candidate']=candidate
            sid=payload.get('adopted_situation_candidates',{}).get(link['candidate_id'])
            state_ids=[sid] if sid else []
        result['states']=[]
        for sid in dict.fromkeys(state_ids):
            row=store.db.execute('SELECT id,text,evidence,validation FROM knowledge WHERE id=?',(sid,)).fetchone()
            if row:result['states'].append(dict(row))
            result['baseline'].extend(dict(r) for r in store.db.execute('SELECT id,state,frames FROM state_baselines WHERE state=? ORDER BY id DESC LIMIT 1',(sid,)))
    evidence=set()
    for record in [result.get('hypothesis'),*result.get('states',[])]:
        if record:evidence.update(json.loads(record['evidence']))
    evidence.update(r['id'] for r in result.get('samples',[]))
    evidence.update((result.get('candidate') or {}).get('frames',[]))
    result['evidence_ids']=sorted(evidence)
    payload['memory_evidence']=sorted(set(payload.get('memory_evidence',[]))|evidence)
    payload['link_context']=result
    return result


def prompt_view(facts):
    """Bound model display only; full evidence and sample ledgers remain available."""
    import copy
    view=copy.deepcopy(facts)
    def recent(record, field):
        values=record.get(field,[])
        if isinstance(values,str):values=json.loads(values)
        record[field+'_count']=len(values)
        record[field]=values[-3:]
    for record in [view.get('hypothesis'),*view.get('states',[])]:
        if record:recent(record,'evidence')
    if view.get('candidate'):recent(view['candidate'],'frames')
    for record in view.get('baseline',[]):recent(record,'frames')
    recent(view,'evidence_ids')
    if 'samples' in view:recent(view,'samples')
    claim=view.get('transition_claim')
    if claim and claim.get('representatives'):recent(claim,'representatives')
    return view


def rejection(scientist, payload, hypothesis, claim=None):
    link=payload.get('assigned_link')
    if not link:return None
    allowed=hypothesis==link.get('hypothesis_id') if 'hypothesis_id' in link else False
    if link['kind']=='situation_candidate':
        claim=claim or {}
        candidate=link['candidate_id'];adopted=payload.get('adopted_situation_candidates',{}).get(candidate)
        allowed=any(claim.get(k)==candidate or adopted is not None and claim.get(k)==adopted for k in ('from_state','to_state'))
    if allowed:return None
    facts=context(scientist,payload)
    receipt={'state':'rejected','code':'outside_assigned_link','assigned_link':dict(link),'action_charged':False}
    if 'missing_samples' in facts:receipt['missing_samples']=facts['missing_samples']
    payload.setdefault('link_rejections',[]).append(receipt)
    return receipt


def finish(scientist,jid,payload):
    link=payload.get('assigned_link')
    if not link:return False
    if payload.get('link_end'):return True
    with scientist.store.lock:
        if link['kind']=='claim':
            experiments=getattr(scientist,'experiments',None)
            done=bool(experiments) and len(experiments.evaluation_samples(link['hypothesis_id'])[0])>=16
        elif link['kind']=='hypothesis':
            kid=link['hypothesis_id']
            done=bool(scientist.store.db.execute('SELECT 1 FROM research_claims WHERE knowledge=?',(kid,)).fetchone() and
                      scientist.store.db.execute('SELECT 1 FROM research_experiments WHERE job=? AND hypothesis=?',(jid,kid)).fetchone())
        else:done=link['candidate_id'] in payload.get('adopted_situation_candidates',{})
    if done:payload['link_end']={'reason':'link_closed','link':dict(link),'at':time.time()}
    return bool(done)


def abandon(scientist,jid,payload,finding):
    if not payload.get('assigned_link'):raise ValueError('No assigned link to abandon')
    if not finding['text'].strip():raise ValueError('Abandonment requires a model-written reason')
    entry={'reason':'abandoned','link':dict(payload['assigned_link']),'text':finding['text'],'evidence':finding['evidence'],'at':time.time(),'job':jid}
    with scientist.store.lock,scientist.store.db:
        scientist.store.db.execute('CREATE TABLE IF NOT EXISTS link_abandonments(link TEXT,job TEXT,text TEXT,evidence TEXT,created REAL)')
        scientist.store.db.execute('INSERT INTO link_abandonments VALUES(?,?,?,?,?)',(key(payload['assigned_link']),jid,finding['text'],json.dumps(finding['evidence']),entry['at']))
    payload['link_end']=entry


def history_view(payload):
    """A resume boundary changes the model view, never the durable journal."""
    if not payload.get('assigned_link'):return payload
    view=dict(payload)
    start=payload.get('link_resume_step',0)
    for field in ('action_history','visual_history','memory_results','research_steps'):
        view[field]=[e for e in payload.get(field,[]) if e.get('step',0)>start]
    view['hypotheses']={k:v for k,v in payload.get('hypotheses',{}).items() if v==payload['assigned_link'].get('hypothesis_id')}
    view['experiment_context']={}
    view['inline_claim_receipts']=[]
    view['evaluation_gate']={}
    return view
