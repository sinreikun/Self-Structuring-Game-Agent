import json
from types import SimpleNamespace
from PIL import Image
from selfstruct.store import Store
from selfstruct.experiments import Experiments
from selfstruct import transitions as t


def fixture(tmp_path):
    store=Store(tmp_path,100_000_000);session=store.session('test',False)
    def event(a,b=None):
        def image(v):return v if isinstance(v,Image.Image) else Image.new('RGB',(64,64),v)
        first=store.image(image(a));last=store.image(image(b if b is not None else a))
        e=store.begin(session,first,'A',.06,1,'seed',0,-1);store.finish(e,last,.5,.001)
        return e
    return store,event


def test_pool_growth_margin_cap_and_frozen_claim(tmp_path):
    store,event=fixture(tmp_path)
    a=t.register_state(store,'model a',[event('black')]);b=t.register_state(store,'model b',[event('white')])
    kid=store.knowledge('hypothesis','model prediction','inferred','hypothesis',[1])
    t.register_claim(store,kid,'A',{'from_state':a,'to_state':b,'probability':1})
    frozen=store.db.execute('SELECT representatives FROM transition_claims').fetchone()[0]
    for i in range(24):t.grow(store,event((i,i,i)),'before','observation')
    assert store.db.execute('SELECT count(*) FROM state_frames WHERE state=?',(a,)).fetchone()[0]==20
    assert store.db.execute('SELECT count(*) FROM state_pool_history WHERE retained=0').fetchone()[0]>0
    assert store.db.execute('SELECT representatives FROM transition_claims').fetchone()[0]==frozen
    assert store.db.execute("SELECT count(*) FROM evidence_pins WHERE reason=?",('claim-'+str(kid),)).fetchone()[0]==2
    close=t.register_state(store,'model nearby',[event((1,1,1))])
    ambiguous=event('black');t.grow(store,ambiguous,'before','observation')
    assert not store.db.execute('SELECT 1 FROM state_pool_history WHERE event=?',(ambiguous,)).fetchone()
    other=store.knowledge('hypothesis','model near transition','inferred','hypothesis',[1])
    t.register_claim(store,other,'A',{'from_state':a,'to_state':close,'probability':.5})
    assert store.db.execute('SELECT warning FROM transition_granularity WHERE knowledge=?',(other,)).fetchone()[0]=='granularity_indistinguishable'


def test_baseline_region_correction_and_evaluation(tmp_path):
    store,event=fixture(tmp_path)
    first=Image.new('RGB',(32,32),'black');noise=first.copy()
    for x in range(4):
        for y in range(4):noise.putpixel((x,y),(255,255,255))
    a=t.register_state(store,'model a',[event(first)])
    bid=t.save_baseline(store,'job',[first,noise,first,noise,first])
    base=store.db.execute('SELECT * FROM state_baselines WHERE id=?',(bid,)).fetchone()
    assert json.loads(base['regions'])==[1]+[0]*63
    ex=Experiments(store,['A'],SimpleNamespace(record=lambda *a:None))
    kid=store.knowledge('hypothesis','model change','inferred','hypothesis',[1]);store.claim(kid,'A',0,0)
    ids=[event(first,noise) for _ in range(16)]
    for e in ids:t.correct(store,e,base)
    store.db.execute('INSERT INTO research_experiments VALUES(?,?,?,?,?,?,?,?,?,?)',('plan','job',kid,json.dumps({'action':0,'repeat':8}),'complete',json.dumps(ids),'{}',0,1,1));store.db.commit()
    result=ex.evaluate(kid,job='job')
    assert result['status']=='supported' and result['observed_probability']==0
    assert len(result['baseline_corrections'])==16
    assert all(r['mean']==0 for r in result['baseline_corrections'])
    assert t.baseline_for(store,first)[1]['id']==bid
    assert t.situation_map(store)=={str(a):[]}


def test_zero_sample_reasons_and_map(tmp_path):
    store,event=fixture(tmp_path);a=t.register_state(store,'a',[event('black')]);b=t.register_state(store,'b',[event('white')])
    ex=Experiments(store,['A'],SimpleNamespace(record=lambda *a:None))
    kid=store.knowledge('hypothesis','model transition','inferred','hypothesis',[1])
    t.register_claim(store,kid,'A',{'from_state':a,'to_state':b,'probability':1})
    assert 'zero_samples_insufficient' in {r['code'] for r in ex.evaluate(kid,job='job')['reasons']}
    ids=[event('black','white') for _ in range(16)]
    store.db.execute('INSERT INTO research_experiments VALUES(?,?,?,?,?,?,?,?,?,?)',('plan','job',kid,json.dumps({'action':0,'repeat':8}),'complete',json.dumps(ids),'{}',0,1,1));store.db.commit()
    result=ex.evaluate(kid,job='job')
    edge=t.situation_map(store)[str(a)][0]
    assert edge['n']==16 and edge['verdict']=='supported' and edge['to_state']==b
    assert edge['evaluation']==result['evaluation_id']

