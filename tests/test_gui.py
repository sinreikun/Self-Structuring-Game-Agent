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
    monkeypatch.setattr('selfstruct.resources.ManagedLLM.start', lambda self,**kwargs: False)
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
        app.llm.current_info={"name":"Test/Model-GGUF","backend":"vulkan","gpu_layers":12,"total_layers":20,"status":"利用可能"}
        app.poll()
        assert "Test/Model-GGUF" in app.active_model.get() and "12/20層" in app.active_model.get()
        assert app.control_buttons['START'].cget('style')=='Running.TButton'
        app.pause()
        assert app.control_buttons['PAUSE'].cget('style')=='Paused.TButton'
        assert app.controller.paused.is_set()
        assert not app.controller.game.held
        event=app.controller.store.rows(1)[0]['id']
        app.controller.visual.handle({'event':event,'box':[0,0,32,32],'size':[64,64],'output':'statistics'})
        app.view_mode.set('AIが調べている画面')
        app.poll()
        assert f'経験 {event}' in app.visual_attention.get()
        assert any(app.preview.type(item)=='rectangle' for item in app.preview.find_all())
        # The same historic box must not be painted onto an unrelated live frame.
        app.view_mode.set('現在の画面');app.poll()
        assert not any(app.preview.type(item)=='rectangle' for item in app.preview.find_all())
        app.tabs.select(app.book)
        app.refresh_text()
        assert '研究ノート' in app.book_text.get('1.0','end')
        if os.environ.get('SELFSTRUCT_SCREENSHOT'):
            from PIL import ImageGrab
            window.update()
            app.tabs.select(app.home);window.update()
            ImageGrab.grab().save(os.environ['SELFSTRUCT_SCREENSHOT'])
        app.start()
        assert not app.controller.paused.is_set()
        assert app.control_buttons['START'].cget('style')=='Running.TButton'
        app.stop()
        until(lambda: app.controller is None and not app.busy)
        assert app.control_buttons['STOP'].cget('style')=='Stopped.TButton'
        app.tabs.select(app.details);app.refresh_text()
        assert '(module' in app.raw_text.get('1.0','end')
        previous_id=app.config.agent_id
        app.newborn()
        assert app.config.agent_id != previous_id and app.config.resume
        assert app.individuals.path(previous_id).is_dir()
        assert len(app.individuals.list())==2
        assert not errors
    finally:
        if app.controller:
            app.controller.close()
        app.llm.stop()
        app.performance_monitor.stop.set()
        app.closing = True
        window.destroy()
