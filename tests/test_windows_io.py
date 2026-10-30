import os
from pathlib import Path
import sys
import threading
import time

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Requires Windows desktop APIs')
def test_real_capture_input_release_and_restart():
    from selfstruct.windows import WindowsGame
    from selfstruct.sandbox import difference
    game = WindowsGame(sys.executable, [str(Path('tests/fixtures/window_game.py').resolve())], 'SelfStruct I/O Test', ['A'])
    try:
        game.start()
        time.sleep(.3)
        before = game.capture()
        game.act('A', .05, threading.Event())
        time.sleep(.1)
        after = game.capture()
        assert difference(before,after) > .05
        assert not game.held
        with pytest.raises(ValueError):
            game.act('B', .05, threading.Event())
        generation = game.generation
        game.restart()
        assert game.generation == generation+1
        cancelled = threading.Event()
        cancelled.set()
        game.act('A', .2, cancelled)
        assert not game.held
    finally:
        game.stop()


@pytest.mark.skipif(os.name != 'nt', reason='Requires Windows desktop APIs')
def test_message_compatible_background_window_does_not_touch_foreground_app():
    import tkinter as tk
    from selfstruct.windows import WindowsGame
    from selfstruct.sandbox import difference
    # A toolkit that discards key messages on deactivation is not a supported
    # background-input consumer. Exercise the native message contract explicitly.
    game = WindowsGame(sys.executable,[str(Path('tests/fixtures/message_window.py').resolve())],
                       'SelfStruct Message Test',['A'])
    other = None
    try:
        game.start()
        time.sleep(.4)
        before = game.capture()
        other = tk.Tk()
        other.title('User foreground application')
        other.geometry('600x500+50+50')
        other.configure(bg='#10ee10')
        received=[]
        other.bind('<KeyPress>',lambda event:received.append(event.keysym))
        other.update()
        other.focus_force()
        other.update()
        foreground=game.user.GetForegroundWindow()
        assert foreground!=game.hwnd
        # The other window covers the game. Capturing desktop pixels would fail this test.
        game.act('A',.05,threading.Event())
        time.sleep(.1)
        other.update()
        after=game.capture()
        assert difference(before,after)>.05
        assert game.user.GetForegroundWindow()==foreground
        assert not received and not game.held
        # A user may return to the game, then leave it again; the agent still works.
        import ctypes
        from ctypes import wintypes
        activate = ctypes.WinDLL('user32').SetForegroundWindow
        activate.argtypes=[wintypes.HWND]
        activate(game.hwnd)
        game.act('A',.05,threading.Event())
        other.focus_force()
        other.update()
        game.act('A',.05,threading.Event())
        other.update()
        assert not received and not game.held
    finally:
        game.stop()
        if other:
            other.destroy()


@pytest.mark.skipif(os.name != 'nt', reason='Requires Windows desktop APIs')
def test_hung_background_renderer_has_bounded_capture_timeout():
    from selfstruct.windows import WindowsGame,GameUnavailable
    game = WindowsGame(sys.executable,[str(Path('tests/fixtures/window_game.py').resolve())],
                       'SelfStruct I/O Test',['H'])
    try:
        game.start()
        time.sleep(.4)
        game.capture()  # Warm the dedicated worker before deliberately hanging the window.
        game.capturer.timeout=.4
        game.act('H',.05,threading.Event())
        started=time.monotonic()
        with pytest.raises(GameUnavailable,match='応答'):
            game.capture()
        assert time.monotonic()-started<3
        assert game.capturer.process is None
    finally:
        game.stop()


@pytest.mark.skipif(os.name != 'nt', reason='Requires Windows desktop APIs')
def test_target_receives_numpad_virtual_keys_not_number_row():
    import ctypes
    from ctypes import wintypes
    from selfstruct.windows import WindowsGame
    game=WindowsGame(sys.executable,[str(Path('tests/fixtures/message_window.py').resolve())],
                     'SelfStruct Message Test',[f'NUM{k}' for k in range(10)])
    get_key=ctypes.WinDLL('user32').GetPropW
    get_key.argtypes=[wintypes.HWND,wintypes.LPCWSTR]
    get_key.restype=wintypes.HANDLE
    try:
        game.start()
        for number in range(10):
            game.act(f'NUM{number}',.02,threading.Event())
            deadline=time.monotonic()+1
            while get_key(game.hwnd,'SelfStructLastKey')!=0x60+number and time.monotonic()<deadline:
                time.sleep(.01)
            assert get_key(game.hwnd,'SelfStructLastKey')==0x60+number
            assert not game.held
    finally:
        game.stop()
