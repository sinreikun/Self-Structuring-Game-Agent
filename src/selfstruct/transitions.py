"""Scientist-defined states, measured without host semantic labels."""
import json
import time
from PIL import Image

THRESHOLD = .90

def setup(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS state_pool_history(id INTEGER PRIMARY KEY,state INTEGER,event INTEGER,phase TEXT,score REAL,margin REAL,source TEXT,created REAL,retained INTEGER);
    CREATE TABLE IF NOT EXISTS state_baselines(id INTEGER PRIMARY KEY,state INTEGER,job TEXT,created REAL,frames TEXT,regions TEXT,method TEXT);
    CREATE TABLE IF NOT EXISTS situation_cards(state INTEGER PRIMARY KEY,label TEXT,scene_kind TEXT,role TEXT,updated REAL);
    CREATE TABLE IF NOT EXISTS event_situations(event INTEGER PRIMARY KEY,situation INTEGER);
    CREATE TABLE IF NOT EXISTS corrected_changes(event INTEGER PRIMARY KEY,baseline INTEGER,raw TEXT,corrected TEXT,mean REAL);
    CREATE TABLE IF NOT EXISTS transition_granularity(knowledge INTEGER PRIMARY KEY,max_similarity REAL,warning TEXT);
    """)
    columns={r[1] for r in db.execute('PRAGMA table_info(claims)')}
    if 'situation' not in columns:db.execute('ALTER TABLE claims ADD COLUMN situation INTEGER')
    if 'representatives' not in columns:db.execute('ALTER TABLE claims ADD COLUMN representatives TEXT')
    db.execute('DROP VIEW IF EXISTS research_claims')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS state_frames(state INTEGER, event INTEGER, phase TEXT, PRIMARY KEY(state,event,phase));
    CREATE TABLE IF NOT EXISTS transition_claims(knowledge INTEGER PRIMARY KEY, action TEXT, probability REAL, after_id INTEGER, from_state INTEGER, to_state INTEGER, representatives TEXT);
    CREATE VIEW research_claims AS
      SELECT knowledge,action,probability,after_id,'change_rate' claim_type,NULL from_state,NULL to_state,representatives,situation FROM claims
      UNION ALL SELECT knowledge,action,probability,after_id,'transition',from_state,to_state,representatives,NULL situation FROM transition_claims;
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
                ranked.append((max(similarity(value,event_pixels(store,r['event'],r['phase'])) for r in refs if r['state']==sid),sid))
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


def event_pixels(store,event,phase):
    """Pixels of a recorded event frame. Event images are write-once, so a
    store that opts in (the Controller sets _pixel_cache) reuses the converted
    32x32 values instead of reloading and decoding the same PNGs on every
    identification. Stores without the attribute always decode."""
    cache=getattr(store,'_pixel_cache',None)
    if cache is None:return pixels(store.observed_image(event,phase))
    key=(event,phase)
    value=cache.get(key)
    if value is None:
        value=pixels(store.observed_image(event,phase))
        if len(cache)>=512:cache.clear()
        cache[key]=value
    return value


def similarity(a,b):
    # Flat RGB bytes take the same integer sum in one pass; the result is
    # bit-identical to the nested tuple form below.
    if type(a) is bytes and type(b) is bytes:
        return 1-sum(abs(x-y) for x,y in zip(a,b))/(32*32*3*255)
    return 1-sum(abs(x-y) for p,q in zip(a,b) for x,y in zip(p,q))/(32*32*3*255)


def flat_pixels(image):
    return image.convert('RGB').resize((32,32),Image.Resampling.BILINEAR).tobytes()


def event_flat(store,event,phase):
    """Flat RGB bytes of a recorded event frame, memoized on stores that opt
    in (see event_pixels); the play loop scores the same frames every step."""
    cache=getattr(store,'_flat_cache',None)
    if cache is None:return flat_pixels(store.observed_image(event,phase))
    key=(event,phase)
    value=cache.get(key)
    if value is None:
        value=flat_pixels(store.observed_image(event,phase))
        if len(cache)>=512:cache.clear()
        cache[key]=value
    return value


def _scores_value(store,value,refs):
    states={}
    for ref in refs:
        try:score=similarity(value,event_flat(store,ref['event'],ref['phase']))
        except (OSError,ValueError):continue
        states[ref['state']]=max(states.get(ref['state'],0),score)
    return sorted(states.items(),key=lambda v:(-v[1],v[0]))


def scores(store,image,refs):
    return _scores_value(store,flat_pixels(image),refs)


def identify(store,image,refs):
    ranked=scores(store,image,refs)
    return ranked[0][0] if ranked and ranked[0][1]>=THRESHOLD else None


def identify_event(store,event,phase,refs):
    # Identification of a recorded frame is a pure function of the frame and
    # the representative pool; on stores that opt in (the Controller sets
    # _identify_cache) the result is memoized per pool fingerprint, because
    # context building re-identifies the same recent frames against the same
    # pool on every research iteration.
    cache=getattr(store,'_identify_cache',None)
    if cache is None:
        ranked=_scores_value(store,event_flat(store,event,phase),refs)
        return ranked[0][0] if ranked and ranked[0][1]>=THRESHOLD else None
    key=(event,phase,tuple((r['state'],r['event'],r['phase']) for r in refs))
    if key in cache:return cache[key]
    ranked=_scores_value(store,event_flat(store,event,phase),refs)
    result=ranked[0][0] if ranked and ranked[0][1]>=THRESHOLD else None
    if len(cache)>=512:cache.clear()
    cache[key]=result
    return result


POOL_LIMIT=20
POOL_MARGIN=.005


def grow(store,event,phase,source):
    with store.lock,store.db:
        ranked=_scores_value(store,event_flat(store,event,phase),representatives(store))
        if not ranked:return
        state,score=ranked[0];margin=score-(ranked[1][1] if len(ranked)>1 else 0)
        if score<THRESHOLD or margin<POOL_MARGIN:return
        # ranked[0] is the best similarity to an existing frame of this state;
        # a near-exact duplicate adds no representative diversity, and adding
        # it would only trigger a full pool trim.
        if score>=0.999:return
        if store.db.execute('SELECT 1 FROM state_frames WHERE state=? AND event=? AND phase=?',(state,event,phase)).fetchone():return
        store.db.execute('INSERT INTO state_frames VALUES(?,?,?)',(state,event,phase))
        store.db.execute('INSERT INTO state_pool_history(state,event,phase,score,margin,source,created,retained) VALUES(?,?,?,?,?,?,?,1)',(state,event,phase,score,margin,source,time.time()))
        trim(store,state)
        store.pin([event],'state-'+str(state))


def trim(store,state):
    refs=[dict(r) for r in store.db.execute('SELECT * FROM state_frames WHERE state=? ORDER BY event,phase',(state,))]
    if len(refs)<=POOL_LIMIT:return
    values=[event_flat(store,r['event'],r['phase']) for r in refs]
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
    a=event_pixels(store,event,'before');b=event_pixels(store,event,'after')
    raw=regions(a,b);noise=json.loads(baseline['regions']);residual=[max(0,x-y) for x,y in zip(raw,noise)]
    with store.lock,store.db:store.db.execute('INSERT OR REPLACE INTO corrected_changes VALUES(?,?,?,?,?)',(event,baseline['id'],json.dumps(raw),json.dumps(residual),sum(residual)/64))


def situation_map(store):
    result={str(r[0]):[] for r in store.db.execute("SELECT id FROM knowledge WHERE kind='state'")}
    for row in store.db.execute('SELECT id,result FROM research_evaluations ORDER BY id'):
        e=json.loads(row['result'])
        if e['status'] not in ('supported','refuted'):continue
        target=e['registered_target']
        if e.get('claim_type')=='transition':
            entry={'hypothesis':e['hypothesis'],'evaluation':row['id'],'input':target['action'],'to_state':target['to_state'],'verdict':e['status'],'n':e['n']}
            bucket=result.setdefault(str(target['from_state']),[])
        elif e.get('claim_type')=='change_rate' and target.get('situation') is not None:
            entry={'hypothesis':e['hypothesis'],'evaluation':row['id'],'input':target['action'],'to_state':None,'effect':'change_rate','verdict':e['status'],'n':e['n']}
            bucket=result.setdefault(str(target['situation']),[])
        else:continue
        bucket[:]=[v for v in bucket if v['hypothesis']!=e['hypothesis']];bucket.append(entry)
    return result


def register_claim(store,kid,action,claim):
    refs=representatives(store)
    if any(claim[k] not in {r['state'] for r in refs} for k in ('from_state','to_state')):raise ValueError('Unknown transition state')
    latest=store.db.execute('SELECT coalesce(max(id),0) FROM events').fetchone()[0]
    left=[event_pixels(store,r['event'],r['phase']) for r in refs if r['state']==claim['from_state']]
    right=[event_pixels(store,r['event'],r['phase']) for r in refs if r['state']==claim['to_state']]
    maximum=max(similarity(a,b) for a in left for b in right)
    with store.db:
        store.pin([r['event'] for r in refs],'claim-'+str(kid))
        store.db.execute('INSERT OR REPLACE INTO transition_granularity VALUES(?,?,?)',(kid,maximum,'granularity_indistinguishable' if maximum>=.99 else ''))
        store.db.execute('INSERT INTO transition_claims VALUES(?,?,?,?,?,?,?)',(kid,action,claim['probability'],latest,claim['from_state'],claim['to_state'],json.dumps(refs)))

def samples(store,claim,rows):
    refs=json.loads(claim['representatives']);result=[]
    for row in rows:
        try:
            before=identify_event(store,row['id'],'before',refs)
            after=identify_event(store,row['id'],'after',refs)
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
        try:state=identify_event(store,row['id'],'after',refs)
        except (ValueError,OSError):state=None
        key=str(state) if state is not None else 'unknown';distribution[key]=distribution.get(key,0)+1
    claims=[]
    for row in store.db.execute('SELECT * FROM transition_claims'):
        rows,_,_=experiments.evaluation_samples(row['knowledge'],job)
        guard=store.db.execute('SELECT max_similarity,warning FROM transition_granularity WHERE knowledge=?',(row['knowledge'],)).fetchone()
        claims.append({'granularity':dict(guard) if guard else None,'hypothesis':row['knowledge'],'from_state':row['from_state'],'to_state':row['to_state'],'action':experiments.keys.index(row['action']),'probability':row['probability'],'samples':len(rows),'from_state_absent_or_unknown':not rows})
    from . import skills as compiled_skills
    return {'situation_map':situation_map(store),'situation_cards':cards(store),'skills':compiled_skills.all_skills(store),'baselines':[{'id':r['id'],'state':r['state'],'active_regions':[i for i,v in enumerate(json.loads(r['regions'])) if v>.01]} for r in store.db.execute('SELECT id,state,regions FROM state_baselines')],'granularity':[dict(r) for r in store.db.execute('SELECT * FROM transition_granularity')],'states':[dict(r) for r in store.db.execute("SELECT id,text,evidence FROM knowledge WHERE kind='state'")],'representatives':refs,'recent_state_distribution':distribution,'transition_claims':claims,'threshold':THRESHOLD,'measurement':'RGB 32x32 bilinear, 1 - normalized mean absolute error; claim-time representatives frozen'}


def sample_diagnostics(store,claim,rows):
    counts={'total':len(rows),'from_unknown':0,'from_mismatch':0,'to_unknown':0,'missing_image':0,'eligible':0}
    refs=json.loads(claim['representatives'])
    for row in rows:
        try:
            before=identify_event(store,row['id'],'before',refs)
            after=identify_event(store,row['id'],'after',refs)
        except (ValueError,OSError):counts['missing_image']+=1;continue
        key='from_unknown' if before is None else 'from_mismatch' if before!=claim['from_state'] else 'to_unknown' if after is None else 'eligible'
        counts[key]+=1
    return counts


def cards(store):
    return {str(r['state']):{'label':r['label'],'scene_kind':r['scene_kind'],'role':r['role']}
            for r in store.db.execute('SELECT * FROM situation_cards')}


def save_card(store,state,label=None,scene_kind=None,role=None):
    with store.lock,store.db:
        if not store.db.execute("SELECT 1 FROM knowledge WHERE id=? AND kind='state'",(state,)).fetchone():raise ValueError('Unknown state')
        current=store.db.execute('SELECT * FROM situation_cards WHERE state=?',(state,)).fetchone()
        values={'label':label,'scene_kind':scene_kind,'role':role}
        if current is not None:
            merged={k:(values[k] if values[k] is not None else current[k]) for k in values}
        else:
            merged=values
        store.db.execute('INSERT OR REPLACE INTO situation_cards VALUES(?,?,?,?,?)',
                         (state,merged['label'],merged['scene_kind'],merged['role'],time.time()))
    return {'state':state,**merged}


def register_unnamed_state(store,ids,phase='before'):
    """Create a state with no model text, the way claim bundles already do.

    store.knowledge() requires model text, so Host-side mechanical creation
    inserts the knowledge row directly and then reuses register_state."""
    with store.lock,store.db:
        state=store.db.execute("INSERT INTO knowledge(kind,text,provenance,validation,evidence,created) VALUES('state','','self_generated_model','hypothesis',?,?)",
                               (json.dumps(list(ids)),time.time())).lastrowid
    return register_state(store,'',list(ids),phase,state=state)


def anchored(store,claim,rows):
    """Keep only samples taken in the claim's registered situation.

    Two Host-mechanical records count: the play loop's contemporaneous
    situation tag for the event, and identification of its before-frame
    against the situation's live representative pool. The anchor itself is
    part of the registered claim, never chosen at evaluation time."""
    refs=[r for r in representatives(store) if r['state']==claim['situation']]
    tagged=set()
    if store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_situations'").fetchone():
        tagged={r[0] for r in store.db.execute('SELECT event FROM event_situations WHERE situation=?',(claim['situation'],))}
    result=[]
    for row in rows:
        if row['id'] in tagged:result.append(row);continue
        try:before=identify_event(store,row['id'],'before',refs)
        except (ValueError,OSError):continue
        if before==claim['situation']:result.append(row)
    return result


def bootstrap_situations(store,limit=2):
    """Register recurring frame clusters as unnamed situations, mechanically.

    The play loop calls this periodically so situation identity exists before
    any research job runs; names and meaning are the model's to add later via
    situation cards. Mirrors the candidate clustering already used at job
    start, minus the adoption step that never fired in practice."""
    from .situation_candidates import cluster
    refs=representatives(store)
    excluded={(r['event'],r['phase']) for r in refs}
    existing=[]
    for ref in refs:
        try:existing.append(event_pixels(store,ref['event'],ref['phase']))
        except (ValueError,OSError):pass
    frames=[]
    for row in store.db.execute("SELECT id FROM events WHERE state='complete' ORDER BY id DESC LIMIT 64"):
        if (row['id'],'before') in excluded:continue
        try:frames.append((row['id'],event_pixels(store,row['id'],'before')))
        except (ValueError,OSError):pass
    created=[]
    for candidate in cluster(frames,existing)[:limit]:
        created.append(register_unnamed_state(store,candidate['frames']))
    return created
