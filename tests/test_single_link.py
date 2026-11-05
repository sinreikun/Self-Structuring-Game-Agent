import json,time
import pytest
from selfstruct.config import Config
from selfstruct.engine import Controller
from selfstruct.smoke import SyntheticGame
from selfstruct.single_link import assign,context,finish,abandon
from selfstruct.job_agenda import calculate
from selfstruct.research_actions import run_actions,record_findings,action_budget_used
from selfstruct import transitions

@pytest.fixture
def world(tmp_path):
    c=Controller(Config(root=str(tmp_path),keys=['A'],research_every=100000),SyntheticGame())
    for _ in range(16):c.step()
    yield c
    c.close()

def hyp(c,text='model hypothesis'):
    return c.store.knowledge('hypothesis',text,'inferred','hypothesis',[c.last_eid])

def job(c,link):
    p={'assigned_link':link,'research_rows':c.store.rows(),'research_started':time.time(),'research_calls':1,'memory_mode':'pull','situation_candidates':[]}
    j=c.store.job('research',p);assign(c.scientist,p)
    return j,p

def claim(c,k,ids=(),j='previous'):
    c.store.claim(k,'A',.5,0)
    if ids:
        with c.store.db:
            c.store.db.execute('INSERT INTO research_experiments VALUES(?,?,?,?,?,?,?,?,?,?)',(str(k),j,k,json.dumps({'action':0,'repeat':len(ids)}),'complete',json.dumps(ids),'{}',0,0,0))

def test_a_fixed_selection_and_terminal_exclusion(world):
    c=world;a=hyp(c);b=hyp(c);done=hyp(c);judged=hyp(c);bare=hyp(c)
    ids=[r['id'] for r in c.store.rows()]
    claim(c,a,ids[:2]);claim(c,b,ids[:4]);claim(c,done,ids);claim(c,judged,ids[:3])
    with c.store.db:c.store.db.execute('INSERT INTO research_evaluations(job,hypothesis,result,created) VALUES(?,?,?,?)',('old',judged,'{}',0))
    p={};assert assign(c.scientist,p)['hypothesis_id']==b
    original=dict(p['assigned_link'])
    with c.store.db:c.store.db.execute('UPDATE research_experiments SET events=? WHERE hypothesis=?',(json.dumps(ids),b))
    assert assign(c.scientist,p)==original
    assert calculate(c.scientist,{})[0]['hypothesis_id']==a
    assert all(x.get('hypothesis_id') not in (done,judged,b) for x in calculate(c.scientist,{}))
    assert bare in [x.get('hypothesis_id') for x in calculate(c.scientist,{})]

def test_a_discovery_and_abandoned_rank_persist(world):
    c=world
    assert assign(c.scientist,{'situation_candidates':[]}) is None
    a=hyp(c);b=hyp(c);j,p=job(c,{'kind':'hypothesis','hypothesis_id':a})
    abandon(c.scientist,j,p,{'text':'model reason','evidence':[c.last_eid]})
    assert calculate(c.scientist,{})[0]['hypothesis_id']==b
    assert p['link_end']['text']=='model reason'
    assert c.store.db.execute('SELECT text FROM link_abandonments').fetchone()[0]=='model reason'

def test_b_outside_experiment_and_findings_claim_are_free(world):
    c=world;a=hyp(c);b=hyp(c);j,p=job(c,{'kind':'hypothesis','hypothesis_id':a})
    req={'op':'experiment','hypothesis':b,'action':0,'claim':{'type':'change_rate','probability':.5}}
    run_actions(c.scientist,j,p,[req])
    assert p['action_history'][-1]['result']['code']=='outside_assigned_link'
    assert action_budget_used(p)==0
    assert not c.store.db.execute('SELECT 1 FROM claims WHERE knowledge=?',(b,)).fetchone()
    assert not c.store.db.execute('SELECT 1 FROM research_experiments WHERE job=?',(j,)).fetchone()
    record_findings(c.scientist,j,p,{'findings':[{'id':'new','text':'new model hypothesis','evidence':[c.last_eid],'action':0,'change_probability':.5}]})
    k=p['hypotheses']['new']
    assert c.store.db.execute('SELECT 1 FROM knowledge WHERE id=?',(k,)).fetchone()
    assert not c.store.db.execute('SELECT 1 FROM claims WHERE knowledge=?',(k,)).fetchone()
    assert len(p['link_rejections'])==2 and action_budget_used(p)==0

def test_b_assigned_experiment_passes_and_closes(world,monkeypatch):
    c=world;a=hyp(c);j,p=job(c,{'kind':'hypothesis','hypothesis_id':a})
    original=c.experiments.submit
    def submit(*args,**kwargs):
        ident=original(*args,**kwargs);c.experiments.finish(ident,'expired');return ident
    monkeypatch.setattr(c.experiments,'submit',submit)
    run_actions(c.scientist,j,p,[{'op':'experiment','hypothesis':a,'action':0,'claim':{'type':'change_rate','probability':.5}}])
    assert p['action_history'][0]['state']=='done'
    assert action_budget_used(p)==1 and finish(c.scientist,j,p)
    assert p['link_end']['reason']=='link_closed'

def test_b_registration_route_outside_free(world):
    c=world;a=hyp(c);b=hyp(c);j,p=job(c,{'kind':'hypothesis','hypothesis_id':a})
    p['registration_routes']={'route':{'hypothesis':b,'claim':{}}}
    run_actions(c.scientist,j,p,[{'op':'registration','route':'route','decision':'complete'}])
    assert p['action_history'][0]['result']['code']=='outside_assigned_link'
    assert action_budget_used(p)==0

def test_c_database_context_resume_and_image_receipts(world):
    c=world;a=hyp(c);hyp(c,'UNRELATED_SECRET');j,p=job(c,{'kind':'hypothesis','hypothesis_id':a})
    p['visual_history']=[{'step':0,'result':{'pixels':['OLD_VISUAL_SECRET']}}]*200
    p['memory_results']=[]
    captured=[]
    class Client:
        vision=True
        def complete(self,prompt,**kwargs):
            captured.append(prompt)
            return json.dumps({'findings':[{'text':'model abandonment','evidence':[c.last_eid],'abandon_link':True}],'actions':[],'wat':''})
    c.scientist.client=Client()
    with c.store.db:c.store.db.execute('UPDATE knowledge SET text=? WHERE id=?',('CURRENT_DB_RECORD',a))
    c.scientist.visual_dialogue('UNRELATED_PROMPT_SECRET',j,p,2048,lambda _:None)
    assert len(captured)==1 and 'CURRENT_DB_RECORD' in captured[0]
    assert all(x not in captured[0] for x in ('UNRELATED_SECRET','OLD_VISUAL_SECRET','UNRELATED_PROMPT_SECRET'))
    saved=json.loads(c.store.get_job(j)['payload'])
    assert saved['image_delivery_totals']=={'count':0,'bytes':0}
    assert saved['research_steps'][-1]['image_delivery_status']=='no_attachment'
    assert len(saved['visual_history'])==200

def test_c_new_image_delivery_count_and_bytes(world):
    c=world;a=hyp(c);j,p=job(c,{'kind':'hypothesis','hypothesis_id':a})
    count=[]
    class Client:
        vision=True
        def complete(self,prompt,**kwargs):
            count.append(kwargs.get('images',[]))
            if len(count)==1:return json.dumps({'findings':[],'actions':[{'op':'observe','event':c.last_eid,'output':'image','mode':'RGB','size':[64,64]}],'wat':''})
            return json.dumps({'findings':[{'text':'image checked; abandon','evidence':[c.last_eid],'abandon_link':True}],'actions':[],'wat':''})
    c.scientist.client=Client()
    c.scientist.visual_dialogue('',j,p,2048,lambda _:None)
    assert count[0]==[] and len(count[1])==1
    assert p['image_delivery_totals']=={'count':1,'bytes':len(count[1][0].encode())}
    assert p['research_steps'][-1]['image_bytes']==len(count[1][0].encode())

def test_d_claim_sixteen_closes_before_model(world):
    c=world;a=hyp(c);ids=[r['id'] for r in c.store.rows()];claim(c,a,ids[:15])
    j,p=job(c,{'kind':'claim','hypothesis_id':a})
    assert not finish(c.scientist,j,p)
    with c.store.db:c.store.db.execute('UPDATE research_experiments SET events=? WHERE hypothesis=?',(json.dumps(ids),a))
    response=c.scientist.visual_dialogue('',j,p,2048,lambda _:None)
    assert json.loads(response)['wat']=='' and p['link_end']['reason']=='link_closed'
    assert p['research_calls']==1

def test_d_candidate_adoption_and_bundle_gate(world):
    c=world;k=hyp(c);j,p=job(c,{'kind':'situation_candidate','candidate_id':'sc-x'})
    p['situation_candidates']=[{'id':'sc-x','frames':[c.last_eid]}]
    assert not finish(c.scientist,j,p)
    record_findings(c.scientist,j,p,{'findings':[{'state':True,'candidate_id':'sc-x','text':'model state','evidence':[c.last_eid]}]})
    assert finish(c.scientist,j,p)

def test_d_abandon_study_finishes_once_and_saves_reason(world):
    c=world;a=hyp(c);j,p=job(c,{'kind':'hypothesis','hypothesis_id':a})
    c.store.set_job(j,'running',p)
    class Client:
        vision=False
        def complete(self,*args,**kwargs):return json.dumps({'findings':[{'text':'model reason remains unknown','evidence':[c.last_eid],'abandon_link':True}],'actions':[],'wat':''})
    c.scientist.client=Client();c.scientist.study(c.store.rows(),j)
    row=c.store.get_job(j);saved=json.loads(row['payload'])
    assert row['state']=='complete' and saved['outcome']=='abandoned'
    assert saved['link_end']['text']=='model reason remains unknown'
    assert saved['research_calls']==2


def test_b_assigned_findings_claim_registered_without_experiment(world):
    c=world;a=hyp(c);j,p=job(c,{'kind':'hypothesis','hypothesis_id':a})
    record_findings(c.scientist,j,p,{'findings':[{'id':'assigned','text':'model hypothesis','evidence':[c.last_eid],'action':0,'change_probability':.5}]})
    assert c.store.db.execute('SELECT 1 FROM claims WHERE knowledge=?',(a,)).fetchone()
    assert not finish(c.scientist,j,p)


def test_b_existing_outside_alias_claim_structured(world):
    c=world;a=hyp(c);b=hyp(c,'other hypothesis');j,p=job(c,{'kind':'hypothesis','hypothesis_id':a})
    p['hypotheses']['other']=b
    record_findings(c.scientist,j,p,{'findings':[{'id':'other','text':'other hypothesis','evidence':[c.last_eid],'action':0,'change_probability':.5}]})
    assert p['link_rejections'][-1]['code']=='outside_assigned_link'
    assert not c.store.db.execute('SELECT 1 FROM claims WHERE knowledge=?',(b,)).fetchone()


def test_d_candidate_claim_bundle_closes(world):
    c=world;j,p=job(c,{'kind':'situation_candidate','candidate_id':'sc-x'})
    p['situation_candidates']=[{'id':'sc-x','frames':[c.last_eid]}]
    record_findings(c.scientist,j,p,{'findings':[{'id':'new','text':'model transition','evidence':[c.last_eid],
        'transition':{'from_state':'sc-x','to_state':'sc-x','action':0,'probability':.5}}]})
    assert finish(c.scientist,j,p)
    assert c.store.db.execute('SELECT 1 FROM transition_claims WHERE knowledge=?',(p['hypotheses']['new'],)).fetchone()


def test_d_hypothesis_study_closes_after_submission(world,monkeypatch):
    c=world;a=hyp(c);j,p=job(c,{'kind':'hypothesis','hypothesis_id':a})
    c.store.set_job(j,'running',p)
    original=c.experiments.submit
    def submit(*args,**kwargs):
        ident=original(*args,**kwargs);c.experiments.finish(ident,'expired');return ident
    monkeypatch.setattr(c.experiments,'submit',submit)
    calls=[]
    class Client:
        vision=False
        def complete(self,*args,**kwargs):
            calls.append(1)
            return json.dumps({'findings':[],'actions':[{'op':'experiment','hypothesis':a,'action':0,'claim':{'type':'change_rate','probability':.5}}],'wat':''})
    c.scientist.client=Client();c.scientist.study(c.store.rows(),j)
    saved=json.loads(c.store.get_job(j)['payload'])
    assert c.store.get_job(j)['state']=='complete' and saved['outcome']=='link_closed'
    assert len(calls)==1


def test_a_discovery_remains_fixed_after_new_hypothesis(world):
    c=world;p={'situation_candidates':[]}
    assert assign(c.scientist,p) is None
    hyp(c)
    assert assign(c.scientist,p) is None and p['job_mode']=='discovery'
    assert assign(c.scientist,{})['kind']=='hypothesis'
