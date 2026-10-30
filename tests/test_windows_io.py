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
