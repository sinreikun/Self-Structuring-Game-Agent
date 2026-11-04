import json
from types import SimpleNamespace
from PIL import Image
import pytest
from selfstruct.store import Store
from selfstruct.experiments import Experiments, PreregistrationRequired, EvaluationRequired
from selfstruct.transitions import register_state, register_claim, identify, representatives

def test_transition_pipeline(tmp_path):
    store=Store(tmp_path,100_000_000);sid=store.session('test',False)
    activity=SimpleNamespace(record=lambda *a:None)
    ex=Experiments(store,['A'],activity)
    def event(a,b):
        before=store.image(Image.new('RGB',(64,64),a));after=store.image(Image.new('RGB',(64,64),b))
        eid=store.begin(sid,before,'A',.06,1,'seed',42,-1);store.finish(eid,after,.5,.001);return eid
    e1=event('black','white');e2=event('white','black')
    a=register_state(store,'Scientist description A',[e1]);b=register_state(store,'Scientist description B',[e2])
    refs=representatives(store)
    assert identify(store,Image.new('RGB',(50,50),(5,5,5)),refs)==a
    assert identify(store,Image.new('RGB',(50,50),'white'),refs)==b
    assert identify(store,Image.new('RGB',(50,50),'gray'),refs) is None
    kid=store.knowledge('hypothesis','transition','inferred','hypothesis',[e1,e2])
    with pytest.raises(PreregistrationRequired):ex.submit('job','bad',{'action':0},kid)
    register_claim(store,kid,'A',{'from_state':a,'to_state':b,'probability':1})
    ids=[event('black','white') for _ in range(16)]+[event('white','black'),event('black','gray')]
    store.db.execute('INSERT INTO research_experiments VALUES(?,?,?,?,?,?,?,?,?,?)',('p','job',kid,json.dumps({'action':0,'repeat':8}),'complete',json.dumps(ids),'{}',0,1,1));store.db.commit()
    rows,_,_=ex.evaluation_samples(kid,'job');assert len(rows)==16 and all(r['success'] for r in rows)
    assert ex.mature_unevaluated('job')==[{'hypothesis':kid,'n':16,'required_samples':16}]
    with pytest.raises(EvaluationRequired):ex.require_evaluation('job')
    from selfstruct.research_memory import finish_report
    scientist=SimpleNamespace(store=store,experiments=ex,activity=activity)
    payload={}
    finish_report(scientist,'job',payload)
    result=payload['terminal_evaluations'][0]
    assert result['executor']=='host_statistical_gate' and result['automatic']
    assert result['status']=='supported' and result['n']==16 and result['claim_type']=='transition'
    assert ex.evaluate(kid,job='job')['evaluation_id']==result['evaluation_id']
    assert not ex.mature_unevaluated('job')
    from selfstruct.research_memory import finish_report
    scientist=SimpleNamespace(store=store,experiments=ex,activity=activity)
    finish_report(scientist,'job',{})
    saved=store.db.execute('SELECT evaluations,producer FROM research_reports WHERE job=?',('job',)).fetchone()
    assert json.loads(saved['evaluations'])[0]['claim_type']=='transition'
    assert saved['producer']=='host_record_summary'
    # Same gate and interval for the existing binary image-change claim.
    other=store.knowledge('hypothesis','change','inferred','hypothesis',[e1]);store.claim(other,'A',1,0)
    store.db.execute('INSERT INTO research_experiments VALUES(?,?,?,?,?,?,?,?,?,?)',('q','other',other,json.dumps({'action':0,'repeat':8}),'complete',json.dumps(ids[:16]),'{}',0,1,1));store.db.commit()
    assert ex.evaluate(other,job='other')['interval95']==result['interval95']
    # Later representatives cannot reclassify the registered claim's unknowns.
    gray=event('gray','gray');register_state(store,'later model',[gray])
    assert len(ex.evaluation_samples(kid,'job')[0])==16
    failed=store.knowledge('hypothesis','failed transition','inferred','hypothesis',[e1])
    register_claim(store,failed,'A',{'from_state':a,'to_state':b,'probability':1})
    failures=[event('black','black') for _ in range(16)]
    store.db.execute('INSERT INTO research_experiments VALUES(?,?,?,?,?,?,?,?,?,?)',('r','failed',failed,json.dumps({'action':0,'repeat':8}),'complete',json.dumps(failures),'{}',0,1,1));store.db.commit()
    verdict=ex.evaluate(failed,job='failed')
    assert verdict['n']==16 and verdict['observed_probability']==0 and verdict['status']=='refuted'


def test_findings_define_states_and_register_transition(tmp_path):
    from selfstruct.research_actions import record_findings, experiment_context
    store=Store(tmp_path,100_000_000);sid=store.session('test',False)
    h=store.image(Image.new('RGB',(64,64),'black'));w=store.image(Image.new('RGB',(64,64),'white'))
    eid=store.begin(sid,h,'A',.06,1,'seed',42,-1);store.finish(eid,w,.5,.001)
    activity=SimpleNamespace(record=lambda *a:None)
    ex=Experiments(store,['A'],activity)
    scientist=SimpleNamespace(store=store,keys=['A'],experiments=ex,activity=activity)
    payload={'research_rows':[{'id':eid}],'research_calls':1,'action_history':[{'state':'done','step':0,'request':{'op':'experiment'},'result':{'experiment':'prior','events':[{'id':eid,'state':'complete'}]}}]}
    findings=[{'id':'s1','state':True,'text':'model A','evidence':[eid]},{'id':'s2','state':True,'phase':'after','text':'model B','evidence':[eid]}]
    record_findings(scientist,'job',payload,{'findings':findings})
    record_findings(scientist,'job',payload,{'findings':findings})
    assert str(eid) in payload['feedback_statements']
    states=experiment_context(scientist,'job')['state_transitions']['states'];assert len(states)==2
    record_findings(scientist,'job',payload,{'findings':[{'id':'t','text':'prediction','evidence':[eid],'transition':{'from_state':states[0]['id'],'to_state':states[1]['id'],'action':0,'probability':1}}]})
    kid=payload['hypotheses']['t'];ex.require_preregistration(kid)
    assert not ex.mature_unevaluated('job')
    ctx=experiment_context(scientist,'job')['state_transitions']
    assert ctx['transition_claims'][0]['samples']==0
    assert ctx['recent_state_distribution'][str(states[1]['id'])]==1
