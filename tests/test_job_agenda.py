import json
import pytest
from selfstruct.engine import Controller
from selfstruct.config import Config
from selfstruct.smoke import SyntheticGame
from selfstruct.job_agenda import calculate, prepend

@pytest.fixture
def world(tmp_path):
    c=Controller(Config(root=str(tmp_path),keys=['A'],research_every=100000),SyntheticGame())
    for _ in range(16):c.step()
    yield c
    c.close()

def hypothesis(c):
    return c.store.knowledge('hypothesis','model record','inferred','hypothesis',[c.last_eid])

def claim(c,k,n,evaluated=False):
    ids=[r['id'] for r in c.store.rows()][-n:] if n else []
    with c.store.db:
        c.store.db.execute('INSERT INTO claims VALUES(?,?,?,?,?)',(k,'A',.5,0,0))
        c.store.db.execute('INSERT INTO research_experiments VALUES(?,?,?,?,?,?,?,?,?,?)',
            (str(k),'prior-job',k,'{}','complete',json.dumps(ids),'{}',0,0,0))
        if evaluated:c.store.db.execute('INSERT INTO research_evaluations(job,hypothesis,result,created) VALUES(?,?,?,?)',('prior-job',k,json.dumps({'pre_registered':True,'n':16,'status':'supported'}),0))

def test_a_fixed_progress_order_and_completed_exclusion(world):
    c=world
    bare=hypothesis(c);one=hypothesis(c);three=hypothesis(c)
    done=hypothesis(c);mature=hypothesis(c)
    claim(c,one,1);claim(c,three,3);claim(c,done,3,True);claim(c,mature,16)
    result=calculate(c.scientist,{})
    assert result==[
        {'kind':'claim','hypothesis_id':three,'samples':3,'required_samples':16,'missing_samples':13},
        {'kind':'claim','hypothesis_id':one,'samples':1,'required_samples':16,'missing_samples':15},
        {'kind':'hypothesis','hypothesis_id':bare,'claim_count':0}]

def test_a_candidates_are_last_unadopted_and_counted(world):
    p={'situation_candidates':[{'id':'sc-1','frames':[1,2,3]}, {'id':'sc-2','frames':[1,2,3,4]}],
       'adopted_situation_candidates':{'sc-1':123}}
    assert calculate(world.scientist,p)==[{'kind':'situation_candidate','candidate_id':'sc-2','frame_count':4}]

def test_b_job_context_starts_with_facts(world):
    c=world;k=hypothesis(c);claim(c,k,3)
    captured=[]
    class Client:
        vision=False
        def complete(self,prompt,**kwargs):
            captured.append(prompt)
            return '{"findings":[],"actions":[],"wat":""}'
    c.scientist.client=Client()
    jid=c.store.job('research',{})
    c.scientist.study(c.store.rows(),jid)
    p=json.loads(c.store.get_job(jid)['payload'])
    assert captured and captured[0].startswith('Job agenda: ')
    first=json.loads(captured[0].splitlines()[0].removeprefix('Job agenda: '))
    assert first==p['job_agenda']
    assert first[0]=={'kind':'claim','hypothesis_id':k,'samples':3,'required_samples':16,'missing_samples':13}
    assert all(set(i)<= {'kind','hypothesis_id','samples','required_samples','missing_samples','claim_count','candidate_id','frame_count'} for i in first)

def test_c_empty_does_not_invent(world):
    assert calculate(world.scientist,{})==[]
    assert prepend('original context',[])=='original context'


def test_provisional_evaluation_keeps_claim_in_agenda(world):
    c=world;k=hypothesis(c);claim(c,k,3)
    with c.store.db:
        c.store.db.execute('INSERT INTO research_evaluations(job,hypothesis,result,created) VALUES(?,?,?,?)',
            ('prior-job',k,json.dumps({'pre_registered':True,'n':3,'status':'inconclusive'}),0))
    assert calculate(c.scientist,{})==[
        {'kind':'claim','hypothesis_id':k,'samples':3,'required_samples':16,'missing_samples':13}]


def test_calculate_without_experiments_returns_empty(world):
    class Bare:
        pass
    bare = Bare()
    bare.store = world.scientist.store
    assert calculate(bare, {}) == []
