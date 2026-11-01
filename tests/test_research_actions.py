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
                assert '同じ許可入力後に画像差分が再現するかを検証する' in prompt
                assert '"preregistered":{"action":1,"change_probability":1.0,"after_id":' in prompt
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


def test_recorded_input_count_is_authoritative_even_if_plan_still_running(tmp_path):
    c=Controller(Config(root=str(tmp_path),keys=['A','B']),UnlabelledDynamics())
    job=c.store.job('research',{});plan=c.experiments.submit(job,'s1',{'action':1,'repeat':1,'duration':10,'wait':10})
    try:
        c.step()
        with c.store.lock,c.store.db:c.store.db.execute("UPDATE research_experiments SET state='running' WHERE id=?",(plan,))
        assert c.experiments.next(c.game.capture()) is None
        row=c.experiments.get(plan)
        assert row['state']=='complete' and len(json.loads(row['events']))==1
    finally:c.close()


def test_typed_schema_rejects_real_diagnostic_command_strings_and_constrains_ids():
    from selfstruct.research_schema import action_schema,validate,response_schema
    from selfstruct.research import PROPOSAL_SCHEMA
    malformed=['observe,event=25847,phase=diff,box=[100,100,300,300],size=[200,200],mode=RGB,output=pixels',
               'observe","event":26187,"phase":"before","box":[0,0,960,540],"size":"native","mode":"RGB","output":"image"},{',
               'compare:observe,phase=before,box=[0,0,1600,900]']
    for op in malformed:
        with pytest.raises(ValueError):validate(action_schema(2),{'op':op})
    validate(action_schema(2),{'op':'observe','event':25847,'phase':'diff','box':[100,100,300,300],'size':[200,200],'mode':'RGB','output':'pixels'})
    schema=response_schema(PROPOSAL_SCHEMA,['A','B'],{25847,26187})
    assert schema['properties']['findings']['items']['properties']['evidence']['items']['enum']==[25847,26187]
    with pytest.raises(ValueError):validate(schema,{'findings':[{'text':'仮説','evidence':[2]}],'problem':'','reason':'','wat':'','actions':[]})
    with pytest.raises(ValueError):validate(action_schema(2),{'op':'experiment','action':2})


@pytest.mark.parametrize('incomplete',[False,True])
def test_protocol_repair_keeps_observations_and_can_request_tools_without_wat(tmp_path,incomplete):
    from selfstruct.research import IncompleteInference,ResearchRepairNeeded
    c=Controller(Config(root=str(tmp_path),keys=['A','B']),UnlabelledDynamics());c.step();eid=c.last_eid
    class Client:
        calls=0
        last_response=''
        def complete(self,prompt,**options):
            self.calls+=1
            if self.calls==1:
                self.last_response='{"findings":[' if incomplete else json.dumps({'findings':[{'text':'不正な参照','evidence':[999999]}],'wat':''})
                if incomplete:raise IncompleteInference('partial')
                return self.last_response
            if self.calls==2:
                assert '研究JSONの形式修正' in prompt and 'Agent ABI:' not in prompt and '完全なモジュール' not in prompt
                assert f'"event":{eid}' in prompt and '追加観測結果' in prompt
                return json.dumps({'actions':[{'op':'observe','event':eid,'phase':'diff','size':[64,32],'output':'statistics'}]})
            assert '研究JSONの形式修正' not in prompt and 'mean' in prompt
            return '{"findings":[],"actions":[],"wat":""}'
    c.scientist.client=Client();job=c.store.job('research',{})
    try:
        with pytest.raises(ResearchRepairNeeded):c.scientist.study(c.store.rows(),job)
        p=json.loads(c.store.get_job(job)['payload']);assert p['repair_kind']=='protocol'
        c.scientist.study(c.store.rows(),job)
        p=json.loads(c.store.get_job(job)['payload'])
        assert c.store.get_job(job)['state']=='complete' and p['progress']['new_evidence']==1
        assert p['progress']['candidates']==0 and p['research_steps'][1]['phase']=='protocol_repair'
        assert 'repair_response' not in p
    finally:c.close()


def test_research_schema_requires_testable_condition_and_linked_hypothesis():
    from selfstruct.research_schema import action_schema,validate
    schema=action_schema(15)
    with pytest.raises(ValueError):validate(schema,{'op':'experiment','action':6,'hypothesis':'h1','condition':{'size':[8,8],'mode':'RGB'}})
    with pytest.raises(ValueError):validate(schema,{'op':'experiment','action':6})
    validate(schema,{'op':'experiment','action':6,'hypothesis':'h1'})
    validate(schema,{'op':'experiment','action':6,'hypothesis':'h1','condition':{'mean_min':1.5}})
    with pytest.raises(ValueError):validate(schema,{'op':'observe','event':27701,'observation':2})
    with pytest.raises(ValueError):validate(schema,{'op':'observe','event':27701,'fresh':True})
    with pytest.raises(ValueError):validate(schema,{'op':'compare','event':27701,'compare':{'event':27701,'observation':2}})
    validate(schema,{'op':'observe','observation':2,'phase':'before'})
    validate(schema,{'op':'observe','fresh':True})


def test_full_pixel_range_is_unconditional_for_preregistered_evaluation(tmp_path):
    c=Controller(Config(root=str(tmp_path),keys=['A','B']),UnlabelledDynamics());c.step()
    try:
        kid=c.store.knowledge('hypothesis','無条件の画像差分の確率','inferred','hypothesis',[c.last_eid]);c.store.claim(kid,'B',1,c.last_eid)
        job=c.store.job('research',{})
        for token in ('s1','s2'):
            c.experiments.submit(job,token,{'action':1,'repeat':8,'duration':10,'wait':10,'condition':{'mean_min':0,'mean_max':255}},kid)
            for _ in range(8):c.step()
        assert c.experiments.evaluate(kid,1)['status']=='supported'
        # A genuinely selective condition must not prove an unconditional claim.
        c.experiments.submit(job,'s3',{'action':1,'condition':{'mean_min':254}},kid)
        assert c.experiments.evaluate(kid,1)['status']=='inconclusive'
    finally:c.close()


def test_managed_context_compacts_old_results_without_mutating_the_job_journal(tmp_path,monkeypatch):
    from selfstruct.research import LocalLLM
    c=Controller(Config(root=str(tmp_path),keys=['A','B']),UnlabelledDynamics());c.step();eid=c.last_eid
    client=LocalLLM('http://127.0.0.1:8766/v1','fixture');client.context=8192
    monkeypatch.setattr(client,'health',lambda:None);monkeypatch.setattr(client,'count_tokens',lambda prompt:len(prompt)//2)
    calls=[]
    def complete(prompt,**options):
        calls.append(prompt)
        assert 'hashes' not in prompt and 'Older full result retained' in prompt
        assert options['max_tokens']>=512
        return '{"findings":[],"actions":[],"problem":"","reason":"","wat":""}'
    monkeypatch.setattr(client,'complete',complete);c.scientist.client=client
    history=[]
    for i in range(24):
        req={'op':'observe','event':eid,'phase':'before','box':[i,0,128,96],'size':[32,32],'mode':'RGB','output':'pixels','limit':128}
        result,_=c.visual.handle(req)
        history.append({'request':req,'result':result,'native':False})
    original=json.dumps(history,sort_keys=True)
    job=c.store.job('research',{'research_rows':c.store.rows(),'initial_visual_evidence':True,'visual_history':history,'research_calls':6})
    try:
        c.scientist.study(c.store.rows(),job);p=json.loads(c.store.get_job(job)['payload'])
        assert len(calls)==1 and c.store.get_job(job)['state']=='complete' and p['context_compactions']>=1
        assert json.dumps(p['visual_history'],sort_keys=True)==original
        assert len(p['visual_history'])==24 and 'pixels' in p['visual_history'][0]['result']
    finally:c.close()


def test_alias_conflict_preflight_creates_no_orphan_claims_or_rows(tmp_path):
    from selfstruct.research_actions import record_findings
    c=Controller(Config(root=str(tmp_path),keys=['A','B']),UnlabelledDynamics());c.step();eid=c.last_eid
    job=c.store.job('research',{});p={'research_rows':c.store.rows()}
    original={'id':'h1','text':'最初の主張','evidence':[eid],'action':1,'change_probability':1.0}
    try:
        record_findings(c.scientist,job,p,{'findings':[original]})
        kid=p['hypotheses']['h1'];before=c.store.db.execute('SELECT count(*) FROM knowledge').fetchone()[0]
        with pytest.raises(ValueError,match='cannot be rebound'):
            record_findings(c.scientist,job,p,{'findings':[{'id':'h2','text':'先に保存してはいけない主張','evidence':[eid]},dict(original,text='変更した主張',change_probability=0.0)]})
        assert c.store.db.execute('SELECT count(*) FROM knowledge').fetchone()[0]==before
        assert p['hypotheses']=={'h1':kid}
        assert c.store.db.execute('SELECT probability FROM claims WHERE knowledge=?',(kid,)).fetchone()[0]==1.0
        c.step();p['research_rows']=c.store.rows()
        record_findings(c.scientist,job,p,{'findings':[dict(original,evidence=[eid,c.last_eid])]})
        assert p['hypotheses']['h1']==kid and c.store.db.execute('SELECT count(*) FROM knowledge').fetchone()[0]==before
    finally:c.close()


def test_new_probability_is_a_new_claim_and_cannot_validate_against_existing_events(tmp_path):
    from selfstruct.research_actions import record_findings
    c=Controller(Config(root=str(tmp_path),keys=['A','B']),UnlabelledDynamics());c.step();eid=c.last_eid
    job=c.store.job('research',{});p={'research_rows':c.store.rows()}
    def finding(ref,prob):return {'id':ref,'text':'同一文章でも登録する確率は別の主張','evidence':[eid],'action':1,'change_probability':prob}
    try:
        record_findings(c.scientist,job,p,{'findings':[finding('r1_1',1)]})
        for _ in range(20):c.step()
        record_findings(c.scientist,job,p,{'findings':[finding('r2_1',0)]})
        a,b=p['hypotheses']['r1_1'],p['hypotheses']['r2_1']
        assert a!=b
        claim=c.store.db.execute('SELECT * FROM claims WHERE knowledge=?',(b,)).fetchone()
        assert claim['probability']==0 and claim['after_id']==c.last_eid
        c.store.validate_claims()
        assert c.store.db.execute('SELECT validation FROM knowledge WHERE id=?',(b,)).fetchone()[0]=='hypothesis'
    finally:c.close()


def test_generation_schema_issues_new_names_per_turn_and_keeps_old_references():
    from selfstruct.research_schema import response_schema,validate
    from selfstruct.research import PROPOSAL_SCHEMA
    schema=response_schema(PROPOSAL_SCHEMA,['A','B'],{1},['r2_1','r2_2'],{'h1':12})
    base={'findings':[],'actions':[],'problem':'','reason':'','wat':''}
    validate(schema,{**base,'findings':[{'id':'r2_1','text':'新しい主張','evidence':[1]}],'actions':[{'op':'experiment','hypothesis':'h1','action':1}]})
    with pytest.raises(ValueError):validate(schema,{**base,'findings':[{'id':'h1','text':'上書きする主張','evidence':[1]}]})
    with pytest.raises(ValueError):validate(schema,{**base,'actions':[{'op':'evaluate','hypothesis':'unissued','change_probability':.5}]})


def test_repeated_cached_observation_redirects_to_explicit_paging_without_choosing_for_llm(tmp_path):
    c=Controller(Config(root=str(tmp_path),keys=['A','B']),UnlabelledDynamics());c.step();eid=c.last_eid
    req={'op':'observe','event':eid,'size':[32,32],'mode':'RGB','output':'pixels','limit':64}
    class Client:
        calls=0
        def complete(self,prompt,**options):
            self.calls+=1
            if self.calls<=3:return json.dumps({'actions':[req]})
            if self.calls==4:
                assert 'next_offset' in prompt and '同一の結果は既に返却済み' in prompt
                return json.dumps({'actions':[{**req,'offset':64}]})
            return '{"findings":[],"actions":[],"wat":""}'
    c.scientist.client=Client();job=c.store.job('research',{})
    try:
        c.scientist.study(c.store.rows(),job);p=json.loads(c.store.get_job(job)['payload'])
        assert p['duplicate_redirects']==1 and p['duplicate_requests']==2
        assert [a['state'] for a in p['action_history']]==['done','duplicate','duplicate','done']
        assert p['action_history'][-1]['request']['offset']==64
    finally:c.close()


def test_inconclusive_evaluation_explains_missing_registration_and_sample_size(tmp_path):
    c=Controller(Config(root=str(tmp_path),keys=['A','B']),UnlabelledDynamics());c.step()
    try:
        kid=c.store.knowledge('hypothesis','まだ確率を登録していない仮説','inferred','hypothesis',[c.last_eid])
        job=c.store.job('research',{});c.experiments.submit(job,'s1',{'action':1,'repeat':1,'duration':10,'wait':10},kid);c.step()
        result=c.experiments.evaluate(kid,.5)
        assert result['status']=='inconclusive' and result['n']==1 and result['required_samples']==16
        assert {r['code'] for r in result['reasons']}=={'not_preregistered','insufficient_samples'}
        from selfstruct.research_actions import history_context
        context=history_context({'action_history':[{'action_id':'1:0','step':1,'state':'done','request':{'op':'evaluate','hypothesis':kid},'result':result}]})
        assert 'not_preregistered' in context and 'insufficient_samples' in context
    finally:c.close()


def test_experiment_targets_survive_new_hypotheses_and_observation_history(tmp_path):
    from selfstruct.research_actions import experiment_context,hypothesis_context,history_context
    c=Controller(Config(root=str(tmp_path),keys=['A','B']),UnlabelledDynamics());c.step()
    job=c.store.job('research',{})
    original=c.store.knowledge('hypothesis','実験後も評価する元の主張','inferred','hypothesis',[c.last_eid]);c.store.claim(original,'B',1,c.last_eid)
    aliases={'r1_1':original}
    try:
        c.experiments.submit(job,'s1',{'action':1,'repeat':1,'duration':10,'wait':10},original);c.step()
        experiment_event=c.last_eid
        for i in range(12):aliases[f'r{i+2}_1']=c.store.knowledge('hypothesis',f'後から追加した別の主張{i}','inferred','hypothesis',[experiment_event])
        p={'hypotheses':aliases,'action_history':[{'step':i,'state':'done','request':{'op':'observe','event':experiment_event},'result':{}} for i in range(12)]}
        p['experiment_context']=experiment_context(c.scientist,job)
        p['hypothesis_context']=hypothesis_context(c.scientist,p)
        assert original in {h['id'] for h in p['hypothesis_context']}
        assert next(h for h in p['hypothesis_context'] if h['id']==original)['text']=='実験後も評価する元の主張'
        summary=p['experiment_context'];assert summary['plans_used']==1 and summary['plans_remaining']==3
        assert summary['requested_inputs']==1 and summary['inputs_remaining']==31
        assert summary['experiments'][0]['evidence']==[experiment_event] and summary['experiments'][0]['completed']==1
        assert 'experiment_context' in history_context(p)
        c.pause();c.experiments.submit(job,'s2',{'action':0,'repeat':3},original)
        c.step();summary=experiment_context(c.scientist,job)
        assert summary['plans_used']==2 and summary['requested_inputs']==4
        assert summary['experiments'][1]['state']=='queued' and summary['experiments'][1]['completed']==0
        assert summary['experiments'][1]['evidence']==[]
    finally:c.close()


def test_experiment_context_separates_inputs_and_explains_unregistered_targets(tmp_path):
    from selfstruct.research_actions import experiment_context
    c=Controller(Config(root=str(tmp_path),keys=['A','B']),UnlabelledDynamics());c.step()
    job=c.store.job('research',{});kid=c.store.knowledge('hypothesis','画像変化についての未登録仮説','inferred','hypothesis',[c.last_eid])
    try:
        for i,action in enumerate((0,1)):
            c.experiments.submit(job,str(i),{'action':action,'repeat':1,'duration':10,'wait':10},kid);c.step()
        group=experiment_context(c.scientist,job)['hypotheses'][0]
        assert group['completed']==2
        assert {x['input']:x['completed'] for x in group['by_action']}=={'A':1,'B':1}
        requirement=group['evaluation_requirements']
        assert not requirement['preregistered'] and requirement['matching_post_registration_samples']==0
        assert set(requirement['blocking_reasons'])=={'not_preregistered','insufficient_samples'}
        assert c.store.db.execute('SELECT validation FROM knowledge WHERE id=?',(kid,)).fetchone()[0]=='inconclusive'
        assert c.store.db.execute('SELECT count(*) FROM claims WHERE knowledge=?',(kid,)).fetchone()[0]==0
    finally:c.close()


def test_last_call_experiment_is_in_final_diagnostic_context(tmp_path,monkeypatch):
    import selfstruct.research_actions as actions
    monkeypatch.setattr(actions,'MAX_CALLS',1)
    c=Controller(Config(root=str(tmp_path),keys=['A','B'],research_every=100000),UnlabelledDynamics());c.step();eid=c.last_eid
    class Client:
        def complete(self,prompt,**options):
            return json.dumps({'findings':[{'id':'r1_1','text':'入力後の変化確率を事前登録','evidence':[eid],'action':1,'change_probability':1}],
                'actions':[{'op':'experiment','hypothesis':'r1_1','action':1,'duration':10,'wait':10,'repeat':1}]})
    c.scientist.client=Client();job=c.store.job('research',{})
    thread=threading.Thread(target=lambda:c.scientist.study(c.store.rows(),job));thread.start()
    try:
        deadline=time.time()+10
        while thread.is_alive() and time.time()<deadline:c.step();time.sleep(.01)
        thread.join(1);assert not thread.is_alive()
        p=json.loads(c.store.get_job(job)['payload']);kid=p['hypotheses']['r1_1']
        assert p['research_limit']=='calls' and p['research_calls']==1
        summary=p['experiment_context'];assert summary['plans_used']==1 and summary['plans_remaining']==3
        assert summary['hypotheses'][0]['hypothesis']==kid and summary['hypotheses'][0]['completed']==1
        requirement=summary['hypotheses'][0]['evaluation_requirements']
        assert requirement['preregistered'] and requirement['matching_post_registration_samples']==1
        assert requirement['missing_samples']==15 and requirement['blocking_reasons']==['insufficient_samples']
        assert kid in {h['id'] for h in p['hypothesis_context']}
        assert p['progress']['hypotheses_evaluated']==0
        assert c.store.db.execute('SELECT validation FROM knowledge WHERE id=?',(kid,)).fetchone()[0]=='inconclusive'
    finally:c.close();thread.join(2)


@pytest.mark.parametrize('vision',[False,True])
def test_managed_context_with_four_experiments_preserves_targets_and_repair(tmp_path,monkeypatch,vision):
    from selfstruct.research import LocalLLM
    from selfstruct.research_actions import record_findings
    c=Controller(Config(root=str(tmp_path),keys=['A','B'],research_every=100000),UnlabelledDynamics());c.step();eid=c.last_eid
    job=c.store.job('research',{});p={'research_rows':c.store.rows(),'research_calls':6,'initial_visual_evidence':True}
    findings=[{'id':f'r{i}_1','text':f'実験対象{i}：'+('画像変化の再現性はまだ不明。'*14),'evidence':[eid]} for i in range(1,5)]
    record_findings(c.scientist,job,p,{'findings':findings})
    for i in range(1,5):
        c.experiments.submit(job,str(i),{'action':1,'repeat':1,'duration':10,'wait':10},p['hypotheses'][f'r{i}_1']);c.step()
    # Old observation arrays, multiple experimental targets and a partial response
    # coexist in the submitted diagnostic that exceeded the 8K model input.
    history=[]
    for i in range(12):
        request={'op':'observe','event':eid,'phase':'before','box':[i,0,128,96],'size':[32,32],'mode':'RGB','output':'pixels','limit':128}
        result,_=c.visual.handle(request)
        history.append({'request':request,'result':result,'native':vision})
    p['visual_history']=history
    p['repair_response']='{"findings":['+'x'*2900;p['repair_kind']='protocol';p['repair_error']='途中応答を保存しました。'
    c.store.set_job(job,'running',p)
    original=json.dumps(history,sort_keys=True)
    client=LocalLLM('http://127.0.0.1:8766/v1','fixture');client.context=8192;client.vision=vision
    monkeypatch.setattr(client,'health',lambda:None);monkeypatch.setattr(client,'count_tokens',lambda prompt:len(prompt)//2)
    calls=[]
    def complete(prompt,**options):
        calls.append(prompt)
        assert '研究JSONの形式修正' in prompt and 'not_preregistered' in prompt
        for finding in findings:assert finding['text'][:240] in prompt
        assert options['max_tokens']>=1024
        assert '"pixels":[7,181,213' in prompt or options.get('images')
        assert len(prompt)//2+options['max_tokens']+256+1024*len(options.get('images',[]))<=8192
        return '{"findings":[],"actions":[],"wat":""}'
    monkeypatch.setattr(client,'complete',complete);c.scientist.client=client
    try:
        c.scientist.study(c.store.rows(),job);final=json.loads(c.store.get_job(job)['payload'])
        assert len(calls)==1 and c.store.get_job(job)['state']=='complete'
        assert final['outcome']!='input_budget_exceeded' and final['research_calls']==7
        assert final['experiment_context']['plans_used']==4
        assert final['context_budget']['compact_history'] and final['context_budget']['omitted_visual_results']>0
        assert len(final['hypothesis_context'])==4
        from selfstruct.diagnostics import diagnostic_job_payload
        assert json.loads(diagnostic_job_payload(json.dumps(final)))['context_budget']==final['context_budget']
        assert json.dumps(final['visual_history'],sort_keys=True)==original
        assert len(final['experiment_context']['experiments'])==4
        assert all(g['evaluation_requirements']['blocking_reasons']==['not_preregistered','insufficient_samples'] for g in final['experiment_context']['hypotheses'])
    finally:c.close()
