import os
from pathlib import Path
import time

import pytest


@pytest.mark.skipif(os.name != 'nt' and not os.environ.get('DISPLAY'), reason='A display server is required')
def test_gui_start_pause_stop_newborn(tmp_path, monkeypatch):
    import tkinter as tk
    from selfstruct import gui
    from selfstruct.config import Config
    from selfstruct.engine import Controller
    from selfstruct.smoke import SyntheticGame

    monkeypatch.setattr(gui, 'default_root', lambda: tmp_path / 'settings')
    monkeypatch.setattr(gui, 'Controller', lambda config: Controller(config, SyntheticGame()))
    monkeypatch.setattr('selfstruct.research.LocalLLM.health', lambda self: None)
    monkeypatch.setattr('selfstruct.resources.ManagedLLM.start', lambda self: False)
    executable = tmp_path / 'game.exe'
    executable.touch()
    config = Config(root=str(tmp_path/'data'), game_exe=str(executable), keys=['A','B'], research_every=10000)
    config.save(tmp_path/'settings'/'host'/'settings.json')
    window = tk.Tk()
    errors = []
    window.report_callback_exception = lambda *args: errors.append(args)
    app = gui.App(window)
    original_keys=app.fields['keys'].get()
    app.fields['keys'].set('UP,DOWN,LEFT,RIGHT,Num0,Num1,Num2,Num3,Num4,Num5,Num6,Num7,Num8,Tab')
    accepted=app.read_settings().keys
    assert accepted[4:13]==[f'NUM{k}' for k in range(9)] and accepted[-1]=='TAB'
    app.fields['keys'].set(original_keys)
    def until(predicate, timeout=10):
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            window.update()
            if predicate():
                return
            time.sleep(.01)
        raise AssertionError('GUI operation timed out: '+app.status.get())
    try:
        app.start()
        until(lambda: app.controller and app.controller.steps >= 3)
        app.pause()
        assert app.controller.paused.is_set()
        assert not app.controller.game.held
        app.tabs.select(app.book)
        app.refresh_text()
        assert '研究ノート' in app.book_text.get('1.0','end')
        if os.environ.get('SELFSTRUCT_SCREENSHOT'):
            from PIL import ImageGrab
            window.update()
            ImageGrab.grab().save(os.environ['SELFSTRUCT_SCREENSHOT'])
        app.start()
        assert not app.controller.paused.is_set()
        app.stop()
        until(lambda: app.controller is None and not app.busy)
        app.newborn()
        assert not app.config.resume
        assert not errors
    finally:
        if app.controller:
            app.controller.close()
        app.llm.stop()
        app.closing = True
        window.destroy()
