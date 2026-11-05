"""Real stores and Player path; scripted decisions do not prove a real-game loop."""
import json
import pytest

from selfstruct.config import Config
from selfstruct.engine import Controller
from selfstruct.smoke import SyntheticGame
from selfstruct.research_memory import report,document,render_report,finish_report
from selfstruct.research_actions import made_progress,loop_gaps



def legacy_unregistered_plan(c,job,token,request,kid):
    """Seed a pre-gate persisted plan to keep legacy evaluation coverage."""
    identifier=c.experiments.submit(job,token,request)
    with c.store.lock,c.store.db:
        c.store.db.execute('UPDATE research_experiments SET hypothesis=? WHERE id=?',(kid,identifier))
        c.store.db.execute("UPDATE knowledge SET validation='testing' WHERE id=?",(kid,))
    return identifier


@pytest.fixture
def world(tmp_path):
    c=Controller(Config(root=str(tmp_path),keys=['A','B'],research_every=100000),SyntheticGame())
    c.step()
    yield c
    c.close()


def test_evaluation_snapshot_survives_later_verdict_and_report_rejects_missing_handoff(world):
    c=world;job=c.store.job('research',{})
    kid=c.store.knowledge('hypothesis','画面の変化率は未確認','inferred','hypothesis',[c.last_eid])
    identifier=legacy_unregistered_plan(c,job,'1',{'action':0,'repeat':1,'duration':10,'wait':10},kid)
    c.step()
    result=json.loads(c.experiments.get(identifier)['result'])
    assert result['completed']==1
    p={'research_rows':c.store.rows(),'action_history':[{'state':'done','request':{'op':'experiment'},'result':result}]}
    request={'title':'実験結果','target_goal':'','goal_status':'unknown','goal_evidence':[],
             'findings':[],'unknowns':['意味と目的は未確認'],'next_steps':[]}
    with pytest.raises(ValueError,match='Evaluate'):report(c.scientist,job,p,request)
    e=c.experiments.evaluate(kid,job=job)
    assert e['expected_probability'] is None
    assert e['status']=='inconclusive' and e['sessions']==[c.session]
    assert {r['code'] for r in e['reasons']}=={'not_preregistered','insufficient_samples'}
    assert e['experiments']==[identifier] and e['evidence']==[result['events'][0]['id']]
    assert c.store.db.execute('select count(*) from research_evaluations').fetchone()[0]==1
    saved=report(c.scientist,job,p,request)
    assert saved['evaluation_ids']==[e['evaluation_id']]
    doc=document(c.scientist,'report',saved['ref'])
    assert doc['evaluations'][0]['status']=='inconclusive'
    with c.store.lock,c.store.db:c.store.db.execute("update knowledge set validation='supported' where id=?",(kid,))
    assert document(c.scientist,'report',saved['ref'])['evaluations'][0]['status']=='inconclusive'
    assert 'not_preregistered' in render_report(doc)
    # A second pending plan must not erase the first evaluated chain's report.
    another=c.store.knowledge('hypothesis','別の未評価仮説','inferred','hypothesis',[c.last_eid])
    second=legacy_unregistered_plan(c,job,'2',{'action':1,'duration':10,'wait':10},another)
    c.step()
    p['action_history'].append({'state':'done','request':{'op':'experiment'},'result':json.loads(c.experiments.get(second)['result'])})
    partial=report(c.scientist,job,p,request)
    assert partial['pending_evaluations'][0]['hypothesis']==another
    assert '未評価' in render_report(document(c.scientist,'report',partial['ref']))
    # A newer evaluation must reach a durable report even if the LLM stops.
    c.experiments.evaluate(another,job=job)
    finish_report(c.scientist,job,p)
    terminal=p['action_history'][-1]
    assert terminal['origin']=='host_record_summary'
    doc=document(c.scientist,'report',terminal['result']['ref'])
    assert doc['producer']=='host_record_summary' and len(doc['evaluations'])==2
    assert 'Host集計' in render_report(doc)
    count=c.store.db.execute('select count(*) from research_reports').fetchone()[0]
    finish_report(c.scientist,job,p)
    assert c.store.db.execute('select count(*) from research_reports').fetchone()[0]==count
    other=c.store.job('research',{})
    assert c.experiments.evaluate(kid,.5,job=other)['n']==1


def test_progress_requires_experiment_and_evaluation():
    for key in ('new_evidence','analysis_methods','candidates','experiments','hypotheses_evaluated'):
        assert not made_progress({'progress':{key:1}})
    p={'progress':{'experiments':1,'hypotheses_evaluated':1},'action_history':[
        {'state':'done','request':{'op':'experiment'},'result':{'experiment':'x','hypothesis':4,'completed':1,'events':[{'id':8,'state':'complete'}]}},
        {'state':'done','request':{'op':'evaluate'},'result':{'hypothesis':5,'experiments':['x'],'evidence':[8]}}]}
    assert not made_progress(p)
    p['action_history'][-1]['result']['hypothesis']=4
    assert made_progress(p)


def test_pending_handoff_tracks_hypothesis_and_actual_evidence():
    p={'action_history':[{'state':'done','request':{'op':'experiment'},'result':
        {'experiment':'x','hypothesis':4,'completed':1,'events':[{'id':8,'state':'complete'}]}}]}
    assert any('evaluate' in s for s in loop_gaps(p))
    p['action_history'].append({'state':'done','request':{'op':'evaluate'},'result':{'hypothesis':5,'evidence':[8]}})
    assert any('evaluate' in s for s in loop_gaps(p))
    p['action_history'][-1]['result']['hypothesis']=4
    assert not any('evaluateが未実行' in s for s in loop_gaps(p))
    assert any('research_report' in s for s in loop_gaps(p))
