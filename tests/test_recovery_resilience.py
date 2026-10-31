import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from selfstruct.config import Config
from selfstruct.engine import Controller
from selfstruct.smoke import SyntheticGame
from selfstruct.windows import GameUnavailable


def test_recovery_retries_beyond_old_limit_and_preserves_agent(tmp_path):
    class Game(SyntheticGame):
        failures=0
        def connect(self):raise GameUnavailable('closed')
        def restart(self):
            self.failures+=1
            if self.failures<8:raise GameUnavailable('launcher not ready')
            super().restart()
    game=Game();ctrl=Controller(Config(root=str(tmp_path),keys=['A'],max_restarts=1),game)
    ctrl.recovery_delay=.001;revision=ctrl.agent_id
    try:
        ctrl.recover_game('resumed')
        assert game.failures==8 and game.connected
        assert not ctrl.stopped.is_set() and not ctrl.paused.is_set()
        assert ctrl.agent_id==revision and ctrl.activity.snapshot().connection=='接続済み'
    finally:ctrl.close()


def test_stop_interrupts_recovery_backoff_without_launching_again(tmp_path):
    class Game(SyntheticGame):
        attempts=0
        def connect(self):raise GameUnavailable('closed')
        def restart(self):self.attempts+=1;raise GameUnavailable('offline')
    game=Game();ctrl=Controller(Config(root=str(tmp_path),keys=['A']),game)
    worker=threading.Thread(target=ctrl.recover_game,args=('resumed',))
    try:
        worker.start();deadline=time.monotonic()+2
        while not game.attempts and time.monotonic()<deadline:time.sleep(.01)
        ctrl.stop();worker.join(timeout=1)
        assert not worker.is_alive() and game.attempts==1
    finally:worker.join(timeout=2);ctrl.close()


def test_attach_finds_unique_new_generation_but_not_other_running_instance(monkeypatch):
    from selfstruct import windows
    from selfstruct.processes import ProcessInfo,ProcessWindow
    old=ProcessInfo(10,1,'game','C:/game.exe',100)
    other=ProcessInfo(11,1,'game','C:/game.exe',90)
    new=ProcessInfo(12,1,'game','C:/game.exe',200)
    monkeypatch.setattr(windows,'snapshot_processes',lambda:{11:other,12:new})
    monkeypatch.setattr(windows,'enumerate_windows',lambda p:[ProcessWindow(other,111,'other'),ProcessWindow(new,222,'game')])
    monkeypatch.setattr(windows,'LiveProcess',lambda info:SimpleNamespace(pid=info.pid,created=info.created,poll=lambda:None))
    game=object.__new__(windows.WindowsGame)
    game.mode='attach';game.target_pid=old.pid;game.target_created=old.created;game.target_exe=old.exe
    game.target_filter=old.exe;game.title='';game.preferred_hwnd=123;game.hwnd=123;game.root_process=None
    game.process=SimpleNamespace(pid=10,created=100,poll=lambda:1,close=lambda:None)
    game.generation=1;game.capturer=SimpleNamespace(close=lambda:None);game.release=lambda:None
    game.connect()
    assert game.target_pid==12 and game.hwnd==222 and game.generation==2
    another=ProcessInfo(13,1,'game','C:/game.exe',300)
    game.target_pid=10;game.target_created=100
    monkeypatch.setattr(windows,'snapshot_processes',lambda:{12:new,13:another})
    monkeypatch.setattr(windows,'enumerate_windows',lambda p:[ProcessWindow(new,222,'one'),ProcessWindow(another,333,'two')])
    with pytest.raises(GameUnavailable,match='複数'):game.connect()


def test_schema_mismatch_is_retryable_and_does_not_restart_runtime(tmp_path):
    from selfstruct.research import Scientist
    from selfstruct.store import Store
    from selfstruct.sandbox import Revisions
    store=Store(tmp_path,64*1024**2);revisions=Revisions(tmp_path/'agents')
    rows=[{'id':1,'action':'A','feature':1,'change':0}]
    class Client:
        calls=0
        def complete(self,*a,**kw):
            self.calls+=1
            return json.dumps({'findings':['wrong type'],'wat':''}) if self.calls==1 else '{"findings":[],"wat":""}'
    client=Client();scientist=Scientist(store,revisions,client,['A'],10000,lambda x:None)
    jid=store.job('research',{});scientist.retry_base_delay=.001
    try:
        from selfstruct.research import ResearchRepairNeeded
        with pytest.raises(ResearchRepairNeeded):scientist.study(rows,jid)
        pending=store.get_job(jid)
        assert pending['state']=='pending' and 'wrong type' in pending['payload']
        scientist.study(rows,jid)
        assert store.get_job(jid)['state']=='complete'
        assert store.db.execute('SELECT count(*) FROM proposals').fetchone()[0]==2
        assert not store.reports()
    finally:store.close()


def test_partial_generation_is_archived_and_repaired(tmp_path):
    from selfstruct.research import Scientist,IncompleteInference,ResearchRepairNeeded
    from selfstruct.store import Store
    from selfstruct.sandbox import Revisions
    store=Store(tmp_path,64*1024**2)
    class Client:
        last_response='{"findings":['
        calls=0
        def complete(self,*a,**kw):
            self.calls+=1
            if self.calls==1:raise IncompleteInference('length')
            return '{"findings":[],"wat":""}'
    client=Client();scientist=Scientist(store,Revisions(tmp_path/'agents'),client,['A'],10000,lambda x:None)
    jid=store.job('research',{});rows=[{'id':1,'action':'A','feature':1,'change':0}]
    try:
        with pytest.raises(ResearchRepairNeeded):scientist.study(rows,jid)
        assert store.get_job(jid)['state']=='pending'
        assert store.db.execute('SELECT response FROM proposals').fetchone()[0]==client.last_response
        scientist.study(rows,jid)
        assert store.get_job(jid)['state']=='complete'
    finally:store.close()


def test_managed_runtime_recovery_continues_after_four_failures(tmp_path):
    from selfstruct.research import Scientist
    from selfstruct.store import Store
    from selfstruct.sandbox import Revisions
    store=Store(tmp_path,64*1024**2)
    class Client:
        calls=0
        def complete(self,*a,**k):
            self.calls+=1
            if self.calls<=7:raise ConnectionError('runtime down')
            return 'resumed response'
    client=Client();scientist=Scientist(store,Revisions(tmp_path/'agents'),client,['A'],10000,lambda x:None)
    jid=store.job('agent_llm',{'prompt':'same task'});recoveries=[]
    scientist.recover_llm=lambda:recoveries.append(True);scientist.retry_base_delay=.001;scientist.start()
    try:
        deadline=time.monotonic()+4
        while time.monotonic()<deadline and store.get_job(jid)['state']!='complete':scientist.tick();time.sleep(.01)
        assert store.get_job(jid)['state']=='complete'
        assert len(recoveries)>=5 and client.calls==8
        assert json.loads(store.get_job(jid)['payload'])['response']=='resumed response'
    finally:scientist.close();store.close()


def test_digest_can_be_cancelled_without_deleting_model(tmp_path):
    from selfstruct.resources import Gateway
    model=tmp_path/'model';model.write_bytes(b'GGUF'+b'x'*10000)
    cancel=threading.Event();cancel.set()
    with pytest.raises(InterruptedError):Gateway.digest(model,cancel)
    assert model.exists()


def test_large_diagnostic_metrics_remain_valid_json():
    from selfstruct.diagnostics import diagnostic_json
    metrics={'baseline':{'loss':.25,'samples':[{'id':i,'loss':.25} for i in range(3000)]},'passed':True}
    output=json.loads(diagnostic_json(metrics))
    assert output['baseline']['loss']==.25 and output['passed']
    assert output['baseline']['samples_diagnostic_total']==3000
    assert len(output['baseline']['samples'])==8


def test_partial_job_survives_store_reopen(tmp_path):
    from selfstruct.research import Scientist,IncompleteInference,ResearchRepairNeeded
    from selfstruct.store import Store
    from selfstruct.sandbox import Revisions
    store=Store(tmp_path,64*1024**2)
    class Broken:
        last_response='{"findings":['
        def complete(self,*a,**k):raise IncompleteInference('length')
    revisions=Revisions(tmp_path/'agents');job=store.job('research',{})
    try:
        scientist=Scientist(store,revisions,Broken(),['A'],10000,lambda x:None)
        with pytest.raises(ResearchRepairNeeded):scientist.study([{'id':1,'action':'A','feature':1,'change':0}],job)
    finally:store.close()
    reopened=Store(tmp_path,64*1024**2)
    class Good:
        def complete(self,*a,**k):return '{"findings":[],"wat":""}'
    try:
        assert 'research_rows' in json.loads(reopened.get_job(job)['payload'])
        Scientist(reopened,revisions,Good(),['A'],10000,lambda x:None).study([],job)
        assert reopened.get_job(job)['state']=='complete'
        assert reopened.db.execute('SELECT count(*) FROM proposals').fetchone()[0]==2
    finally:reopened.close()


def test_game_recovery_suspends_live_candidate_without_losing_source(tmp_path):
    from selfstruct.research import Scientist
    from selfstruct.store import Store
    from selfstruct.sandbox import Revisions
    store=Store(tmp_path,64*1024**2);revisions=Revisions(tmp_path/'agents')
    scientist=Scientist(store,revisions,object(),['A'],10000,lambda x:None)
    job=store.job('research',{'research_rows':[]})
    scientist.live={'job':job,'revision':revisions.active,'metrics':{'base':{'samples':[]},'agent':{'samples':[]}}}
    try:
        scientist.suspend_trial('game closed',42)
        payload=json.loads(store.get_job(job)['payload'])
        assert store.get_job(job)['state']=='pending' and payload['after']==42
        assert payload['source']==revisions.source() and 'suspended_live' in payload
        assert scientist.live is None and not store.reports()
    finally:store.close()
