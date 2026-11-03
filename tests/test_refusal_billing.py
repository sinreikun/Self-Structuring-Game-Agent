import json,time
import pytest
from selfstruct.config import Config
from selfstruct.engine import Controller
from selfstruct.smoke import SyntheticGame
from selfstruct.experiments import PreregistrationRequired,EvaluationRequired
from selfstruct.research_actions import run_actions,action_budget_used,record_findings
from selfstruct.research_memory import overview


@pytest.mark.parametrize('gate',['A','B'])
def test_rejected_only_turns_preserve_full_journal_and_budget(tmp_path,monkeypatch,gate):
 c=Controller(Config(root=str(tmp_path),keys=['A']),SyntheticGame());c.step()
 j=c.store.job('research',{});p={'research_started':time.time(),'research_rows':c.store.rows()}
 def reject(*args):
  if gate=='A':raise PreregistrationRequired(1)
  raise EvaluationRequired([{'hypothesis':1,'n':16,'required_samples':16}])
 monkeypatch.setattr('selfstruct.research_actions.execute',reject)
 try:
  for step in range(1,41):
   p['research_calls']=step
   run_actions(c.scientist,j,p,[{'op':'experiment','hypothesis':1,'action':0}])
   assert action_budget_used(p)==0
  saved=json.loads(c.store.get_job(j)['payload'])
  assert len(saved['action_history'])==40
  assert all(r['result']['state']=='rejected' for r in saved['action_history'])
  assert action_budget_used(saved)==0 and 'research_limit' not in saved
  assert overview(c.scientist,j,p)['research_budget']['actions_remaining']==24
  if gate=='A':assert saved['preregistration_gate']['1']['rejections']==40
  else:assert saved['evaluation_gate']['rejections']['experiment']==40
  # Replaying the same completed turn retains all records without re-execution.
  run_actions(c.scientist,j,p,[{'op':'experiment','hypothesis':1,'action':0}])
  assert len(p['action_history'])==40 and action_budget_used(p)==0
 finally:c.close()


def test_normal_duplicate_and_other_errors_remain_billable(tmp_path,monkeypatch):
 c=Controller(Config(root=str(tmp_path),keys=['A']),SyntheticGame());c.step()
 j=c.store.job('research',{});p={'research_started':time.time(),'research_rows':c.store.rows()}
 def execute(s,j,p,r,t):
  if r['op']=='invalid':raise ValueError('Other refusal')
  return {'ok':True},None,None
 monkeypatch.setattr('selfstruct.research_actions.execute',execute)
 try:
  for step in range(1,26):
   p['research_calls']=step
   run_actions(c.scientist,j,p,[{'op':'invalid' if step%2 else 'evaluate'}])
   assert action_budget_used(p)==min(step,24)
  assert p['research_limit']=='actions' and len(p['action_history'])==24
  assert any(r['state']=='duplicate' for r in p['action_history'])
  assert overview(c.scientist,j,p)['research_budget']['actions_remaining']==0
  assert len(json.loads(c.store.get_job(j)['payload'])['action_history'])==24
 finally:c.close()


