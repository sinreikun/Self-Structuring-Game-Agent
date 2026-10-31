import json
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import threading
import zipfile

import pytest
from PIL import Image

from selfstruct.config import Config, write_json, InstanceLock
from selfstruct.engine import Controller
from selfstruct.individuals import Individuals
from selfstruct.model_library import ModelLibrary, MissingModel, MANAGED_URL
from selfstruct.smoke import SyntheticGame


def test_append_keys_keeps_individual_checkpoint_revision_and_evidence(tmp_path):
    cfg=Config(root=str(tmp_path/'root'),keys=['A','B'],research_every=10000)
    first=Controller(cfg,SyntheticGame())
    for _ in range(4):first.step()
    identifier=first.config.agent_id;revision=first.agent_id;first.close()
    checkpoint=json.loads((first.experiment/'host'/'checkpoint.json').read_text())
    cfg=replace(first.config,keys=['A','B','ESC'],resume=True,mode='Experienced')
    class ExtendedGame(SyntheticGame):
        def act(self,key,*args,**kwargs):return super().act('B' if key=='ESC' else key,*args,**kwargs)
    resumed=Controller(cfg,ExtendedGame())
    try:
        assert resumed.config.agent_id==identifier and resumed.agent_id==revision
        assert resumed.store.counts()['experience']==4 and resumed.last_eid==checkpoint['event']
        assert resumed.scientist.keys==['A','B','ESC']
        resumed.step();assert resumed.store.counts()['experience']==5
    finally:resumed.close()
    repo=Individuals(cfg.root);assert repo.settings(identifier)['keys']==['A','B','ESC']
    assert len(repo.list())==1
    # A removed/reordered action must not reinterpret learned action numbers.
    for keys in (['B','A','ESC'],['A']):
        with pytest.raises(ValueError,match='入力番号'):Controller(replace(cfg,keys=keys),SyntheticGame())


def test_load_restores_individual_settings_preserves_common_settings_and_provenance(tmp_path):
    repo=Individuals(tmp_path)
    a=Config(root=str(tmp_path),keys=['A','ESC'],llm_context=4096,llm_backend='cpu',model_id='a'*64,game_exe='one.exe',research_every=42)
    aid=repo.create(a,name='One')
    b=replace(a,keys=['B'],game_exe='two.exe',model_id='b'*64,mode='Guided',channel='Development')
    bid=repo.create(b,name='Two',parent=aid)
    loaded=repo.load(aid,b)
    assert loaded.keys==a.keys and loaded.game_exe==a.game_exe and loaded.model_id==a.model_id
    assert loaded.llm_context==4096 and loaded.llm_backend=='cpu' and loaded.research_every==42
    assert loaded.channel=='Development' and loaded.root==str(tmp_path) and not loaded.auto_run
    assert loaded.agent_id==aid and repo.active()==aid
    assert repo.load(bid,a).mode=='Guided'
    with pytest.raises(ValueError,match='Guided'):repo.save_settings(bid,replace(b,mode='Experienced'))


def test_full_clone_export_delete_are_isolated_and_do_not_touch_shared_assets(tmp_path):
    root=tmp_path/'data';cfg=Config(root=str(root),keys=['A','B']);c=Controller(cfg,SyntheticGame())
    c.step();c.close();repo=Individuals(root);identifier=c.config.agent_id
    shared=root/'assets'/'model'/'shared'/'artifact';shared.parent.mkdir(parents=True);shared.write_bytes(b'GGUFshared')
    clone=repo.clone(identifier,cfg)
    assert repo.profile(clone)['parent']==identifier
    source=repo.path(identifier);target=repo.path(clone)
    assert (target/'host'/'checkpoint.json').read_bytes()==(source/'host'/'checkpoint.json').read_bytes()
    with closing(sqlite3.connect(source/'host'/'experience.sqlite3')) as db:original=db.execute('select action from events').fetchone()[0]
    with closing(sqlite3.connect(target/'host'/'experience.sqlite3')) as db:
        assert db.execute('select count(*) from events').fetchone()[0]==1
        db.execute("update events set action='ESC'");db.commit()
    with closing(sqlite3.connect(source/'host'/'experience.sqlite3')) as db:assert db.execute('select action from events').fetchone()[0]==original
    archive=tmp_path/'individual.zip';repo.export(clone,archive)
    with zipfile.ZipFile(archive) as z:
        assert 'profile.json' in z.namelist() and 'host/experience.sqlite3' in z.namelist()
        assert any(n.startswith('evidence/') for n in z.namelist())
        assert not any(n.startswith('assets/') for n in z.namelist())
        assert all(not n.endswith(('-wal','-shm')) for n in z.namelist())
    with pytest.raises(ValueError,match='ロード中'):repo.delete(identifier)
    repo.delete(clone);assert not target.exists() and shared.read_bytes()==b'GGUFshared'
    settings=repo.clone(identifier,cfg,settings_only=True)
    assert repo.settings(settings)['keys']==cfg.keys and not (repo.path(settings)/'host'/'experience.sqlite3').exists()


def test_legacy_migration_marks_unknown_settings_and_keeps_all_evidence(tmp_path):
    cfg=Config(root=str(tmp_path),keys=['A','B'])
    old=Controller(cfg,SyntheticGame());old.step();old.close()
    new=Controller(cfg,SyntheticGame());new.step();new.close()
    repo=Individuals(tmp_path)
    for c in (old,new):
        (c.experiment/'profile.json').unlink();(c.experiment/'settings.json').unlink()
    images={p:p.read_bytes() for p in tmp_path.glob('experiments/*/evidence/*')}
    repo.migrate(new.config)
    assert len(repo.list())==2 and all(p.read_bytes()==b for p,b in images.items())
    assert repo.profile(old.config.agent_id)['settings_review_required']
    with pytest.raises(ValueError,match='当時の設定'):repo.load(old.config.agent_id,cfg)
    repo.save_settings(old.config.agent_id,cfg,review=True)
    assert repo.load(old.config.agent_id,cfg).keys==['A','B']
    assert repo.settings(new.config.agent_id)['keys']==['A','B']


def test_operations_cannot_switch_or_clone_while_controller_owns_root(tmp_path):
    c=Controller(Config(root=str(tmp_path),keys=['A']),SyntheticGame());repo=Individuals(tmp_path)
    try:
        for operation in (lambda:repo.create(c.config),lambda:repo.load(c.config.agent_id,c.config),lambda:repo.clone(c.config.agent_id,c.config),lambda:repo.rename(c.config.agent_id,'changed')):
            with pytest.raises(ValueError,match='使用中'):operation()
    finally:c.close()


def model_fixture(root,identifier):
    artifact=root/'assets'/'model'/identifier/'artifact';artifact.parent.mkdir(parents=True);artifact.write_bytes(b'GGUFfixture')
    state={'model_sha256':identifier,'model':str(artifact),'engine':'engine','engine_sha256':'hash','backend':'cpu','model_info':{'name':'Fixture/Q4_K_M','size':11},'validation':{'minimal_inference':True}}
    lib=ModelLibrary(root);lib.register(state);return lib,state


def test_model_inventory_single_standby_switch_and_missing_model_no_substitution(tmp_path):
    lib,a=model_fixture(tmp_path,'a'*64);_,b=model_fixture(tmp_path,'b'*64)
    class Process:
        def poll(self):return None
    class Runtime:
        root=tmp_path;state_path=tmp_path/'host'/'llm.json';quota=1024**3
        process=None;active_state=None;current_info={};stops=0;starts=[]
        def stop(self):self.process=None;self.stops+=1
        def start(self,state,**kwargs):self.active_state=state;self.process=Process();self.starts.append(state);return True
    llm=Runtime();cfg=Config(root=str(tmp_path),model_id='a'*64,llm_context=4096)
    lib.prepare(llm,cfg,threading.Event());assert len(llm.starts)==1 and llm.active_state['context']==4096
    assert lib.status(a,llm)=='スタンバイ' and lib.status(a,llm,True)=='使用中'
    lib.prepare(llm,cfg,threading.Event());assert len(llm.starts)==1
    lib.prepare(llm,replace(cfg,model_id='b'*64),threading.Event());assert len(llm.starts)==2 and llm.stops==2
    with pytest.raises(MissingModel):lib.prepare(llm,replace(cfg,model_id='c'*64),threading.Event())
    assert llm.active_state['model_sha256']=='b'*64 and len(llm.starts)==2
    with pytest.raises(ValueError,match='スタンバイ中'):lib.delete('b'*64,llm)
    repo=Individuals(tmp_path);agent=repo.create(cfg)
    llm.stop();lib.delete('a'*64,llm)
    assert repo.path(agent).is_dir() and repo.settings(agent)['model_id']=='a'*64
    assert lib.get('b'*64)['model_sha256']=='b'*64


def test_delete_and_export_refuse_linked_individual_assets(tmp_path):
    repo=Individuals(tmp_path/'data');identifier=repo.create(Config(root=str(repo.root)))
    other=repo.create(Config(root=str(repo.root)))
    outside=tmp_path/'outside';outside.mkdir();(outside/'precious').write_text('keep')
    try:(repo.path(identifier)/'evidence').symlink_to(outside,target_is_directory=True)
    except OSError:pytest.skip('Symlinks unavailable')
    with pytest.raises(ValueError,match='リンク'):repo.delete(identifier)
    with pytest.raises(ValueError,match='リンク'):repo.export(identifier,tmp_path/'out.zip')
    assert (outside/'precious').read_text()=='keep'


@pytest.mark.skipif(__import__('os').name!='nt' and not __import__('os').environ.get('DISPLAY'),reason='Display required')
def test_gui_load_changes_settings_and_prepares_exact_model_without_manual_alignment(tmp_path,monkeypatch):
    import tkinter as tk
    import time
    from selfstruct import gui
    from selfstruct.model_library import ModelLibrary
    root=tmp_path/'data';repo=Individuals(root)
    _,state=model_fixture(root,'a'*64)
    a=Config(root=str(root),keys=['A','ESC'],model_id='a'*64,model_name='Fixture',game_exe='first.exe',llm_context=4096,quota_bytes=2*1024**3)
    aid=repo.create(a,name='Earlier')
    b=replace(a,keys=['B'],game_exe='second.exe',model_id='b'*64,llm_context=8192)
    bid=repo.create(b,name='Current')
    cfg=replace(b,agent_id=bid,resume=True,channel='Development')
    monkeypatch.setattr(gui,'default_root',lambda:tmp_path/'app')
    cfg.save(tmp_path/'app'/'host'/'settings.json')
    calls=[]
    monkeypatch.setattr(ModelLibrary,'prepare',lambda self,llm,config,cancel:calls.append(config) or config.model_id)
    window=tk.Tk();app=gui.App(window);errors=[];window.report_callback_exception=lambda *e:errors.append(e)
    try:
        assert [app.tabs.tab(t,'text') for t in app.tabs.tabs()]==['研究室','個体管理','設定','LLM管理','研究ノート','能力接続図','開発者詳細']
        app.individual_tree.selection_set(aid);app.load_individual()
        deadline=time.monotonic()+5
        while (not calls or app.busy) and time.monotonic()<deadline:window.update();time.sleep(.01)
        assert len(calls)==1 and calls[0].model_id=='a'*64
        assert app.fields['keys'].get()=='A,ESC'
        assert app.fields['game_exe'].get()=='first.exe' and app.fields['llm_context'].get()=='4096'
        assert app.config.channel=='Development' and app.config.agent_id==aid
        assert 'Earlier' in app.individual_name.get()
        app.fields['keys'].set('A,ESC,TAB');app.save_settings()
        assert repo.settings(aid)['keys']==['A','ESC','TAB']
        assert repo.settings(bid)['keys']==['B'] and repo.settings(bid)['model_id']=='b'*64
        assert not errors
    finally:app.closing=True;app.performance_monitor.stop.set();window.destroy()
