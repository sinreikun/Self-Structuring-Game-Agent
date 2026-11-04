import json
from selfstruct.transitions import representatives, identify, situation_map, THRESHOLD

def context(store,experiments,job):
    refs=representatives(store);distribution={}
    for row in store.db.execute("SELECT id FROM events WHERE state='complete' ORDER BY id DESC LIMIT 64"):
        try:state=identify(store,store.observed_image(row['id'],'after'),refs)
        except (ValueError,OSError):state=None
        key=str(state) if state is not None else 'unknown';distribution[key]=distribution.get(key,0)+1
    claims=[]
    for row in store.db.execute('SELECT * FROM transition_claims'):
        rows,_,_=experiments.evaluation_samples(row['knowledge'],job)
        guard=store.db.execute('SELECT max_similarity,warning FROM transition_granularity WHERE knowledge=?',(row['knowledge'],)).fetchone()
        claims.append({'granularity':dict(guard) if guard else None,'hypothesis':row['knowledge'],'from_state':row['from_state'],'to_state':row['to_state'],'action':experiments.keys.index(row['action']),'probability':row['probability'],'samples':len(rows),'from_state_absent_or_unknown':not rows})
    return {'situation_map':situation_map(store),'baselines':[{'id':r['id'],'state':r['state'],'active_regions':[i for i,v in enumerate(json.loads(r['regions'])) if v>.01]} for r in store.db.execute('SELECT id,state,regions FROM state_baselines')],'granularity':[dict(r) for r in store.db.execute('SELECT * FROM transition_granularity')],'states':[dict(r) for r in store.db.execute("SELECT id,text,evidence FROM knowledge WHERE kind='state'")],'representatives':refs,'recent_state_distribution':distribution,'transition_claims':claims,'threshold':THRESHOLD,'measurement':'RGB 32x32 bilinear, 1 - normalized mean absolute error; claim-time representatives frozen'}

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
                'note':'現在Jobの集計であり評価結果ではない。未登録の過去経験は標本へ転用できない。evaluateは現在Jobの同じ仮説に紐づく実験だけを検査する。'}
            transition=scientist.store.db.execute('SELECT * FROM transition_claims WHERE knowledge=?',(group['hypothesis'],)).fetchone()
            if transition and getattr(scientist,'experiments',None):
                selected,conditional,_=scientist.experiments.evaluation_samples(group['hypothesis'],jid)
                group['claim_type']='transition'
                group['evaluation_requirements']={'scope':'current_job_only','preregistered':True,'registered_target':dict(transition),'matching_post_registration_samples':len(selected),'missing_samples':max(0,16-len(selected)),'from_state_absent_or_unknown':not selected}
            result['hypotheses'].append(group)
    transition_context = context
    with scientist.store.lock:
        result['state_transitions']=transition_context(scientist.store,getattr(scientist,'experiments',None),jid)
    result['plans_remaining']=max(0,result['plan_limit']-result['plans_used'])
    result['inputs_remaining']=max(0,result['input_limit']-result['requested_inputs'])
    result['note']='completed/changedは入力別内訳を持つ観測数で統計的な支持・反証ではない。完了経験だけを計数。未送信・中断は成功扱いしない。未登録なら新しい仮説と今後の実験が必要。異なる仮説・入力の標本は混ぜない。'
    return result
