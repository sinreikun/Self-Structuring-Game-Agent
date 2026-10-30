from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import random
import sqlite3
import threading
import time
import zipfile

from PIL import Image
import pytest

from selfstruct.activity import Activity
from selfstruct.capabilities import validate_description
from selfstruct.config import Config
from selfstruct.diagnostics import create_diagnostics,redact
from selfstruct.layout import fit_geometry,Layout
from selfstruct.store import Store,CapacityError


def add(store,sid,img,feature=1):
    digest=store.image(img)
    eid=store.begin(sid,digest,'A',.01,1,'seed',feature,-1)
    store.finish(eid,digest,0,.02)
    return eid,digest


def test_gc_protects_knowledge_pins_and_replay_but_compacts_old_repetition(tmp_path):
    store=Store(tmp_path,64*1024**2);sid=store.session('Newborn',False)
    rows=[]
    for i in range(400):
        image=Image.new('RGB',(24,24),(i%256,i//256,0))
        rows.append(add(store,sid,image))
    kid=store.knowledge('hypothesis','同条件を再確認します。','inferred','hypothesis',[rows[5][0]])
    store.pin([rows[6][0]],'evaluation')
    for p in (tmp_path/'evidence').glob('*'):os.utime(p,(1,1))
    before=store.usage(True)
    freed=store.collect(True)
    assert 400-len(list((tmp_path/'evidence').glob('*.png')))<=128
    freed+=store.collect(True)+store.collect(True)
    assert freed>0 and store.usage(True)<before+1024**2
    assert (tmp_path/'evidence'/(rows[5][1]+'.png')).exists()
    assert (tmp_path/'evidence'/(rows[6][1]+'.png')).exists()
    assert not (tmp_path/'evidence'/(rows[10][1]+'.png')).exists()
    assert len(store.rows())>=128
    assert store.db.execute('SELECT sum(count) FROM compacted').fetchone()[0]>0
    assert store.db.execute('SELECT count(*) FROM gc_history').fetchone()[0]>0
    store.close()


def test_emergency_reserve_and_pressure_continue_with_summarized_actions(tmp_path):
    store=Store(tmp_path,12*1024**2);sid=store.session('Newborn',False)
    rng=random.Random(42)
    for i in range(500):
        img=Image.frombytes('RGB',(96,96),rng.randbytes(96*96*3))
        add(store,sid,img)
        if i%16==0:
            for p in (tmp_path/'evidence').glob('*'):os.utime(p,(1,1))
    assert len(store.rows())>=48
    assert store.db.execute('SELECT count(*) FROM gc_history').fetchone()[0]>0
    # Exact boundary: ordinary evidence cannot take the last 5%; research can.
    store.close()
    # Uncollectible assets isolate reservation from the GC exercised above.
    store=Store(tmp_path/'boundary',64*1024**2)
    asset=store.root/'protected-model'
    with asset.open('wb') as f:f.truncate(8*1024**2)
    used=store.usage(True);store.quota=used+200000
    with pytest.raises(CapacityError):store.reserve(100000,critical=False)
    store.reserve(100000,critical=True)
    store.report('seed',False,'緊急診断を保存しました。',{'error':'test'})
    store.close()


def test_diagnostic_zip_bounded_redacted_and_excludes_assets(tmp_path):
    store=Store(tmp_path,64*1024**2);sid=store.session('Newborn',False)
    add(store,sid,Image.new('RGB',(16,16)))
    store.report('seed',False,'未確認です。',{'api_key':'secret','path':r'C:\Users\Alice\game'})
    (tmp_path/'assets').mkdir();(tmp_path/'assets'/'model.gguf').write_bytes(b'not included')
    (tmp_path/'host'/'host.log').write_text('Authorization=Bearer abcdef\nAPI_KEY=xyz\n'+r'C:\Users\Alice\game',encoding='utf-8')
    result=create_diagnostics(tmp_path,store,Config(root=str(tmp_path)),Activity().snapshot())
    assert result.parent==tmp_path/'exports'/'diagnostics'
    with zipfile.ZipFile(result) as z:
        assert 'diagnosis.json' in z.namelist() and 'diagnostics.sqlite3' in z.namelist()
        assert all('assets' not in n for n in z.namelist())
        assert sum(i.file_size for i in z.infolist())<=24*1024**2
        content=z.read('diagnosis.json')+z.read('host/host.log')
        assert b'Alice' not in content and b'xyz' not in content and b'abcdef' not in content
        dbpath=tmp_path/'check.sqlite3';dbpath.write_bytes(z.read('diagnostics.sqlite3'))
        with sqlite3.connect(dbpath) as db:
            assert 'secret' not in db.execute('SELECT metrics FROM reports').fetchone()[0]
    store.close()


def test_activity_immutable_snapshots_and_presentation():
    activity=Activity();old=activity.snapshot()
    activity.update(llm='推論中');activity.emit('llm_timeout')
    assert old.llm=='待機'
    assert activity.snapshot().events[-1].text.startswith('研究用LLMの応答')
    with pytest.raises(FrozenInstanceError):activity.snapshot().llm='bad'


def test_layout_detached_monitor_negative_monitor_and_corrupt_values():
    screens=[(0,0,1920,1080),(-1280,0,1280,1024)]
    assert fit_geometry(dict(width=1000,height=800,x=-1200,y=50),screens)==(1000,800,-1200,50)
    width,height,x,y=fit_geometry(dict(width=4000,height=2000,x=5000,y=5000),screens)
    assert x>=0 and y>=0 and width<=1920 and height<=1080
    assert fit_geometry({'width':'broken'},screens)[0]==1150


def test_capabilities_are_only_agent_metadata_not_permissions():
    graph=validate_description({'nodes':[dict(id='n',description='変化を抽出します',role='観測',uses=3)],'edges':[]},'r1')
    assert graph['source']=='agent_self_report' and graph['nodes'][0]['uses']==3
    with pytest.raises(ValueError):validate_description({'nodes':[],'edges':[['host','shell']]},'r1')


@pytest.mark.skipif(os.name!='nt',reason='Windows CF_HDROP')
def test_windows_clipboard_contains_file_drop_not_path_text(tmp_path):
    import ctypes as c
    from ctypes import wintypes as w
    from selfstruct.diagnostics import copy_file
    file=tmp_path/'診断.zip';file.write_bytes(b'zip')
    import tkinter as tk
    root=tk.Tk();root.withdraw()
    try:
        copy_file(file,root.winfo_id())
        u=c.WinDLL('user32');shell=c.WinDLL('shell32')
        u.GetClipboardData.argtypes=[w.UINT];u.GetClipboardData.restype=w.HANDLE
        shell.DragQueryFileW.argtypes=[w.HANDLE,w.UINT,w.LPWSTR,w.UINT]
        assert u.OpenClipboard(root.winfo_id())
        try:
            handle=u.GetClipboardData(15);assert handle
            assert shell.DragQueryFileW(handle,0xffffffff,None,0)==1
            buf=c.create_unicode_buffer(32768);shell.DragQueryFileW(handle,0,buf,len(buf))
            assert Path(buf.value)==file.resolve()
        finally:u.CloseClipboard()
    finally:root.destroy()


@pytest.mark.skipif(os.name!='nt' and not os.environ.get('DISPLAY'),reason='Display required')
def test_observatory_gui_diagnostics_layout_activity_and_no_full_knowledge_poll(tmp_path,monkeypatch):
    import tkinter as tk
    from selfstruct import gui
    from selfstruct.engine import Controller
    from selfstruct.smoke import SyntheticGame
    import selfstruct.diagnostics as diagnostics
    monkeypatch.setattr(gui,'default_root',lambda:tmp_path/'settings')
    monkeypatch.setattr(gui,'Controller',lambda config:Controller(config,SyntheticGame()))
    monkeypatch.setattr('selfstruct.research.LocalLLM.health',lambda self:None)
    monkeypatch.setattr('selfstruct.resources.ManagedLLM.start',lambda self:False)
    copied=[];monkeypatch.setattr(diagnostics,'copy_file',lambda path,owner:copied.append(path))
    exe=tmp_path/'game.exe';exe.touch()
    Config(root=str(tmp_path/'data'),game_exe=str(exe),keys=['A','B'],research_every=10000).save(tmp_path/'settings'/'host'/'settings.json')
    window=tk.Tk();app=gui.App(window);errors=[]
    window.report_callback_exception=lambda *a:errors.append(a)
    def until(predicate):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            window.update()
            if predicate():return
            time.sleep(.01)
        raise AssertionError(app.status.get())
    try:
        app.start();until(lambda:app.controller and app.controller.steps>=3)
        monkeypatch.setattr(app.controller.store,'knowledge_rows',lambda:(_ for _ in ()).throw(AssertionError('full scan')))
        app.poll();app.pause()
        app.export();until(lambda:bool(copied) and not app.busy)
        assert copied[0].parent==tmp_path/'data'/'exports'/'diagnostics'
        app.layout.save()
        saved=json.loads(app.layout.path.read_text())
        assert 'lab-console' in saved['elements'] and 'main' in saved['elements']
        assert app.llm_status.get().startswith('● LLM')
        assert not errors
    finally:
        if app.controller:app.controller.close()
        app.llm.stop();app.closing=True;window.destroy()


def test_ten_gib_budget_under_pressure_starts_research_and_gc(tmp_path):
    """Sparse accounting fixture: exercise a real 10GiB logical budget, not a 10GiB write."""
    from selfstruct.research import Scientist
    from selfstruct.sandbox import Revisions
    store=Store(tmp_path,10*1024**3)
    model=tmp_path/'assets'/'model'/'fixture';model.parent.mkdir(parents=True)
    try:
        with model.open('wb') as f:
            if os.name=='nt':
                import ctypes as c
                import msvcrt
                from ctypes import wintypes as w
                kernel=c.WinDLL('kernel32')
                kernel.DeviceIoControl.argtypes=[w.HANDLE,w.DWORD,c.c_void_p,w.DWORD,c.c_void_p,w.DWORD,c.POINTER(w.DWORD),c.c_void_p]
                returned=w.DWORD()
                if not kernel.DeviceIoControl(msvcrt.get_osfhandle(f.fileno()),0x900c4,None,0,None,0,c.byref(returned),None):raise OSError('Sparse not supported')
            f.truncate(int(store.quota*.94))
    except OSError:
        store.close();pytest.skip('Filesystem does not support sparse accounting fixture')
    sid=store.session('Newborn',False)
    for i in range(120):add(store,sid,Image.new('RGB',(32,32),(i,0,0)))
    activity=Activity();calls=[]
    class LLM:
        def complete(self,*a,**k):
            calls.append(activity.snapshot().llm)
            return '{"findings":[],"wat":""}'
    scientist=Scientist(store,Revisions(tmp_path/'agents'),LLM(),['A'],1000,lambda t:None,activity)
    store.collect(True)
    scientist.start();scientist.tick()
    try:
        deadline=time.monotonic()+5
        while not calls and time.monotonic()<deadline:time.sleep(.01)
        assert calls==['推論中']
    finally:scientist.close()
    assert activity.snapshot().llm=='待機'
    store.collect(True)
    assert store.storage_state=='積極的に自動整理'
    assert len(store.rows())>=48
    assert store.usage(True)<store.quota*.95
    store.close()


def test_capacity_cache_accounts_for_wal_growth_between_directory_scans(tmp_path):
    store=Store(tmp_path,64*1024**2)
    before=store.usage(True)
    with store.lock,store.db:
        store.db.execute('CREATE TABLE diagnostic_fixture(data BLOB)')
        store.db.execute('INSERT INTO diagnostic_fixture VALUES (?)',(b'x'*256*1024,))
    assert store.usage()>before+128*1024
    store.close()


def test_compaction_preserves_input_timing_revision_and_exact_image_context():
    row=dict(before_hash='a',after_hash='b',feature=257,action='A',duration=.01,
             revision='r1',process_generation=1,prediction=-1,change=.03)
    original=Store.condition_signature(row)
    for key,value in [('duration',.02),('revision','r2'),('before_hash','c'),('feature',1)]:
        assert Store.condition_signature(dict(row,**{key:value}))!=original


def test_gc_work_is_bounded_and_runs_before_byte_limit_for_many_small_frames(tmp_path):
    store=Store(tmp_path,64*1024**2);sid=store.session('Newborn',False)
    digest=store.image(Image.new('RGB',(16,16)))
    with store.db:
        store.db.executemany("""INSERT INTO events(session,before_hash,after_hash,action,duration,
            process_generation,revision,state,feature,change,prediction,sent) VALUES (?,?,?,'A',.01,1,'r','complete',1,0,-1,1)""",[(sid,digest,digest)]*5000)
    store.collect()
    count=store.db.execute("SELECT count(*) FROM events WHERE state='compacted'").fetchone()[0]
    assert count==512
    assert len(store.rows(128))==128
    store.close()


def test_gc_unlink_failure_keeps_live_references_consistent_and_lifetime_count(tmp_path,monkeypatch):
    store=Store(tmp_path,64*1024**2);sid=store.session('Newborn',False)
    for i in range(200):add(store,sid,Image.new('RGB',(16,16),(i,0,0)))
    for p in (tmp_path/'evidence').glob('*.png'):os.utime(p,(1,1))
    original=Path.unlink;attempts=[]
    def unlink(path,*args,**kwargs):
        if path.suffix=='.png':
            attempts.append(path)
            if len(attempts)==2:raise PermissionError('simulated sharing violation')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'unlink',unlink)
    store.collect(True)
    assert len(attempts)>2
    for row in store.rows():
        assert (tmp_path/'evidence'/(row['before_hash']+'.png')).exists()
        assert (tmp_path/'evidence'/(row['after_hash']+'.png')).exists()
    assert store.counts()['experience']==200
    store.close()


def test_storage_breakdown_separates_models_evidence_runtime_and_archives(tmp_path):
    store=Store(tmp_path,64*1024**2)
    for path,size in [('assets/model/hash/artifact',2000),('assets/engine/hash/artifact',1000),('assets/engine/hash/expanded/server.exe',3000)]:
        dest=tmp_path/path;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(b'x'*size)
    store.image(Image.new('RGB',(16,16)))
    result=store.storage_breakdown()
    assert result['models']==2000 and result['engine_archives']==1000 and result['engine_runtime']==3000
    assert result['evidence_images']>0 and result['database_and_wal']>0
    store.close()
