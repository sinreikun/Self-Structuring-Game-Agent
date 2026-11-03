import json
import pytest
from test_gate_b import world,collect
from selfstruct.research_memory import finish_report
from selfstruct.research_actions import run_actions

@pytest.mark.parametrize('probability,status',[(.8,'refuted'),(0.,'supported')])
def test_terminal_same_arithmetic_and_report(world,probability,status):
 c,j,p,k=world
 with c.store.db:c.store.db.execute('update claims set probability=? where knowledge=?',(probability,k))
 collect(c,j,k)
 with c.store.db:c.store.db.execute('update events set change=0')
 manual=c.experiments.evaluate(k,job=j)
 with c.store.db:
  c.store.db.execute('delete from research_evaluations')
  c.store.db.execute("update knowledge set validation='testing' where id=?",(k,))
 finish_report(c.scientist,j,p)
 auto=json.loads(c.store.db.execute('select result from research_evaluations').fetchone()[0])
 for field in ['status','n','interval95','pre_registered','evidence','expected_probability','observed_probability']:
  assert auto[field]==manual[field]
 assert auto['status']==status and auto['executor']=='host_statistical_gate' and auto['automatic']
 row=c.store.db.execute('select * from research_reports').fetchone()
 assert json.loads(row['evaluations'])[0]['executor']=='host_statistical_gate'
 finish_report(c.scientist,j,p)
 assert c.store.db.execute('select count(*) from research_evaluations').fetchone()[0]==1
 assert p['progress']['hypotheses_evaluated']==1

@pytest.mark.parametrize('mode',['short','unregistered','manual','other_job'])
def test_terminal_exclusions(world,mode):
 c,j,p,k=world;collect(c,j,k,15 if mode=='short' else 16)
 if mode=='unregistered':
  with c.store.db:c.store.db.execute('delete from claims')
 if mode=='manual':
  p['research_rows']=c.store.rows(64)
  run_actions(c.scientist,j,p,[{'op':'evaluate','hypothesis':k}])
 if mode=='other_job':j=c.store.job('research',{})
 finish_report(c.scientist,j,p)
 rows=c.store.db.execute('select result from research_evaluations').fetchall()
 assert len(rows)==(1 if mode=='manual' else 0)
 if rows:
  result=json.loads(rows[0][0]);assert result['executor']=='scientist' and not result['automatic']
 assert not p.get('terminal_evaluations')

def test_terminal_sweeps_all_mature(world):
 c,j,p,k=world;collect(c,j,k)
 other=c.store.knowledge('hypothesis','別の主張','inferred','hypothesis',[1])
 c.store.claim(other,'A',0,1)
 with c.store.db:
  for row in c.store.db.execute('select * from research_experiments').fetchall():
   c.store.db.execute('insert into research_experiments values(?,?,?,?,?,?,?,?,?,?)',
    ('other-'+row['id'],j,other,row['request'],row['state'],row['events'],row['result'],row['attempts'],row['created'],row['updated']))
 finish_report(c.scientist,j,p)
 assert {r['hypothesis'] for r in p['terminal_evaluations']}=={k,other}
 assert len(json.loads(c.store.db.execute('select evaluations from research_reports').fetchone()[0]))==2
