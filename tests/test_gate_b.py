import json,time
import pytest
from selfstruct.config import Config
from selfstruct.engine import Controller
from selfstruct.smoke import SyntheticGame
from selfstruct.research_actions import record_findings,run_actions,action_budget_used,experiment_context,loop_gaps
from selfstruct.experiments import EvaluationRequired

@pytest.fixture
def world(tmp_path):
 c=Controller(Config(root=str(tmp_path),keys=['A','B'],research_every=100000),SyntheticGame());c.step()
 j=c.store.job('research',{});p={'research_rows':c.store.rows(),'research_started':time.time(),'research_calls':1}
 record_findings(c.scientist,j,p,{'findings':[{'id':'h','text':'差分の仮説','evidence':[c.last_eid],'action':0,'change_probability':0}]})
 k=p['hypotheses']['h']
 yield c,j,p,k
 c.close()

def collect(c,j,k,n=16,**extra):
 for i in range((n+7)//8):
  count=min(8,n-i*8)
  c.experiments.submit(j,str(k)+':'+str(i),{'action':0,'repeat':count,'duration':10,'wait':10,**extra},k)
  for _ in range(count):c.step()

def test_maturity_rejection_budget_priority_and_evaluation_release(world):
 c,j,p,k=world;collect(c,j,k)
 p['research_rows']=c.store.rows(64)
 before=experiment_context(c.scientist,j)
 for step,request in enumerate([{'op':'experiment','action':0},{'op':'experiment','hypothesis':999,'action':0}],2):
  p['research_calls']=step;run_actions(c.scientist,j,p,[request])
  assert p['action_history'][-1]['result']['reasons'][0]['code']=='evaluation_required'
 assert experiment_context(c.scientist,j)['requested_inputs']==before['requested_inputs']
 assert experiment_context(c.scientist,j)['plans_used']==2
 with pytest.raises(EvaluationRequired):c.experiments.submit(j,'direct',{'action':0})
 for name,claim in [('finding',{}),('claim',{'action':0,'change_probability':1})]:
  record_findings(c.scientist,j,p,{'findings':[{'id':name,'text':name,'evidence':[c.last_eid],**claim}]})
  assert name not in p['hypotheses']
 assert action_budget_used(p)==0 and len(p['action_history'])==2
 # Existing hypothesis and non-forward memory actions remain available.
 record_findings(c.scientist,j,p,{'findings':[{'id':'h','text':'差分の仮説','evidence':[c.last_eid]}]})
 p['research_calls']=4;run_actions(c.scientist,j,p,[{'op':'memory_note','title':'結果','body':'未確認','evidence':[c.last_eid]}])
 assert any('成熟した未評価' in x for x in loop_gaps(p))
 p['research_calls']=5;run_actions(c.scientist,j,p,[{'op':'evaluate','hypothesis':k}])
 e=p['action_history'][-1]['result'];assert e['pre_registered'] and e['n']==16
 assert action_budget_used(p)==2
 assert not c.experiments.mature_unevaluated(j)
 assert all(x['evaluated_after'] for x in p['evaluation_gate']['events'])
 assert p['evaluation_gate']['rejections']=={'experiment':2,'finding':1,'claim':1}
 saved=json.loads(c.store.get_job(j)['payload']);assert saved['evaluation_gate']==p['evaluation_gate']
 record_findings(c.scientist,j,p,{'findings':[{'id':'new','text':'次の主張','evidence':[c.last_eid],'action':0,'change_probability':1}]})
 assert 'new' in p['hypotheses']
 assert c.experiments.submit(j,'released',{'action':0})

def test_below_threshold(world):
 c,j,p,k=world;collect(c,j,k,15)
 assert not c.experiments.mature_unevaluated(j)
 assert c.experiments.submit(j,'below',{'action':0})

@pytest.mark.parametrize('corruption',['action','before','session','conditional'])
def test_same_evaluate_evidence_constraints(world,corruption):
 c,j,p,k=world;collect(c,j,k)
 with c.store.db:
  if corruption=='action':c.store.db.execute("update events set action='B' where id=(select max(id) from events)")
  if corruption=='before':c.store.db.execute('update claims set after_id=(select max(id) from events)')
  if corruption=='session':c.store.db.execute("insert into sessions values('other','Newborn',0,null)");c.store.db.execute("update events set session='other' where id=(select max(id) from events)")
  if corruption=='conditional':
   r=c.store.db.execute('select id,request from research_experiments limit 1').fetchone();v=json.loads(r['request']);v['condition']={'mean_min':1};c.store.db.execute('update research_experiments set request=? where id=?',(json.dumps(v),r['id']))
 assert not c.experiments.mature_unevaluated(j)
 assert c.experiments.evaluate(k,job=j)['status']=='inconclusive'


def test_multiple_mature_hypotheses_require_each_evaluation(world):
 c,j,p,k=world;collect(c,j,k)
 # Persist a second pre-existing plan set, as recovery can expose multiple mature hypotheses.
 other=c.store.knowledge('hypothesis','別の既存主張','inferred','hypothesis',[1])
 c.store.claim(other,'A',0,1)
 with c.store.db:
  for row in c.store.db.execute('select * from research_experiments').fetchall():
   c.store.db.execute('insert into research_experiments values(?,?,?,?,?,?,?,?,?,?)',
    ('other-'+row['id'],j,other,row['request'],row['state'],row['events'],row['result'],row['attempts'],row['created'],row['updated']))
 assert {x['hypothesis'] for x in c.experiments.mature_unevaluated(j)}=={k,other}
 p['research_calls']=2;run_actions(c.scientist,j,p,[{'op':'evaluate','hypothesis':k}])
 assert [x['hypothesis'] for x in c.experiments.mature_unevaluated(j)]==[other]
 with pytest.raises(EvaluationRequired):c.experiments.require_evaluation(j)
 p['research_calls']=3;run_actions(c.scientist,j,p,[{'op':'evaluate','hypothesis':other}])
 c.experiments.require_evaluation(j)


def test_rejection_model_view_is_bounded_and_db_audit_is_complete(world):
 from selfstruct.research_actions import history_context,HISTORY_VIEW_CHARS,experiment_context
 c,j,p,k=world;collect(c,j,k)
 p['research_rows']=c.store.rows(64)
 for step in range(2,12):
  p['research_calls']=step
  run_actions(c.scientist,j,p,[{'op':'experiment','hypothesis':k,'action':0}])
 # Findings rejected by gate B also have a full audit entry, independently of
 # the bounded action budget. Exercise a long persisted Job without input I/O.
 for step in range(12,212):
  p['research_calls']=step
  record_findings(c.scientist,j,p,{'findings':[{'id':f'rejected{step}','text':'未評価のまま次へ進む要求','evidence':[c.last_eid]}]})
 p['experiment_context']=experiment_context(c.scientist,j)
 c.store.set_job(j,'running',p)
 original=json.dumps(p,sort_keys=True)
 for compact in (False,True):
  text=history_context(p,compact)
  view=json.loads(text)
  assert len(text)<=HISTORY_VIEW_CHARS
  assert 'evaluate' in str(view['loop_gaps'])
  assert view['evaluation_gate']['latest'][0]['count']==210
  assert 'evaluation_required' in text
  assert 'events' not in view['evaluation_gate']
 assert json.dumps(p,sort_keys=True)==original
 saved=json.loads(c.store.get_job(j)['payload'])
 assert len(saved['evaluation_gate']['events'])==210
 assert len(saved['action_history'])==10
 assert saved['evaluation_gate']['rejections']=={'experiment':10,'finding':200}
 assert len(saved['experiment_context']['experiments'])==2


def test_visible_history_keeps_success_and_both_gate_guidance():
 from selfstruct.research_actions import history_context
 a={'action_id':'1:0','step':1,'request':{'op':'observe'},'state':'done','result':{'status':'complete'}}
 rejected={'action_id':'2:0','step':2,'request':{'op':'experiment'},'state':'error','result':{'state':'rejected','hypothesis':1,'reasons':[{'code':'not_preregistered','detail':'register action and probability'}]}}
 p={'action_history':[a,rejected], 'preregistration_gate':{'1':{'rejections':1}},'evaluation_gate':{'rejections':{'finding':1000},'events':[{'step':3,'pending':[{'hypothesis':2,'n':16}],'reasons':[{'code':'evaluation_required','detail':'evaluate first'}]}]*1000},'mature_unevaluated':[{'hypothesis':2,'n':16}]}
 view=json.loads(history_context(p))
 assert view['latest_success'][0]['request']['op']=='observe'
 assert 'not_preregistered' in str(view['latest_action_rejection'])
 assert view['evaluation_gate']['latest'][0]['count']==1000
 assert len(view['loop_gaps'])>=2
