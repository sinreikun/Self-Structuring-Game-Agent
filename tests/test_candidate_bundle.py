import json
import time
from types import SimpleNamespace
import pytest
from test_situation_candidates import seed
from selfstruct import situation_candidates as candidates, transitions
from selfstruct.research_actions import record_findings, execute, route_registration
from selfstruct.research_schema import reference_domain, response_schema, validate
from selfstruct.research import PROPOSAL_SCHEMA


def setup(tmp_path):
    store, ids = seed(tmp_path)
    payload = {'situation_candidates': candidates.generate(store, 'job'), 'research_started': time.time()}
    scientist = SimpleNamespace(store=store, keys=['A'], experiments=None,
        activity=SimpleNamespace(record=lambda *a:None, update=lambda **kw:None))
    return store, payload, scientist


def claim(payload):
    a,b = payload['situation_candidates'][:2]
    return {'from_state':a['id'], 'to_state':b['id'], 'probability':.8}


def assert_bundle(store, payload, kid):
    row = dict(store.db.execute('SELECT * FROM transition_claims WHERE knowledge=?',(kid,)).fetchone())
    provenance = [dict(r) for r in store.db.execute('SELECT * FROM candidate_adoptions')]
    assert len(provenance)==2
    frozen = json.loads(row['representatives'])
    for field,c in zip(('from_state','to_state'),payload['situation_candidates'][:2]):
        sid = row[field]
        assert sid == payload['adopted_situation_candidates'][c['id']]
        assert set(r['event'] for r in frozen if r['state']==sid)==set(c['frames'])
        assert set(r['event'] for r in transitions.representatives(store) if r['state']==sid)==set(c['frames'])
        assert store.db.execute('SELECT text FROM knowledge WHERE id=?',(sid,)).fetchone()[0]==''
        assert any(r['candidate']==c['id'] and r['state']==sid and r['job']=='job' and r['via']=='claim_bundle' and r['created']>0 for r in provenance)
    assert store.db.execute('SELECT count(*) FROM transition_granularity WHERE knowledge=?',(kid,)).fetchone()[0]==1


def test_standalone_bundle_pool_freeze_provenance_no_text(tmp_path):
    store,payload,scientist=setup(tmp_path)
    finding={'id':'h','text':'model claim','evidence':[payload['situation_candidates'][0]['frames'][0]],'transition':{**claim(payload),'action':0}}
    record_findings(scientist,'job',payload,{'findings':[finding]})
    assert_bundle(store,payload,payload['hypotheses']['h'])
    record_findings(scientist,'job',payload,{'findings':[finding]})
    assert store.db.execute('SELECT count(*) FROM candidate_adoptions').fetchone()[0]==2


def test_inline_receipt_precedes_first_input(tmp_path):
    import threading
    store,payload,scientist=setup(tmp_path)
    kid=store.knowledge('hypothesis','model','inferred','hypothesis',[1])
    payload['hypotheses']={'h':kid}
    called=[]
    class Executor:
        def require_evaluation(self,jid):pass
        def submit(self,jid,token,request,hypothesis):
            assert_bundle(store,payload,kid)
            receipt=store.db.execute('SELECT * FROM inline_claim_receipts').fetchone()
            assert receipt and receipt['registered']<=time.time()
            assert json.loads(receipt['claim'])['from_state'] in payload['adopted_situation_candidates'].values()
            called.append('first input boundary')
            return 'plan'
        def get(self,identifier):return {'state':'complete','result':'{"completed":0,"events":[]}'}
    scientist.experiments=Executor();scientist.stop_event=threading.Event()
    request={'op':'experiment','hypothesis':'h','action':0,'claim':{'type':'transition',**claim(payload)}}
    result,_,_=execute(scientist,'job',payload,request,'token')
    assert called and result['registration']['claim']['from_state'] in payload['adopted_situation_candidates'].values()


@pytest.mark.parametrize('bad',[{'probability':2},{'to_state':'sc-missing'},{'to_state':999999}])
def test_failed_inline_leaves_no_adoption(tmp_path,bad):
    from selfstruct.inline_claims import accept
    store,payload,scientist=setup(tmp_path)
    kid=store.knowledge('hypothesis','model','inferred','hypothesis',[1])
    result=accept(scientist,'job',payload,{'action':0,'claim':{'type':'transition',**claim(payload),**bad}},'token',kid)
    assert result['state']=='claim_required'
    assert store.db.execute("SELECT count(*) FROM knowledge WHERE kind='state'").fetchone()[0]==0
    assert not payload.get('adopted_situation_candidates')


def test_storage_rejection_rolls_back_all_adoption(tmp_path):
    store,payload,scientist=setup(tmp_path)
    kid=store.knowledge('hypothesis','model','inferred','hypothesis',[1])
    store.db.execute("CREATE TRIGGER reject_claim BEFORE INSERT ON transition_claims BEGIN SELECT RAISE(ABORT,'test rejection'); END")
    with pytest.raises(Exception,match='test rejection'):
        candidates.register_bundled_claim(store,payload,'job',kid,'A',claim(payload))
    assert store.db.execute("SELECT count(*) FROM knowledge WHERE kind='state'").fetchone()[0]==0
    assert store.db.execute('SELECT count(*) FROM state_frames').fetchone()[0]==0
    assert store.db.execute('SELECT count(*) FROM state_pool_history').fetchone()[0]==0
    assert store.db.execute('SELECT count(*) FROM evidence_pins').fetchone()[0]==0
    assert not payload.get('adopted_situation_candidates')


def test_closed_schema_only_claim_fields_accept_current_candidates(tmp_path):
    store,payload,scientist=setup(tmp_path)
    kid=store.knowledge('hypothesis','model','inferred','hypothesis',[1]);payload['hypotheses']={'h':kid}
    schema=response_schema(PROPOSAL_SCHEMA,['A'],{1},domain=reference_domain(store,payload))
    action={'op':'experiment','hypothesis':'h','action':0,'claim':{'type':'transition',**claim(payload)}}
    validate(schema['properties']['actions'],[action])
    action['claim']['to_state']='sc-missing'
    with pytest.raises(ValueError):validate(schema['properties']['actions'],[action])
    with pytest.raises(ValueError):validate(schema['properties']['actions'],[{'op':'observe','event':payload['situation_candidates'][0]['id']}])


def test_registration_form_bundles_only_after_all_fields_valid(tmp_path):
    store,payload,scientist=setup(tmp_path)
    scientist.experiments=SimpleNamespace(require_evaluation=lambda jid:None)
    kid=store.knowledge('hypothesis','model','inferred','hypothesis',[1])
    route_registration(scientist,'job',payload,None,'token',kid,{'claim_type':'transition','action':0})
    route=next(iter(payload['registration_routes']))
    request={'op':'registration','route':route,'decision':'complete','claim_type':'transition',**claim(payload)}
    execute(scientist,'job',payload,{**request,'probability':2},'bad')
    assert store.db.execute("SELECT count(*) FROM knowledge WHERE kind='state'").fetchone()[0]==0
    execute(scientist,'job',payload,request,'good')
    assert_bundle(store,payload,kid)


def test_real_player_input_after_bundled_receipt(tmp_path):
    import threading
    from selfstruct.config import Config
    from selfstruct.engine import Controller
    from test_research_actions import UnlabelledDynamics
    c=Controller(Config(root=str(tmp_path/'data'),keys=['A','B'],research_every=100000),UnlabelledDynamics())
    try:
        for _ in range(8):c.step()
        payload={'situation_candidates':candidates.generate(c.store,'job'),'research_started':time.time()}
        assert len(payload['situation_candidates'])>=2
        kid=c.store.knowledge('hypothesis','model transition','inferred','hypothesis',[c.store.rows()[-1]['id']])
        payload['hypotheses']={'h':kid}
        jid=c.store.job('research',payload)
        results=[];errors=[]
        def research():
            try:results.append(execute(c.scientist,jid,payload,{'op':'experiment','hypothesis':'h','action':0,'duration':10,'wait':10,'repeat':1,'claim':{'type':'transition',**claim(payload)}},'token'))
            except BaseException as error:errors.append(error)
        worker=threading.Thread(target=research);worker.start()
        deadline=time.monotonic()+10
        while worker.is_alive() and time.monotonic()<deadline:c.step()
        worker.join(1)
        assert not worker.is_alive() and not errors,errors
        result=results[0][0]
        receipt=c.store.db.execute('SELECT * FROM inline_claim_receipts WHERE job=?',(jid,)).fetchone()
        assert receipt and result['completed']==1
        events=result['events']
        assert all(c.store.db.execute('SELECT sent FROM events WHERE id=?',(e['id'],)).fetchone()[0]>=receipt['registered'] for e in events)
    finally:c.close()
