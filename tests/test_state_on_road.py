import json
import threading
import time
from types import SimpleNamespace
from PIL import Image
import pytest
from selfstruct.store import Store
from selfstruct.experiments import Experiments
from selfstruct.research_actions import execute, record_findings, history_context, action_budget_used

@pytest.fixture
def road(tmp_path):
    store=Store(tmp_path,100_000_000);session=store.session('test',False)
    a=store.image(Image.new('RGB',(32,32),'black'));b=store.image(Image.new('RGB',(32,32),'white'))
    eid=store.begin(session,a,'A',.06,1,'seed',42,-1);store.finish(eid,b,.5,.001)
    activity=SimpleNamespace(record=lambda *a:None,update=lambda **kw:None)
    ex=Experiments(store,['A'],activity)
    submit=ex.submit
    def complete(*args):
        identifier=submit(*args)
        ex.finish(identifier,'complete')
        return identifier
    ex.submit=complete
    scientist=SimpleNamespace(store=store,keys=['A'],experiments=ex,activity=activity,stop_event=threading.Event())
    payload={'research_rows':[{'id':eid}],'research_started':time.time(),'research_calls':1}
    record_findings(scientist,'job',payload,{'findings':[{'id':'h','text':'model prediction','evidence':[eid]}]})
    yield scientist,payload,eid
    store.close()


def experiment(s,p,claim=None,token='initial',omit=False):
    r={'op':'experiment','hypothesis':'h','action':0,'repeat':8}
    if not omit:r['claim']=claim
    return execute(s,'job',p,r,token)[0]


def test_registration_precedes_submit_and_receipt_survives(road):
    s,p,e=road
    submit=s.experiments.submit
    def checked(*args):
        assert s.store.db.execute('select probability from claims').fetchone()[0]==.7
        row=s.store.db.execute('select * from inline_claim_receipts').fetchone()
        assert json.loads(row['request'])['claim']['probability']==.7
        assert s.store.db.execute('select count(*) from research_experiments').fetchone()[0]==0
        return submit(*args)
    s.experiments.submit=checked
    result=experiment(s,p,{'type':'change_rate','probability':.7,'situation':{'text':'start','evidence':[e],'phase':'before'}})
    assert result['registration']['plan']==result['experiment']
    assert not p.get('registration_routes')


def test_inline_transition_reuses_definition_and_claim(road):
    s,p,e=road
    claim={'type':'transition','probability':.8,'from_state':{'text':'model dark','evidence':[e],'phase':'before'},'to_state':{'text':'model light','evidence':[e],'phase':'after'}}
    first=experiment(s,p,claim)
    normalized=first['registration']['claim']
    assert type(normalized['from_state'])==int and type(normalized['to_state'])==int
    assert s.store.db.execute('select count(*) from state_frames').fetchone()[0]==2
    assert experiment(s,p,claim,'resend')['registration']['reused']
    assert experiment(s,p,normalized,'ids')['registration']['reused']
    assert experiment(s,p,token='omit',omit=True)['registration']['reused']
    assert s.store.db.execute('select count(*) from transition_claims').fetchone()[0]==1
    assert s.store.db.execute('select count(*) from state_frames').fetchone()[0]==2
    assert s.store.db.execute('select count(*) from research_experiments').fetchone()[0]==4


@pytest.mark.parametrize('claim,fields',[
    ({'type':'change_rate'},['claim.probability','claim.situation']),
    ({'type':'transition','probability':.6},['claim.from_state','claim.to_state']),
    ({'type':'change_rate','probability':.6,'from_state':1},['claim.from_state','claim.situation']),
    ({'probability':.6},['claim.type']),
    ({'type':'change_rate','probability':True},['claim.probability','claim.situation']),
    ({'type':'change_rate','probability':float('nan')},['claim.probability','claim.situation']),
])
def test_invalid_inline_is_named_unbilled_and_has_no_pending(road,claim,fields):
    s,p,e=road
    from selfstruct.research_actions import run_actions
    run_actions(s,'job',p,[{'op':'experiment','hypothesis':'h','action':0,'claim':claim}])
    result=p['action_history'][-1]['result']
    assert result['state']=='claim_required'
    assert [x['field'] for x in result['fields']]==fields
    assert action_budget_used(p)==0
    assert not p.get('registration_routes')
    assert s.store.db.execute('select count(*) from research_experiments').fetchone()[0]==0
    assert s.store.db.execute('select count(*) from research_claims').fetchone()[0]==0


def test_all_fields_validated_before_state_save(road):
    s,p,e=road
    result=experiment(s,p,{'type':'transition','probability':.6,'from_state':{'text':'a','evidence':[e],'phase':'before'},'to_state':900})
    assert result['fields'][0]['field']=='claim.to_state'
    assert not s.store.db.execute('select * from state_frames').fetchall()


def test_gate_b_precedes_inline(road):
    s,p,e=road
    from selfstruct.experiments import EvaluationRequired
    def blocked(job):raise EvaluationRequired([{'hypothesis':3,'n':16}])
    s.experiments.require_evaluation=blocked
    with pytest.raises(EvaluationRequired):experiment(s,p,{'type':'change_rate','probability':.7})
    assert not s.store.db.execute('select * from claims').fetchall()


def test_corrected_resend_and_conflicting_claim(road):
    s,p,e=road
    assert experiment(s,p,{'type':'change_rate'})['state']=='claim_required'
    anchored={'type':'change_rate','probability':.7,'situation':{'text':'start','evidence':[e],'phase':'before'}}
    assert experiment(s,p,anchored,'fixed')['state']=='complete'
    assert experiment(s,p,{**anchored,'probability':.8},'conflict')['state']=='claim_required'
    assert s.store.db.execute('select count(*) from claims').fetchone()[0]==1
