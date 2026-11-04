import json
import threading
from types import SimpleNamespace
import pytest
from selfstruct.config import Config
from selfstruct.engine import Controller
from selfstruct.smoke import SyntheticGame
from selfstruct.store import CapacityError


def records(c):
    with c.diagnostic_lock:
        rows = [json.loads(line) for line in (c.experiment/'host/diagnostics.jsonl').read_text(encoding='utf-8').splitlines()]
    return [r for r in rows if not r['kind'].startswith('store_lock_')]


@pytest.fixture
def controller(tmp_path):
    c = Controller(Config(root=str(tmp_path),keys=['A'],research_every=100000),SyntheticGame())
    yield c
    c.close()


def test_explicit_pause_once_and_existing_resume(controller):
    c=controller
    c.scientist.current_job='job-test';c.current_experiment='experiment-test'
    c.pause();c.pause()
    rows=[r for r in records(c) if r['kind']=='pause']
    assert len(rows)==1
    r=rows[0]
    assert r['reason']=='explicit' and r['pause_job_id']=='job-test'
    assert r['pause_experiment_id']=='experiment-test'
    assert r['flag_wall_time']>0 and r['flag_monotonic']>0 and r['progress_age']>=0
    assert r['thread']==threading.current_thread().name
    assert c.paused.is_set() and c.cancel_input.is_set() and c.halted_step()
    c.current_experiment=None
    c.thread=SimpleNamespace(is_alive=lambda:True,ident=None)
    c.start()
    assert not c.paused.is_set() and not c.cancel_input.is_set()
    c.pause();assert len([r for r in records(c) if r['kind']=='pause'])==2
    c.thread=None


@pytest.mark.parametrize('age,connected,recovering,paused',[(5,True,False,False),(5.001,True,False,False),(30,True,False,False),(30.001,True,False,True),(60,False,False,False),(60,True,True,False)])
def test_watchdog_thresholds(controller,monkeypatch,age,connected,recovering,paused):
    from selfstruct import engine
    c=controller
    c.activity.update(connection='接続済み' if connected else '未接続')
    if recovering:c.recovering.set()
    released=[];c.game.release=lambda:released.append(True)
    c.last_progress=100
    c.active_stage=("capture_before",100)
    c.step_stage="capture_before"
    monkeypatch.setattr(engine.time,'monotonic',lambda:100+age)
    calls=[]
    monkeypatch.setattr(c.stopped,'wait',lambda _:bool(calls) or calls.append(True) or False)
    c.watchdog()
    assert c.paused.is_set()==paused and c.cancel_input.is_set()==paused
    assert bool(released)==(age>5)
    if paused:
        r=records(c)[0];assert r['reason']=='watchdog' and r['progress_age']==pytest.approx(age)
    monkeypatch.undo()


def test_capacity_run_path(controller,monkeypatch):
    c=controller
    def capacity():raise CapacityError('capacity-test')
    monkeypatch.setattr(c,'step',capacity)
    original=c.notify
    def notify(text):original(text);c.stopped.set()
    monkeypatch.setattr(c,'notify',notify)
    c.run()
    r=next(r for r in records(c) if r['kind']=='pause')
    assert r['reason']=='capacity' and c.paused.is_set() and c.cancel_input.is_set()


def test_stage_timings_and_error_cleanup(controller,monkeypatch):
    c=controller;c.step()
    rows=records(c)
    for stage in ('step','capture_before','image_before','store_finish','grow_before','grow_after','diff_store','learn','trial_result'):
        starts=[r for r in rows if r['kind']=='stage_start' and r['stage']==stage]
        ends=[r for r in rows if r['kind']=='stage_end' and r['stage']==stage]
        assert len(starts)==len(ends)==1
        assert ends[0]['duration']>=0 and ends[0]['monotonic']>=starts[0]['monotonic']
    assert c.step_stage is None
    def broken():raise ValueError('capture-test')
    monkeypatch.setattr(c.game,'capture',broken)
    with pytest.raises(ValueError):c.step()
    assert c.step_stage is None
    assert records(c)[-1]['outcome']=='error'


def test_pause_records_active_stage_and_io_stack(controller):
    c=controller;c.thread=threading.current_thread()
    with c.timed_stage('store_finish'):c.pause('watchdog')
    r=next(r for r in records(c) if r['kind']=='pause')
    assert r['pause_stage']=='store_finish' and any('test_pause_records' in line for line in r['io_stack'])
    c.thread=None



def watchdog_once(c, monkeypatch):
    calls=[]
    monkeypatch.setattr(c.stopped,'wait',lambda _:bool(calls) or calls.append(True) or False)
    c.watchdog()


def test_completed_stages_allow_long_step(controller,monkeypatch):
    from selfstruct import engine
    c=controller;clock=[100.]
    c.activity.update(connection='接続済み')
    monkeypatch.setattr(engine.time,'monotonic',lambda:clock[0])
    with c.timed_stage('step'):
        for name in ('capture_before','store_finish','learn'):
            with c.timed_stage(name):
                clock[0]+=20
                watchdog_once(c,monkeypatch)
                assert not c.paused.is_set()
            assert c.last_progress==clock[0]
        assert clock[0]==160
        watchdog_once(c,monkeypatch)
        assert not c.paused.is_set()
    clock[0]+=60
    watchdog_once(c,monkeypatch)
    assert not c.paused.is_set() and c.active_stage is None


def test_unreturned_stage_records_timeout(controller,monkeypatch):
    from selfstruct import engine
    c=controller;clock=[100.]
    c.activity.update(connection='接続済み');c.last_progress=100
    c.scientist.current_job='running-research'
    monkeypatch.setattr(engine.time,'monotonic',lambda:clock[0])
    with c.timed_stage('capture_after'):
        clock[0]=130;watchdog_once(c,monkeypatch)
        assert not c.paused.is_set()
        clock[0]=130.001;watchdog_once(c,monkeypatch)
        assert c.paused.is_set()
    r=next(r for r in records(c) if r['kind']=='pause')
    assert r['reason']=='watchdog' and r['pause_stage']=='capture_after'
    assert r['active_stage']==['capture_after',100]


def test_baseline_internal_completions(controller,monkeypatch):
    from selfstruct import engine
    c=controller;clock=[100.];captures=[]
    c.activity.update(connection='接続済み')
    c.store.job('research',{})
    original=c.game.capture
    monkeypatch.setattr(engine.time,'monotonic',lambda:clock[0])
    def capture():
        captures.append(c.last_progress)
        clock[0]+=10
        watchdog_once(c,monkeypatch)
        assert not c.paused.is_set()
        return original()
    monkeypatch.setattr(c.game,'capture',capture)
    c.step()
    assert len(captures)==6 and captures[4]>captures[1]
    rows=[r for r in records(c) if r['kind']=='stage_end' and r['stage']=='baseline_capture']
    assert len(rows)==4 and all(r['last_progress']==r['monotonic'] for r in rows)
    assert not c.paused.is_set()


def test_failed_stage_is_not_progress(controller,monkeypatch):
    from selfstruct import engine
    c=controller;c.last_progress=100
    monkeypatch.setattr(engine.time,'monotonic',lambda:150)
    with pytest.raises(ValueError),c.timed_stage('capture_before'):
        raise ValueError('no response')
    assert c.last_progress==100 and c.active_stage is None
