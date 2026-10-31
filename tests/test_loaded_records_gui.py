import json
import os
from pathlib import Path
import time
import zipfile

import pytest

from selfstruct.config import Config
from selfstruct.individuals import Individuals
from test_inspection import stored_individual


@pytest.mark.skipif(os.name!='nt' and not os.environ.get('DISPLAY'),reason='Display required')
def test_loaded_records_and_diagnostic_available_without_start_and_follow_selection(tmp_path,monkeypatch):
    import tkinter as tk
    from selfstruct import gui
    cfg,job,_=stored_individual(tmp_path)
    repo=Individuals(cfg.root);other=repo.create(cfg,name='個体B')
    cfg=repo.load(other,cfg)
    monkeypatch.setattr(gui,'default_root',lambda:tmp_path/'settings')
    cfg.save(tmp_path/'settings'/'host'/'settings.json')
    root=tk.Tk();errors=[];root.report_callback_exception=lambda *a:errors.append(a)
    app=gui.App(root)
    monkeypatch.setattr(app,'prepare_individual_llm',lambda:None)
    copied=[];monkeypatch.setattr('selfstruct.diagnostics.copy_file',lambda path,owner:copied.append(path))
    def until(condition):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            root.update()
            if condition():return
            time.sleep(.01)
        raise AssertionError(app.status.get())
    try:
        assert 'NEWBORN' not in app.control_buttons
        assert app.control_buttons['STOP'].cget('style')=='Stopped.TButton'
        identifier=job and next(p['id'] for p in repo.list() if p['id']!=other)
        app.individual_tree.selection_set(identifier);app.load_individual()
        assert app.controller is None
        app.tabs.select(app.book);app.note_category.set('仮説');app.refresh_text()
        assert '保存済みの個体Aの仮説' in app.book_text.get('1.0','end')
        app.tabs.select(app.capabilities);app.refresh_text()
        assert any(app.graph.type(i)=='rectangle' for i in app.graph.find_all())
        app.tabs.select(app.details);app.refresh_text()
        assert '(module' in app.raw_text.get('1.0','end')
        assert '保存済み記録' in app.record_scope.get()
        assert 'STOP前' in app.diagnostic_help.cget('text')
        root.geometry('900x650');app.tabs.select(app.home);root.update()
        assert int(app.diagnostic_help.cget('wraplength'))<=app.home.winfo_width()
        app.export()
        assert app.control_buttons['診断ZIPをコピー'].cget('style')=='Working.TButton'
        until(lambda:not app.busy)
        assert len(copied)==1 and app.control_buttons['診断ZIPをコピー'].cget('style')=='Copied.TButton'
        with zipfile.ZipFile(copied[0]) as z:
            header=json.loads(z.read('diagnosis.json'))
            assert header['config']['agent_id']==identifier
            assert header['gui']['loaded_individual']==identifier and header['gui']['snapshot_kind']=='saved'
        assert app.controller is None
        app.individual_tree.selection_set(other);app.load_individual()
        app.tabs.select(app.book);app.note_category.set('仮説');app.refresh_text()
        assert '保存済みの個体Aの仮説' not in app.book_text.get('1.0','end')
        app.tabs.select(app.details);app.refresh_text()
        assert '(module' not in app.raw_text.get('1.0','end') and 'まだAgent' in app.raw_text.get('1.0','end')
        app.tabs.select(app.capabilities);app.refresh_text()
        assert not any(app.graph.type(i)=='rectangle' for i in app.graph.find_all())
        assert errors==[]
    finally:
        app.closing=True;app.llm.stop();app.performance_monitor.stop.set();root.destroy()
