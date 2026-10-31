"""End-to-end protocol harness: real images and Player I/O, scripted LLM decisions."""
import json
import threading
import time
import zipfile

from PIL import Image
import pytest

from selfstruct.config import Config
from selfstruct.engine import Controller
from selfstruct.research import Scientist
from selfstruct.sandbox import Revisions
from selfstruct.smoke import SyntheticGame
from selfstruct.store import Store


class UnlabelledDynamics(SyntheticGame):
    """Hidden bit toggles on input; no game object/goal labels reach research."""
    def capture(self):return Image.new('RGB',(160,96),(231,39,72) if self.position%2 else (7,181,213))
    def act(self,key,duration,cancel,on_sent=None):
        assert key in ('A','B')
        if cancel.is_set():return
        if on_sent:on_sent(time.time())
        self.position+=1;self.held=False


@pytest.mark.parametrize('vision',[False,True])
def test_same_job_crop_compare_experiment_evaluate_analyzer_loop(tmp_path,vision):
    c=Controller(Config(root=str(tmp_path/'data'),keys=['A','B'],research_every=100000),UnlabelledDynamics())
    for _ in range(4):c.step()
    rows=c.store.rows();last=rows[-1]['id'];older=rows[0]['id']
    class Client:
        calls=[]
        def complete(self,prompt,**options):
            self.calls.append((prompt,options));n=len(self.calls)
            if n==1:
                assert '許可入力' in prompt and '"0":"A"' in prompt and '"1":"B"' in prompt
                return json.dumps({'actions':[{'op':'observe','event':last,'phase':'before','box':[80,0,160,48],'size':[128,64],'mode':'RGB','output':'image' if vision else 'pixels','limit':64}]})
            assert f'"job":"{job}"' in prompt and '追加観測結果' in prompt
            if vision:assert options.get('images') and all(i.startswith('data:image/png;base64,') for i in options['images'])
            else:assert 'images' not in options and ('pixels' in prompt or 'mean' in prompt)
            if n==2:
                assert '"box":[80,0,160,48]' in prompt and '"size":[128,64]' in prompt
                return json.dumps({'actions':[{'op':'compare','event':last,'phase':'before','compare':{'event':older,'phase':'after'},'box':[80,0,160,48],'size':[64,32],'output':'statistics'}]})
            if n==3:
                assert 'similarity' in prompt
                return json.dumps({'findings':[{'id':'h1','text':'同じ許可入力後に画像差分が再現するかを検証する。意味は不明。','action':1,'change_probability':1.0,'evidence':[last,older]}],
                    'actions':[{'op':'experiment','hypothesis':'h1','action':1,'duration':10,'wait':10,'repeat':8}]})
            if n==4:
                assert '"completed":8' in prompt
                return json.dumps({'actions':[{'op':'experiment','hypothesis':'h1','action':1,'duration':10,'wait':10,'repeat':8}]})
            if n==5:
                return json.dumps({'actions':[{'op':'evaluate','hypothesis':'h1','change_probability':1.0}]})
            if n==6:
                assert '"status":"supported"' in prompt
                return json.dumps({'actions':[{'op':'analyzer_create','definition':{'description':'領域の色統計','query':{'box':[80,0,160,48],'size':[64,32],'output':'statistics'}}}]})
            assert n==7
            return json.dumps({'findings':[],'actions':[],'wat':'','reason':'計画した観測と入力試験を完了。ゲーム意味は未確認。'})
    client=Client();client.vision=vision;c.scientist.client=client
    job=c.store.job('research',{});errors=[]
    def research():
        try:c.scientist.study(rows,job)
        except BaseException as error:errors.append(error)
    t=threading.Thread(target=research);t.start()
    deadline=time.monotonic()+15
    try:
        while t.is_alive() and time.monotonic()<deadline:c.step()
        t.join(1);assert not t.is_alive() and not errors,errors
        state=c.store.get_job(job);p=json.loads(state['payload'])
        assert state['state']=='complete' and p['outcome']=='research_progress'
        assert len(client.calls)==7 and p['research_calls']==7
        assert p['progress']['experiments']==2 and p['progress']['hypotheses_evaluated']==1
        assert p['progress']['analysis_methods']==1 and p['progress']['new_hypotheses']==1
        with c.store.lock:
            plans=c.store.db.execute('SELECT * FROM research_experiments').fetchall()
            hypothesis=c.store.db.execute('SELECT * FROM knowledge WHERE id=?',(p['hypotheses']['h1'],)).fetchone()
        assert len(plans)==2 and all(r['job']==job and r['state']=='complete' for r in plans)
        assert hypothesis['validation']=='supported'
        for plan in plans:
            result=json.loads(plan['result']);assert len(result['events'])==8
            assert all(r['action']=='B' and r['state']=='complete' and r['before_hash'] and r['after_hash'] for r in result['events'])
        assert c.visual.analyzers.list()[0]['status']=='candidate'
        assert 'base64' not in state['payload'] and 'data:image' not in state['payload']
        from selfstruct.diagnostics import create_diagnostics,diagnostic_job_payload
        safe=json.loads(diagnostic_job_payload(state['payload']))
        assert len(safe['action_history'])==6 and safe['research_calls']==7
        assert safe['action_history'][0]['request']['box']==[80,0,160,48]
        assert safe['action_history'][0]['returned_at'] and safe['action_history'][0]['step']==1
        assert 'pixels' not in safe['action_history'][0]['result']
        before=c.store.db.total_changes
        archive=create_diagnostics(c.config.root,store=c.store,config=c.config,activity=c.snapshot())
        assert c.store.db.total_changes==before
        with zipfile.ZipFile(archive) as z:assert b'research_experiments' in z.read('diagnostics.sqlite3')
        trace=c.snapshot().trace
        assert any(e.kind=='EVAL' for e in trace) and any(e.kind=='EXPERIMENT' for e in trace)
    finally:
        c.scientist.stop_event.set();t.join(2);c.close()


def test_natural_intent_redirects_without_host_choosing_action_and_deduplicates(tmp_path):
    c=Controller(Config(root=str(tmp_path),keys=['A']),UnlabelledDynamics());c.step();eid=c.last_eid
    request={'op':'observe','event':eid,'box':[0,0,80,48],'size':[32,32],'output':'statistics'}
    class Client:
        calls=0
        def complete(self,prompt,**options):
            self.calls+=1
            if self.calls==1:return json.dumps({'findings':[{'text':'高解像度で確認したい','evidence':[eid]}],'reason':'情報不足'})
            if self.calls==2:
                assert '追加観測・実験はまだ実行されていません' in prompt
                return json.dumps({'actions':[request,request]})
            return '{"findings":[],"actions":[],"wat":""}'
    c.scientist.client=Client();job=c.store.job('research',{})
    try:
        c.scientist.study(c.store.rows(),job);p=json.loads(c.store.get_job(job)['payload'])
        assert p['stagnation_detected'] and p['duplicate_requests']==1 and not p['stalled']
        assert p['action_history'][1]['state']=='duplicate' and p['action_history'][0]['state']=='done'
        assert p['research_calls']==3
    finally:c.close()


def test_limits_and_unresolved_intent_save_progress_instead_of_repair_loop(tmp_path,monkeypatch):
    from selfstruct import research_actions
    c=Controller(Config(root=str(tmp_path),keys=['A']),UnlabelledDynamics());c.step();eid=c.last_eid
    class Client:
        calls=0
        def complete(self,prompt,**options):
            self.calls+=1
            return json.dumps({'findings':[{'text':'情報不足で高解像度を確認したい','evidence':[eid]}],'wat':''})
    c.scientist.client=Client();job=c.store.job('research',{})
    try:
        c.scientist.study(c.store.rows(),job);p=json.loads(c.store.get_job(job)['payload'])
        assert p['outcome']=='stalled' and p['research_calls']==2 and p['progress']['new_hypotheses']==1
        monkeypatch.setattr(research_actions,'MAX_CALLS',1)
        job=c.store.job('research',{});c.scientist.study(c.store.rows(),job);p=json.loads(c.store.get_job(job)['payload'])
        assert p['outcome']=='research_limit' and p['research_limit']=='calls' and p['research_calls']==1
    finally:c.close()


@pytest.mark.parametrize('plan',[{'action':2},{'action':-1},{'action':True},{'action':0,'duration':501},{'action':0,'wait':2001},{'action':0,'repeat':9},{'action':0,'condition':{'mean_min':float('nan')}}])
def test_experiment_rejects_inputs_outside_limits(tmp_path,plan):
    c=Controller(Config(root=str(tmp_path),keys=['A']),UnlabelledDynamics())
    try:
        with pytest.raises(ValueError):c.experiments.submit('job','token',plan)
        assert not c.store.db.execute('SELECT * FROM research_experiments').fetchall()
    finally:c.close()


def test_paused_experiment_never_sends_and_interrupted_plan_never_replays(tmp_path):
    game=UnlabelledDynamics();c=Controller(Config(root=str(tmp_path),keys=['A','B']),game)
    job=c.store.job('research',{});plan=c.experiments.submit(job,'s1',{'action':1,'repeat':2,'duration':10,'wait':10})
    try:
        c.pause();position=game.position;c.step()
        assert game.position==position and c.experiments.get(plan)['state']=='queued'
        c.paused.clear();c.cancel_input.clear()
        original=game.act
        def interrupted(*args,**kwargs):original(*args,**kwargs);c.pause()
        game.act=interrupted;c.step()
        row=c.experiments.get(plan)
        assert row['state']=='interrupted' and len(json.loads(row['events']))==1
        event=json.loads(row['events'])[0]
        assert c.store.db.execute('SELECT state FROM events WHERE id=?',(event,)).fetchone()[0]=='interrupted'
        assert c.experiments.submit(job,'s1',{'action':1,'repeat':2})==plan
        game.act=original;c.paused.clear();c.cancel_input.clear();c.step()
        assert len(json.loads(c.experiments.get(plan)['events']))==1
    finally:c.close()


def test_restart_discards_unsent_experiments_and_condition_wait_is_bounded(tmp_path):
    c=Controller(Config(root=str(tmp_path),keys=['A','B']),UnlabelledDynamics());cfg=c.config
    job=c.store.job('research',{});plan=c.experiments.submit(job,'s1',{'action':1})
    c.close();c=Controller(cfg,UnlabelledDynamics())
    try:
        assert c.experiments.get(plan)['state']=='interrupted'
        assert c.experiments.submit(job,'s1',{'action':1})==plan
        c.step();assert json.loads(c.experiments.get(plan)['events'])==[]
        pending=c.experiments.submit(job,'s2',{'action':1,'condition':{'mean_min':254,'max_wait_steps':2}})
        c.step();c.step()
        assert c.experiments.get(pending)['state']=='expired'
        assert json.loads(c.experiments.get(pending)['events'])==[]
    finally:c.close()


def test_candidate_has_separate_implementation_and_repair_prompts(tmp_path,monkeypatch):
    c=Controller(Config(root=str(tmp_path),keys=['A','B']),UnlabelledDynamics())
    c.step();c.step();rows=c.store.rows();ids=[r['id'] for r in rows];seen=[]
    seed=c.revisions.source()
    class Client:
        calls=0
        def complete(self,prompt,**options):
            seen.append(prompt);self.calls+=1
            if self.calls==1:
                assert 'Agent ABI' not in prompt and seed not in prompt
                return json.dumps({'actions':[{'op':'candidate','problem':'変化条件の未確認','evidence':ids,'behavior':'画素に応じた予測を学ぶ','evaluation':'独立経験と定数基準で比較する'}]})
            assert '実装フェーズ' in prompt and 'Agent ABI' in prompt and seed in prompt
            return json.dumps({'wat':seed,'findings':[]})
    c.scientist.client=Client();job=c.store.job('research',{});submitted=[]
    monkeypatch.setattr(c.scientist,'evaluate_proposal',lambda source,after,jid:submitted.append((source,jid)))
    try:
        c.scientist.study(rows,job)
        assert submitted==[(seed,job)]
        p=json.loads(c.store.get_job(job)['payload']);p['repair_response']=json.dumps({'wat':seed});p['repair_error']='unknown local';p['research_calls']=0
        c.store.set_job(job,'pending',p)
        def repair(prompt,**options):
            assert '修正フェーズ' in prompt and 'unknown local' in prompt
            assert '画面そのものが主要Evidence' not in prompt and '画像添付' in prompt
            return json.dumps({'wat':seed,'findings':[]})
        c.scientist.client.complete=repair;c.scientist.study(rows,job)
        assert len(submitted)==2
    finally:c.close()


@pytest.mark.parametrize('budget',['time','images','actions'])
def test_each_research_budget_saves_truthful_terminal_outcome(tmp_path,monkeypatch,budget):
    from selfstruct import research_actions
    c=Controller(Config(root=str(tmp_path),keys=['A','B']),UnlabelledDynamics());c.step();eid=c.last_eid
    class Client:
        vision=budget=='images'
        calls=0
        def complete(self,prompt,**options):
            self.calls+=1
            return json.dumps({'actions':[{'op':'observe','event':eid,'size':[n,n],'output':'statistics'} for n in (32,64)]})
    client=Client();c.scientist.client=client;job=c.store.job('research',{})
    if budget=='time':monkeypatch.setattr(research_actions,'MAX_SECONDS',-1)
    elif budget=='images':monkeypatch.setattr(research_actions,'MAX_IMAGE_BYTES',1)
    else:monkeypatch.setattr(research_actions,'MAX_ACTIONS',1)
    try:
        c.scientist.study(c.store.rows(),job);p=json.loads(c.store.get_job(job)['payload'])
        assert c.store.get_job(job)['state']=='complete' and p['outcome']=='research_limit'
        assert p['research_limit']=={'time':'time','images':'image_bytes','actions':'actions'}[budget]
        assert client.calls==(1 if budget=='actions' else 0)
        assert len(p.get('action_history',[]))==(1 if budget=='actions' else 0)
    finally:c.close()


def test_diagnostic_preserves_every_action_and_excludes_nested_prompt_source_arrays():
    from selfstruct.diagnostics import diagnostic_job_payload
    history=[{'action_id':f'{i}:0','state':'done','step':i,'at':i,'returned_at':i+.5,'request':{'op':'compare','event':i,'box':[0,0,100,100],'size':[128,64],'compare':{'event':i-1,'prompt':'PRIVATE PROMPT','pixels':[17]*1000}},'result':{'evidence':{'event':i},'pixels':[23]*1000}} for i in range(24)]
    safe=diagnostic_job_payload(json.dumps({'action_history':history,'prompt':'PRIVATE PROMPT','source':'PRIVATE SOURCE','research_steps':[{'step':i,'consumed_actions':[f'{j}:0' for j in range(i)]} for i in range(10)]}))
    value=json.loads(safe)
    assert len(value['action_history'])==24 and len(value['research_steps'])==10
    assert value['action_history'][12]['action_id']=='12:0'
    assert 'PRIVATE' not in safe and 'pixels' not in safe


def test_hypothesis_refutation_requires_preregistered_probability_and_samples(tmp_path):
    c=Controller(Config(root=str(tmp_path),keys=['A','B']),UnlabelledDynamics());c.step()
    try:
        kid=c.store.knowledge('hypothesis','入力後の差分確率は0という仮説','inferred','hypothesis',[c.last_eid])
        c.store.claim(kid,'B',0,c.last_eid);job=c.store.job('research',{})
        plan=c.experiments.submit(job,'s1',{'action':1,'repeat':8,'duration':10,'wait':10},kid)
        c.step()
        assert c.experiments.evaluate(kid,0)['status']=='inconclusive'
        for _ in range(7):c.step()
        c.experiments.submit(job,'s2',{'action':1,'repeat':8,'duration':10,'wait':10},kid)
        for _ in range(8):c.step()
        assert c.experiments.evaluate(kid,1)['status']=='inconclusive'  # no post-hoc target
        result=c.experiments.evaluate(kid,0)
        assert result['status']=='refuted' and result['n']==16 and result['pre_registered']
    finally:c.close()


def test_research_time_budget_includes_candidate_wait_and_survives_restart(tmp_path,monkeypatch):
    c=Controller(Config(root=str(tmp_path),keys=['A','B']),UnlabelledDynamics());seed=c.revisions.source()
    job=c.store.job('research',{'research_started':time.time()-601,'research_calls':1,'progress':{'new_hypotheses':1}})
    try:
        monkeypatch.setattr('selfstruct.research.evaluate',lambda *a:pytest.fail('Expired candidate must not execute'))
        c.scientist.evaluate_proposal(seed,0,job)
        row=c.store.get_job(job);p=json.loads(row['payload'])
        assert row['state']=='complete' and p['outcome']=='research_limit' and p['research_limit']=='time'
        assert p['source']==seed and p['progress']['new_hypotheses']==1
        assert c.scientist.research_time_expired(p)
    finally:c.close()


def test_live_comparison_time_limit_saves_unconfirmed_candidate(tmp_path):
    c=Controller(Config(root=str(tmp_path),keys=['A','B']),UnlabelledDynamics())
    job=c.store.job('research',{'research_started':time.time()-601})
    try:
        c.scientist.live={'job':job,'revision':c.revisions.active,'metrics':{'base':{'samples':[]},'agent':{'samples':[]}}}
        c.scientist.expire_live_time()
        p=json.loads(c.store.get_job(job)['payload'])
        assert c.scientist.live is None and p['outcome']=='research_limit' and not p['implementation_verified']
        assert p['source']==c.revisions.source() and p['retained_live']['base']['samples']==[]
    finally:c.close()
