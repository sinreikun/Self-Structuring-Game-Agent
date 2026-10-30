import os
from pathlib import Path
import time

import pytest

from selfstruct.config import Config
from selfstruct.processes import ProcessInfo,ProcessWindow


@pytest.mark.skipif(os.name!='nt' and not os.environ.get('DISPLAY'),reason='Display required')
def test_process_picker_filters_selects_and_persists_attach_settings(tmp_path,monkeypatch):
    import tkinter as tk
    from selfstruct import gui,process_picker
    monkeypatch.setattr(gui,'default_root',lambda:tmp_path/'settings')
    config=Config(root=str(tmp_path/'data'),game_exe=str(tmp_path/'launcher.exe'))
    config.save(tmp_path/'settings'/'host'/'settings.json')
    selected=ProcessInfo(123,1,'game.exe',str(tmp_path/'game.exe'),456)
    other=ProcessInfo(124,1,'other.exe',str(tmp_path/'other.exe'),457)
    items=[ProcessWindow(selected,888,'ゲーム本体'),ProcessWindow(other,889,'別のアプリ')]
    monkeypatch.setattr(process_picker,'discover_targets',lambda:items)
    monkeypatch.setattr(process_picker,'process_info',lambda pid:selected if pid==123 else other)
    window=tk.Tk();app=gui.App(window)
    picker=None
    try:
        picker=process_picker.ProcessPicker(window,app.selected_target)
        deadline=time.monotonic()+5
        while len(picker.tree.get_children())<2 and time.monotonic()<deadline:window.update();time.sleep(.01)
        assert len(picker.tree.get_children())==2
        picker.search.set('ゲーム本体');window.update()
        assert picker.tree.get_children()==('0',)
        picker.tree.selection_set('0');picker.select()
        accepted=app.read_settings()
        assert accepted.connection_mode=='attach' and accepted.target_pid==123
        assert accepted.target_created==456 and accepted.target_hwnd==888
        accepted.validate()  # Launcher file need not exist when attaching.
        app.save_settings()
        assert Config.load(app.config_path).target_exe==selected.exe
        app.connection_method.set('ランチャーから起動')
        assert app.read_settings().connection_mode=='launcher'
    finally:
        if picker and not picker.closed:picker.close()
        app.closing=True;window.destroy()
