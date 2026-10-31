"""Regression coverage for the user's repeated WAT-failure diagnostics."""
import json
import sqlite3
import time
import zipfile
from pathlib import Path

from PIL import Image
import pytest

from selfstruct.research import Scientist
from selfstruct.sandbox import Revisions
from selfstruct.store import Store


@pytest.fixture
def research_world(tmp_path):
    store=Store(tmp_path,128*1024**2)
    session=store.session('Experienced',True)
    before=store.image(Image.new('RGB',(160,90),(18,27,36)))
    after=store.image(Image.new('RGB',(160,90),(54,63,72)))
    event=store.begin(session,before,'A',.01,1,'seed',1,0)
    store.finish(event,after,.1,.01)
    revisions=Revisions(tmp_path/'agents'/'revisions')
    yield store,revisions,event
    store.close()


def await_job(scientist,store,job):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline and store.get_job(job)['state']!='complete':
        scientist.tick();time.sleep(.01)
    assert store.get_job(job)['state']=='complete'


def test_invalid_wat_is_rejected_before_waiting_for_new_experiences(research_world):
    store,revisions,event=research_world
    scientist=Scientist(store,revisions,object(),['A'],10000,lambda _:None)
    class NoWaiting:
        def wait(self,_):pytest.fail('Malformed WAT must be rejected before collecting more experiences')
    scientist.stop_event=NoWaiting()
    source=revisions.source().replace('(i32.const -1)','(i32.shr (local.get $feature) (i32.const 12))')
    job=store.job('candidate',{'source':source,'after':event})
    active=revisions.active
    with pytest.raises(ValueError,match='unknown operator'):
        scientist.evaluate_proposal(source,event,job)
    assert json.loads(store.get_job(job)['payload'])['source']==source
    assert revisions.active==active and not store.reports()


def test_repeated_compile_error_finishes_job_without_adoption_and_keeps_original_draft(research_world):
    store,revisions,_=research_world
    source=revisions.source().replace('(i32.const -1)','(i32.shr (local.get $feature) (i32.const 12))')
    class BrokenCode:
        calls=0
        def complete(self,prompt,**kwargs):
            self.calls+=1
            if self.calls==2:
                assert 'unknown operator' in prompt and 'i32.shr' in prompt
            return json.dumps({'findings':[],'wat':source})
    client=BrokenCode();scientist=Scientist(store,revisions,client,['A'],10000,lambda _:None)
    scientist.automatic=False;scientist.retry_base_delay=.001
    job=store.job('research',{});active=revisions.active
    scientist.start()
    try:
        await_job(scientist,store,job)
        payload=json.loads(store.get_job(job)['payload'])
        assert client.calls==2 and payload['repair_failures']==2
        assert payload['outcome']=='repair_exhausted' and payload['implementation_verified'] is False
        assert source==json.loads(payload['repair_response'])['wat']
        assert revisions.active==active and not store.jobs()
        assert store.db.execute('SELECT count(*) FROM proposals WHERE job=?',(job,)).fetchone()[0]==2
        assert not store.db.execute('SELECT 1 FROM evidence_pins WHERE reason=?',('research-job-'+job,)).fetchone()
    finally:scientist.close()


def test_syntax_repair_limit_survives_restart_and_different_errors(tmp_path):
    store=Store(tmp_path,128*1024**2);revisions=Revisions(tmp_path/'agents')
    scientist=Scientist(store,revisions,object(),['A'],10000,lambda _:None)
    job=store.job('candidate',{'source':'preserve this invalid candidate'})
    assert scientist.repair_or_finish(job,ValueError('first compile error'))
    store.close()
    store=Store(tmp_path,128*1024**2)
    try:
        resumed=Scientist(store,revisions,object(),['A'],10000,lambda _:None)
        payload=json.loads(store.get_job(job)['payload'])
        assert not resumed.restore_repair_budget(job,payload)
        assert resumed.repair_or_finish(job,ValueError('different compile error'))
        assert not resumed.repair_or_finish(job,ValueError('third compile error'))
        payload=json.loads(store.get_job(job)['payload'])
        assert payload['repair_failures']==3 and payload['outcome']=='repair_exhausted'
        assert 'preserve this invalid candidate' in payload['repair_response']
        assert store.get_job(job)['state']=='complete'
    finally:store.close()


def test_legacy_62_failure_job_is_retired_without_repeating_inference(research_world):
    store,revisions,event=research_world
    class NoInference:
        def complete(self,*a,**k):pytest.fail('The diagnostic backlog must be retired before inference')
    job=store.job('research',{'repair_response':'preserved malformed WAT','research_rows':[{'id':event}]})
    error='WasmtimeError: expected valid module field at <anon>:39:6'
    for _ in range(62):store.report('candidate',False,'retained for repair',{'job':job,'error':error})
    store.pin([event],'research-job-'+job)
    scientist=Scientist(store,revisions,NoInference(),['A'],10000,lambda _:None);scientist.automatic=False
    scientist.start()
    try:
        await_job(scientist,store,job)
        payload=json.loads(store.get_job(job)['payload'])
        assert payload['repair_failures']==62 and payload['outcome']=='repair_exhausted'
        assert payload['repair_response']=='preserved malformed WAT'
        assert store.db.execute('SELECT count(*) FROM reports').fetchone()[0]==63
        assert not store.jobs()
        assert store.rows(1)[0]['id']==event
    finally:scientist.close()


def test_text_scientist_receives_actual_rgb_before_after_before_first_inference(research_world):
    store,revisions,event=research_world
    class Client:
        def complete(self,prompt,**kwargs):
            assert '"pixels":[18,27,36' in prompt and '"pixels":[54,63,72' in prompt
            assert '"phase":"before"' in prompt and '"phase":"after"' in prompt
            assert '"mode":"RGB"' in prompt and 'visual_requests' in prompt
            assert 'images' not in kwargs
            return '{"findings":[],"wat":""}'
    scientist=Scientist(store,revisions,Client(),['A'],10000,lambda _:None)
    job=store.job('research',{})
    scientist.study(store.rows(),job)
    payload=json.loads(store.get_job(job)['payload'])
    assert payload['initial_visual_evidence'] and len(payload['visual_history'])==2
    assert all(entry['result']['evidence']['event']==event for entry in payload['visual_history'])
    assert store.get_job(job)['state']=='complete'


def test_diagnostic_exposes_repair_and_visual_progress_without_prompt_source_or_pixels(research_world,tmp_path):
    from selfstruct.diagnostics import create_diagnostics
    from selfstruct.vision import Eyes,VisualAccess
    store,_,event=research_world
    visual=VisualAccess(Eyes(store))
    visual.analyzers.create({'description':'試験用の色分布','query':{'mode':'RGB','output':'histogram'}})
    job=store.job('research',{'prompt':'PRIVATE PROMPT','source':'PRIVATE SOURCE','repair_failures':2,
        'same_error_failures':2,'initial_visual_evidence':True,'visual_history':[{'kind':'initial_evidence',
        'request':{'op':'observe','event':event,'phase':'before','size':[8,8],'mode':'RGB'},
        'result':{'pixels':['PRIVATE PIXELS']}}]})
    metadata=store.root/'host'/'model-capabilities.json';metadata.write_text('{"input":"text","status":"runtime_reported"}')
    path=create_diagnostics(store.root,store=store,destination=tmp_path/'diagnostic.zip')
    with zipfile.ZipFile(path) as z:
        dbpath=tmp_path/'export.sqlite3';dbpath.write_bytes(z.read('diagnostics.sqlite3'))
        db=sqlite3.connect(dbpath)
        payload=json.loads(db.execute('SELECT payload FROM jobs WHERE id=?',(job,)).fetchone()[0])
        assert payload['repair_failures']==2 and payload['visual_request_count']==1
        assert payload['visual_requests'][0]['event']==event
        assert 'PRIVATE' not in json.dumps(payload)
        assert db.execute('SELECT count(*) FROM visual_analyzers').fetchone()[0]==1
        db.close()
        assert 'experiment/model-capabilities.json' in z.namelist()


def test_legacy_failure_yields_to_fresh_visual_research_without_resetting_individual(research_world):
    store,revisions,event=research_world
    first=store.rows(1)[0]
    def collect(n):
        ids=[]
        for _ in range(n):
            i=store.begin(first['session'],first['before_hash'],'A',.01,1,'seed',1,0)
            store.finish(i,first['after_hash'],.1,.01);ids.append(i)
        return ids
    collect(48)
    old=store.job('research',{'repair_response':'old invalid draft','research_rows':[first]})
    for _ in range(3):store.report('candidate',False,'old compile error',{'job':old,'error':'bad module'})
    class Client:
        calls=0
        def complete(self,prompt,**kw):
            self.calls+=1
            assert 'old invalid draft' not in prompt
            assert '"kind":"initial_evidence"' in prompt
            return '{"findings":[],"wat":""}'
    client=Client();scientist=Scientist(store,revisions,client,['A'],48,lambda _:None)
    active=revisions.active;scientist.start()
    try:
        await_job(scientist,store,old)
        assert client.calls==0
        new_ids=collect(48)
        deadline=time.monotonic()+5
        fresh=[]
        while time.monotonic()<deadline:
            scientist.tick()
            with store.lock:
                fresh=store.db.execute("SELECT * FROM jobs WHERE id<>? AND state='complete'",(old,)).fetchall()
            if fresh:break
            time.sleep(.01)
        assert len(fresh)==1 and client.calls==1
        payload=json.loads(fresh[0]['payload'])
        assert payload['visual_history'][0]['result']['evidence']['event']==new_ids[-1]
        assert payload['outcome']=='observations_only' and not payload['implementation_verified']
        assert revisions.active==active and store.counts()['improvements']==0
    finally:scientist.close()


def test_repeated_invalid_json_is_finite_and_does_not_restart_inference(research_world):
    store,revisions,_=research_world
    class Client:
        calls=0
        def complete(self,*a,**kw):self.calls+=1;return '{"findings":["not an object"],"wat":""}'
    client=Client();scientist=Scientist(store,revisions,client,['A'],10000,lambda _:None)
    scientist.automatic=False;scientist.retry_base_delay=.001
    recoveries=[];scientist.recover_llm=lambda:recoveries.append(True)
    job=store.job('research',{});scientist.start()
    try:
        await_job(scientist,store,job)
        assert client.calls==2 and recoveries==[]
        assert json.loads(store.get_job(job)['payload'])['outcome']=='repair_exhausted'
    finally:scientist.close()
