"""Scientist-defined states, measured without host semantic labels."""
import json
import time
from PIL import Image

THRESHOLD = .90

def setup(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS state_pool_history(id INTEGER PRIMARY KEY,state INTEGER,event INTEGER,phase TEXT,score REAL,margin REAL,source TEXT,created REAL,retained INTEGER);
    CREATE TABLE IF NOT EXISTS state_baselines(id INTEGER PRIMARY KEY,state INTEGER,job TEXT,created REAL,frames TEXT,regions TEXT,method TEXT);
    CREATE TABLE IF NOT EXISTS corrected_changes(event INTEGER PRIMARY KEY,baseline INTEGER,raw TEXT,corrected TEXT,mean REAL);
    CREATE TABLE IF NOT EXISTS transition_granularity(knowledge INTEGER PRIMARY KEY,max_similarity REAL,warning TEXT);
    """)
    db.executescript('''
    CREATE TABLE IF NOT EXISTS state_frames(state INTEGER, event INTEGER, phase TEXT, PRIMARY KEY(state,event,phase));
    CREATE TABLE IF NOT EXISTS transition_claims(knowledge INTEGER PRIMARY KEY, action TEXT, probability REAL, after_id INTEGER, from_state INTEGER, to_state INTEGER, representatives TEXT);
    CREATE VIEW IF NOT EXISTS research_claims AS
      SELECT knowledge,action,probability,after_id,'change_rate' claim_type,NULL from_state,NULL to_state,NULL representatives FROM claims
      UNION ALL SELECT knowledge,action,probability,after_id,'transition',from_state,to_state,representatives FROM transition_claims;
    ''')

def register_state(store,text,ids,phase='before',state=None):
    if phase not in ('before','after'):raise ValueError('Invalid representative phase')
    for event in ids:store.observed_image(event,phase)
    with store.lock,store.db:
        if state is None:state=store.knowledge('state',text,'self_generated_model','hypothesis',ids)
        elif not store.db.execute("SELECT 1 FROM knowledge WHERE id=? AND kind='state'",(state,)).fetchone():raise ValueError('Unknown state')
        for event in ids:
            if not store.db.execute('SELECT 1 FROM state_frames WHERE state=? AND event=? AND phase=?',(state,event,phase)).fetchone():
                store.db.execute('INSERT INTO state_frames VALUES(?,?,?)',(state,event,phase))
                store.db.execute('INSERT INTO state_pool_history(state,event,phase,score,margin,source,created,retained) VALUES(?,?,?,NULL,NULL,?,?,1)',(state,event,phase,'model_definition',time.time()))
        trim(store,state)
        # Attach previously unclassified no-input observations by measurement only.
        for base in store.db.execute('SELECT id,frames FROM state_baselines WHERE state IS NULL').fetchall():
            value=json.loads(base['frames'])[0]
            refs=representatives(store);ranked=[]
            for sid in sorted({r['state'] for r in refs}):
                ranked.append((max(similarity(value,pixels(store.observed_image(r['event'],r['phase']))) for r in refs if r['state']==sid),sid))
            ranked.sort(reverse=True)
            if ranked and ranked[0][0]>=THRESHOLD and (len(ranked)==1 or ranked[0][0]-ranked[1][0]>=POOL_MARGIN):
                store.db.execute('UPDATE state_baselines SET state=? WHERE id=?',(ranked[0][1],base['id']))
        old=json.loads(store.db.execute('SELECT evidence FROM knowledge WHERE id=?',(state,)).fetchone()[0])
        store.db.execute('UPDATE knowledge SET evidence=? WHERE id=?',(json.dumps(sorted(set(old+ids))),state))
        store.pin([r[0] for r in store.db.execute('SELECT event FROM state_frames WHERE state=?',(state,))],'state-'+str(state))
    return state

def representatives(store):
    return [dict(r) for r in store.db.execute('SELECT * FROM state_frames ORDER BY state,event,phase')]

def pixels(image):
    return list(image.convert('RGB').resize((32,32),Image.Resampling.BILINEAR).getdata())


def similarity(a,b):
    return 1-sum(abs(x-y) for p,q in zip(a,b) for x,y in zip(p,q))/(32*32*3*255)


def scores(store,image,refs):
    value=pixels(image);states={}
    for ref in refs:
        try:score=similarity(value,pixels(store.observed_image(ref['event'],ref['phase'])))
        except (OSError,ValueError):continue
        states[ref['state']]=max(states.get(ref['state'],0),score)
    return sorted(states.items(),key=lambda v:(-v[1],v[0]))


def identify(store,image,refs):
    ranked=scores(store,image,refs)
    return ranked[0][0] if ranked and ranked[0][1]>=THRESHOLD else None


POOL_LIMIT=20
POOL_MARGIN=.005


def grow(store,event,phase,source):
    with store.lock,store.db:
        ranked=scores(store,store.observed_image(event,phase),representatives(store))
        if not ranked:return
        state,score=ranked[0];margin=score-(ranked[1][1] if len(ranked)>1 else 0)
        if score<THRESHOLD or margin<POOL_MARGIN:return
        if store.db.execute('SELECT 1 FROM state_frames WHERE state=? AND event=? AND phase=?',(state,event,phase)).fetchone():return
        store.db.execute('INSERT INTO state_frames VALUES(?,?,?)',(state,event,phase))
        store.db.execute('INSERT INTO state_pool_history(state,event,phase,score,margin,source,created,retained) VALUES(?,?,?,?,?,?,?,1)',(state,event,phase,score,margin,source,time.time()))
        trim(store,state)
        store.pin([event],'state-'+str(state))


def trim(store,state):
    refs=[dict(r) for r in store.db.execute('SELECT * FROM state_frames WHERE state=? ORDER BY event,phase',(state,))]
    if len(refs)<=POOL_LIMIT:return
    values=[pixels(store.observed_image(r['event'],r['phase'])) for r in refs]
    totals=[sum(1-similarity(a,b) for b in values) for a in values]
    medoid=min(range(len(refs)),key=lambda i:(totals[i],i))
    keep=sorted(range(len(refs)),key=lambda i:(1-similarity(values[medoid],values[i]),i))[:POOL_LIMIT]
    for i,r in enumerate(refs):
        if i not in keep:
            store.db.execute('DELETE FROM state_frames WHERE state=? AND event=? AND phase=?',(state,r['event'],r['phase']))
            store.db.execute('UPDATE state_pool_history SET retained=0 WHERE state=? AND event=? AND phase=?',(state,r['event'],r['phase']))
    # Claim snapshots have independent pins; changing the live pool cannot erase them.
    store.unpin('state-'+str(state))
    store.pin([refs[i]['event'] for i in keep],'state-'+str(state))


def regions(a,b):
    return [sum(abs(a[y*32+x][c]-b[y*32+x][c]) for y in range(gy*4,gy*4+4) for x in range(gx*4,gx*4+4) for c in range(3))/(16*3*255) for gy in range(8) for gx in range(8)]


def save_baseline(store,job,frames):
    values=[pixels(f) for f in frames];deltas=[regions(values[0],v) for v in values[1:]]
    activity=[max(d[i] for d in deltas) for i in range(64)]
    ranked=scores(store,frames[0],representatives(store))
    state=ranked[0][0] if ranked and ranked[0][1]>=THRESHOLD and (len(ranked)==1 or ranked[0][1]-ranked[1][1]>=POOL_MARGIN) else None
    with store.lock,store.db:
        return store.db.execute('INSERT INTO state_baselines(state,job,created,frames,regions,method) VALUES(?,?,?,?,?,?)',(state,job,time.time(),json.dumps(values),json.dumps(activity),'RGB32 bilinear; 8x8 cells; max absolute change from first frame; positive residual')).lastrowid


def baseline_for(store,image):
    ranked=scores(store,image,representatives(store))
    if not ranked or ranked[0][1]<THRESHOLD or (len(ranked)>1 and ranked[0][1]-ranked[1][1]<POOL_MARGIN):return None,None
    state=ranked[0][0]
    row=store.db.execute('SELECT * FROM state_baselines WHERE state=? ORDER BY id DESC LIMIT 1',(state,)).fetchone()
    return state,row


def correct(store,event,baseline):
    a=pixels(store.observed_image(event,'before'));b=pixels(store.observed_image(event,'after'))
    raw=regions(a,b);noise=json.loads(baseline['regions']);residual=[max(0,x-y) for x,y in zip(raw,noise)]
    with store.lock,store.db:store.db.execute('INSERT OR REPLACE INTO corrected_changes VALUES(?,?,?,?,?)',(event,baseline['id'],json.dumps(raw),json.dumps(residual),sum(residual)/64))


def situation_map(store):
    result={str(r[0]):[] for r in store.db.execute("SELECT id FROM knowledge WHERE kind='state'")}
    for row in store.db.execute('SELECT id,result FROM research_evaluations ORDER BY id'):
        e=json.loads(row['result'])
        if e.get('claim_type')!='transition' or e['status'] not in ('supported','refuted'):continue
        target=e['registered_target'];entry={'hypothesis':e['hypothesis'],'evaluation':row['id'],'input':target['action'],'to_state':target['to_state'],'verdict':e['status'],'n':e['n']}
        bucket=result.setdefault(str(target['from_state']),[])
        bucket[:]=[v for v in bucket if v['hypothesis']!=e['hypothesis']];bucket.append(entry)
    return result


def register_claim(store,kid,action,claim):
    refs=representatives(store)
    if any(claim[k] not in {r['state'] for r in refs} for k in ('from_state','to_state')):raise ValueError('Unknown transition state')
    latest=store.db.execute('SELECT coalesce(max(id),0) FROM events').fetchone()[0]
    left=[pixels(store.observed_image(r['event'],r['phase'])) for r in refs if r['state']==claim['from_state']]
    right=[pixels(store.observed_image(r['event'],r['phase'])) for r in refs if r['state']==claim['to_state']]
    maximum=max(similarity(a,b) for a in left for b in right)
    with store.db:
        store.pin([r['event'] for r in refs],'claim-'+str(kid))
        store.db.execute('INSERT OR REPLACE INTO transition_granularity VALUES(?,?,?)',(kid,maximum,'granularity_indistinguishable' if maximum>=.99 else ''))
        store.db.execute('INSERT INTO transition_claims VALUES(?,?,?,?,?,?,?)',(kid,action,claim['probability'],latest,claim['from_state'],claim['to_state'],json.dumps(refs)))

def samples(store,claim,rows):
    refs=json.loads(claim['representatives']);result=[]
    for row in rows:
        try:
            before=identify(store,store.observed_image(row['id'],'before'),refs)
            after=identify(store,store.observed_image(row['id'],'after'),refs)
        except (ValueError,OSError):continue
        if before!=claim['from_state'] or after is None:continue
        correction=store.db.execute('SELECT mean FROM corrected_changes WHERE event=?',(row['id'],)).fetchone()
        effect=correction is None or before==after or correction['mean']>.01
        result.append({**row,'from_state':before,'to_state':after,'success':after==claim['to_state'] and effect})
    return result

def context(store,experiments,job):
    if not getattr(store, 'context_snapshot', False):
        from .context_snapshot import snapshot
        import copy
        view = snapshot(store, job)
        try:
            detached = copy.copy(experiments)
            if detached is not None: detached.store = view
            return context(view, detached, job)
        finally:
            view.db.close()

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


def sample_diagnostics(store,claim,rows):
    counts={'total':len(rows),'from_unknown':0,'from_mismatch':0,'to_unknown':0,'missing_image':0,'eligible':0}
    refs=json.loads(claim['representatives'])
    for row in rows:
        try:
            before=identify(store,store.observed_image(row['id'],'before'),refs)
            after=identify(store,store.observed_image(row['id'],'after'),refs)
        except (ValueError,OSError):counts['missing_image']+=1;continue
        key='from_unknown' if before is None else 'from_mismatch' if before!=claim['from_state'] else 'to_unknown' if after is None else 'eligible'
        counts[key]+=1
    return counts
