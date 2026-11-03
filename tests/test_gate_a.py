import json,time,threading
import pytest
from selfstruct.config import Config
from selfstruct.engine import Controller
from selfstruct.smoke import SyntheticGame
from selfstruct.experiments import PreregistrationRequired
from selfstruct.research_actions import run_actions,action_budget_used,record_findings,loop_gaps,history_context,experiment_context


def test_gate_rejection_budget_persistence_and_new_id_adaptation(tmp_path):
    c=Controller(Config(root=str(tmp_path),keys=['A'],research_every=100000),SyntheticGame());c.step()
    job=c.store.job('research',{});p={'research_rows':c.store.rows(),'research_calls':1,'research_started':time.time()}
    try:
        finding={'id':'h1','text':'画像差分について未確認','evidence':[c.last_eid]}
        record_findings(c.scientist,job,p,{'findings':[finding]});kid=p['hypotheses']['h1']
        for step in range(1,4):
            p['research_calls']=step
            run_actions(c.scientist,job,p,[{'op':'experiment','hypothesis':'h1','action':0,'repeat':8}])
        assert c.store.db.execute('select count(*) from research_experiments').fetchone()[0]==0
        budget=experiment_context(c.scientist,job)
        assert budget['plans_remaining']==4 and budget['inputs_remaining']==32
        saved=json.loads(c.store.get_job(job)['payload'])
        assert saved['preregistration_gate'][str(kid)]['rejections']==3
        assert action_budget_used(saved)==0 and len(saved['action_history'])==3
        result=saved['action_history'][-1]['result']
        assert result['state']=='rejected' and result['hypothesis']==kid
        assert result['reasons'][0]['code']=='not_preregistered'
        assert 'action と change_probability' in result['reasons'][0]['detail']
        p['action_history']=[] # reminder survives truncation of recent action history
        assert str(kid) in history_context(p) and any('事前登録不足' in s for s in loop_gaps(p))
        # Registration mechanism remains unchanged: use a supplied new ID.
        record_findings(c.scientist,job,p,{'findings':[{**finding,'id':'h2','action':0,'change_probability':1}]})
        registered=p['hypotheses']['h2'];assert registered!=kid
        assert p['registrations_after_rejections'][0]['hypothesis']==registered
        assert not p['preregistration_gate'][str(kid)]['registered_after_rejection']
        plan=c.experiments.submit(job,'registered',{'action':0,'repeat':8},registered)
        assert c.experiments.get(plan)['hypothesis']==registered
        free=c.experiments.submit(job,'explore',{'action':0},None)
        assert c.experiments.get(free)['hypothesis'] is None
        assert experiment_context(c.scientist,job)['plans_used']==2
    finally:c.close()


def test_exploration_through_action_boundary(tmp_path):
    c=Controller(Config(root=str(tmp_path),keys=['A'],research_every=100000),SyntheticGame());c.step()
    job=c.store.job('research',{});p={'research_started':time.time(),'research_calls':1};errors=[]
    def run():
        try:run_actions(c.scientist,job,p,[{'op':'experiment','action':0,'duration':10,'wait':10}])
        except BaseException as e:errors.append(e)
    t=threading.Thread(target=run);t.start()
    try:
        end=time.time()+10
        while t.is_alive() and time.time()<end:c.step();time.sleep(.01)
        t.join(1);assert not t.is_alive() and not errors
        assert p['action_history'][0]['result']['completed']==1
        assert p['action_history'][0]['result']['hypothesis'] is None
        assert any('仮説IDに紐付く' in s for s in loop_gaps(p))
    finally:c.scientist.stop_event.set();t.join(2);c.close()


@pytest.mark.parametrize('probability',[None,-1,2,'invalid'])
def test_missing_or_invalid_claim_never_creates_plan(tmp_path,probability):
    c=Controller(Config(root=str(tmp_path),keys=['A']),SyntheticGame());c.step()
    try:
        kid=c.store.knowledge('hypothesis','検証対象','inferred','hypothesis',[c.last_eid])
        if probability is not None:
            with c.store.db:c.store.db.execute('insert into claims values(?,?,?,?,?)',(kid,'A',probability,c.last_eid,c.last_eid))
        with pytest.raises(PreregistrationRequired) as error:c.experiments.submit('job','x',{'action':0},kid)
        assert error.value.hypothesis==kid
        assert c.store.db.execute('select count(*) from research_experiments').fetchone()[0]==0
    finally:c.close()
