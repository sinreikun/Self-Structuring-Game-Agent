"""Scientist-defined states, measured without host semantic labels."""
import json
from PIL import Image

THRESHOLD = .90

def setup(db):
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
        for event in ids:store.db.execute('INSERT OR IGNORE INTO state_frames VALUES(?,?,?)',(state,event,phase))
        old=json.loads(store.db.execute('SELECT evidence FROM knowledge WHERE id=?',(state,)).fetchone()[0])
        store.db.execute('UPDATE knowledge SET evidence=? WHERE id=?',(json.dumps(sorted(set(old+ids))),state))
        store.pin(ids,'state-'+str(state))
    return state

def representatives(store):
    return [dict(r) for r in store.db.execute('SELECT * FROM state_frames ORDER BY state,event,phase')]

def identify(store,image,refs):
    pixels=list(image.convert('RGB').resize((32,32),Image.Resampling.BILINEAR).getdata())
    best=(0,None)
    for ref in refs:
        try:other=list(store.observed_image(ref['event'],ref['phase']).convert('RGB').resize((32,32),Image.Resampling.BILINEAR).getdata())
        except (OSError,ValueError):continue
        score=1-sum(abs(a-b) for x,y in zip(pixels,other) for a,b in zip(x,y))/(32*32*3*255)
        if score>best[0]:best=(score,ref['state'])
    return best[1] if best[0]>=THRESHOLD else None

def register_claim(store,kid,action,claim):
    refs=representatives(store)
    if any(claim[k] not in {r['state'] for r in refs} for k in ('from_state','to_state')):raise ValueError('Unknown transition state')
    latest=store.db.execute('SELECT coalesce(max(id),0) FROM events').fetchone()[0]
    with store.db:store.db.execute('INSERT INTO transition_claims VALUES(?,?,?,?,?,?,?)',(kid,action,claim['probability'],latest,claim['from_state'],claim['to_state'],json.dumps(refs)))

def samples(store,claim,rows):
    refs=json.loads(claim['representatives']);result=[]
    for row in rows:
        try:
            before=identify(store,store.observed_image(row['id'],'before'),refs)
            after=identify(store,store.observed_image(row['id'],'after'),refs)
        except (ValueError,OSError):continue
        if before!=claim['from_state'] or after is None:continue
        result.append({**row,'from_state':before,'to_state':after,'success':after==claim['to_state']})
    return result

def context(store,experiments,job):
    refs=representatives(store);distribution={}
    for row in store.db.execute("SELECT id FROM events WHERE state='complete' ORDER BY id DESC LIMIT 64"):
        try:state=identify(store,store.observed_image(row['id'],'after'),refs)
        except (ValueError,OSError):state=None
        key=str(state) if state is not None else 'unknown';distribution[key]=distribution.get(key,0)+1
    claims=[]
    for row in store.db.execute('SELECT * FROM transition_claims'):
        rows,_,_=experiments.evaluation_samples(row['knowledge'],job)
        claims.append({'hypothesis':row['knowledge'],'from_state':row['from_state'],'to_state':row['to_state'],'action':experiments.keys.index(row['action']),'probability':row['probability'],'samples':len(rows),'from_state_absent_or_unknown':not rows})
    return {'states':[dict(r) for r in store.db.execute("SELECT id,text,evidence FROM knowledge WHERE kind='state'")],'representatives':refs,'recent_state_distribution':distribution,'transition_claims':claims,'threshold':THRESHOLD,'measurement':'RGB 32x32 bilinear, 1 - normalized mean absolute error; claim-time representatives frozen'}
