from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from selfstruct.config import Config
from selfstruct.processes import ProcessInfo,enumerate_windows,process_info,track_descendants


def test_descendants_follow_exited_launcher_and_nested_child():
    known={10:100}
    snapshot={20:ProcessInfo(20,10,'starter','starter.exe',110),30:ProcessInfo(30,20,'game','game.exe',120),
              90:ProcessInfo(90,1,'other','other.exe',130)}
    track_descendants(snapshot,known,100)
    assert known=={10:100,20:110,30:120}
    track_descendants({40:ProcessInfo(40,20,'child','child.exe',140)},known,100)
    assert known[40]==140


def test_recycled_parent_pid_does_not_authorize_an_unrelated_process():
    known={10:100}
    track_descendants({10:ProcessInfo(10,1,'new','new.exe',200),
                       20:ProcessInfo(20,10,'other','other.exe',210)},known,100)
    assert known=={10:100}


def test_attach_settings_do_not_require_a_launcher_and_roundtrip(tmp_path):
    config=Config(root=str(tmp_path/'data'),game_exe=str(tmp_path/'missing.exe'),connection_mode='attach',
                  target_pid=123,target_created=456,target_exe=str(tmp_path/'game.exe'),target_hwnd=789)
    config.validate()
    path=tmp_path/'settings.json';config.save(path)
    assert Config.load(path)==config
    with pytest.raises(ValueError,match='選択'):
        replace(config,target_created=0).validate()
    with pytest.raises(ValueError,match='実行ファイル'):
        replace(config,connection_mode='launcher').validate()


def test_same_process_window_recreation_rebinds_without_restarting(monkeypatch):
    from types import SimpleNamespace
    from selfstruct import windows
    from selfstruct.processes import ProcessWindow
    info=ProcessInfo(123,1,'game','game.exe',456)
    monkeypatch.setattr(windows,'snapshot_processes',lambda:{123:info})
    monkeypatch.setattr(windows,'enumerate_windows',lambda processes:[ProcessWindow(info,222,'new window',100)])
    game=object.__new__(windows.WindowsGame)
    game.mode='attach';game.target_pid=123;game.target_created=456;game.target_exe='game.exe'
    game.target_filter='game.exe';game.title='';game.preferred_hwnd=111;game.hwnd=111
    game.process=SimpleNamespace(pid=123,created=456,poll=lambda:None)
    game.root_process=None;game.generation=1;game.last_input_target=111
    closed=[];game.capturer=SimpleNamespace(close=lambda:closed.append(True))
    game.connect()
    assert game.hwnd==222 and game.process.pid==123 and game.generation==1
    assert closed and game.last_input_target is None and not game.target_owned


def _wait_window(process):
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        info=process_info(process.pid)
        if info:
            windows=enumerate_windows({info.pid:info})
            if windows:return info,windows[0]
        if process.poll() is not None:raise AssertionError('Test window exited')
        time.sleep(.05)
    raise AssertionError('Test window did not appear')


@pytest.mark.skipif(os.name!='nt',reason='Windows process API')
def test_attach_existing_process_controls_only_selection_and_stop_preserves_it():
    from selfstruct.windows import WindowsGame
    from selfstruct.sandbox import difference
    external=subprocess.Popen([sys.executable,str(Path('tests/fixtures/message_window.py').resolve())])
    game=None
    try:
        info,window=_wait_window(external)
        game=WindowsGame('missing-launcher.exe',[],'',['A'],mode='attach',target_pid=info.pid,
                         target_created=info.created,target_exe=info.exe,target_hwnd=window.hwnd)
        game.start();before=game.capture()
        game.act('A',.03,threading.Event());time.sleep(.05)
        assert difference(before,game.capture())>.05
        assert game.root_process is None and game.process.pid==external.pid
        game.restart()
        assert external.poll() is None and game.process.pid==external.pid
        game.stop()
        assert external.poll() is None
    finally:
        if game:game.stop()
        external.terminate();external.wait(timeout=5)


@pytest.mark.skipif(os.name!='nt',reason='Windows process API')
def test_launcher_exit_adopts_real_child_and_does_not_touch_other_game():
    from selfstruct.windows import WindowsGame
    decoy=subprocess.Popen([sys.executable,str(Path('tests/fixtures/message_window.py').resolve())])
    game=WindowsGame(sys.executable,[str(Path('tests/fixtures/launcher.py').resolve())],
                     'SelfStruct Message Test',['A'],mode='launcher')
    try:
        _wait_window(decoy)
        game.start()
        assert game.process.pid not in {game.root_process.pid,decoy.pid}
        first=game.process.pid
        game.capture();game.act('A',.03,threading.Event())
        game.restart()
        assert game.process.pid!=first and decoy.poll() is None
        game.stop()
        assert decoy.poll() is None
    finally:
        game.stop();decoy.terminate();decoy.wait(timeout=5)


@pytest.mark.skipif(os.name!='nt',reason='Windows process API')
def test_reused_selected_pid_is_rejected_even_if_executable_matches():
    from selfstruct.windows import WindowsGame,GameUnavailable
    external=subprocess.Popen([sys.executable,str(Path('tests/fixtures/message_window.py').resolve())])
    game=None
    try:
        info,window=_wait_window(external)
        game=WindowsGame('',[],'',['A'],mode='attach',target_pid=info.pid,target_created=info.created+1,
                         target_exe=info.exe,target_hwnd=window.hwnd)
        with pytest.raises(GameUnavailable,match='選択'):
            game.connect()
        assert game.process is None
        game.stop();assert external.poll() is None
    finally:
        if game:game.stop()
        external.terminate();external.wait(timeout=5)


@pytest.mark.skipif(os.name!='nt',reason='Windows process API')
def test_controller_attach_collects_experience_and_detaches_without_killing(tmp_path):
    from selfstruct.engine import Controller
    external=subprocess.Popen([sys.executable,str(Path('tests/fixtures/message_window.py').resolve())])
    controller=None
    try:
        info,window=_wait_window(external)
        config=Config(root=str(tmp_path/'data'),connection_mode='attach',target_pid=info.pid,
                      target_created=info.created,target_exe=info.exe,target_hwnd=window.hwnd,
                      keys=['A'],research_every=1000000,quota_bytes=64*1024**2)
        controller=Controller(config)
        controller.start()
        deadline=time.monotonic()+15
        while controller.steps<3 and time.monotonic()<deadline and not controller.stopped.is_set():time.sleep(.05)
        controller.pause()
        assert controller.steps>=3,controller.error
        assert controller.game.process.pid==external.pid
        controller.close()
        assert external.poll() is None
    finally:
        if controller:controller.close()
        external.terminate();external.wait(timeout=5)
