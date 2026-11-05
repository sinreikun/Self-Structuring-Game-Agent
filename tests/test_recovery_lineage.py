"""Lineage recording, launch-entry resolution, and attach-mode recovery steps."""

from selfstruct import windows
from selfstruct.processes import ProcessInfo, ProcessWindow, launch_entry, process_lineage


def info(pid, parent, exe, created):
    return ProcessInfo(pid, parent, exe.replace('\\', '/').split('/')[-1], exe, created)


GAME = r'D:\Games\Title\Binaries\Win64\Game-Win64-Shipping.exe'
LAUNCHER = r'D:\Games\Title\Title.exe'
BOOT = r'D:\Games\Title\Boot.exe'
STEAM = r'C:\Steam\steam.exe'


def test_lineage_walks_parents_and_stops_at_recycled_parent():
    game = info(30, 20, GAME, 300)
    launcher = info(20, 10, LAUNCHER, 200)
    steam = info(10, 1, STEAM, 100)
    snapshot = {30: game, 20: launcher, 10: steam}
    assert [node.pid for node in process_lineage(snapshot, 30)] == [30, 20, 10]
    recycled = {30: game, 20: info(20, 10, LAUNCHER, 400)}
    assert [node.pid for node in process_lineage(recycled, 30)] == [30]


def test_launch_entry_is_the_launcher_in_the_games_tree():
    chain = [info(30, 20, GAME, 300), info(20, 10, LAUNCHER, 200), info(10, 1, STEAM, 100)]
    assert launch_entry(chain) == LAUNCHER


def test_launch_entry_takes_the_highest_in_tree_ancestor():
    chain = [info(30, 20, GAME, 300), info(20, 10, LAUNCHER, 200), info(10, 1, BOOT, 100)]
    assert launch_entry(chain) == BOOT


def test_launch_entry_for_a_directly_launched_program_is_itself():
    explorer = info(10, 1, r'C:\Windows\explorer.exe', 100)
    assert launch_entry([info(30, 10, GAME, 300), explorer]) == GAME
    assert launch_entry([info(30, 10, GAME, 300), info(10, 1, STEAM, 100)]) == GAME


def make_game(monkeypatch, snapshots, window_sets, entry_exe):
    game = object.__new__(windows.WindowsGame)
    game.mode = 'attach'
    game.target_pid, game.target_created = 11, 1000
    game.target_exe = GAME
    game.target_filter = GAME
    game.ignored_processes = set()
    game.lineage = []
    game.entry_exe = entry_exe
    game.launched_after = 0
    game.cancel = None
    game.root_process = None
    game.recovery_note = ''
    game._recover_candidate = None
    game._recover_launched = None
    game._recover_launches = 0
    game._recover_last_launch = 0.0
    game._recover_known = {}
    game.exe = ''
    game.args = []
    game.bound = []
    game.connect = lambda: game.bound.append((game.target_pid, game.target_created))
    state = {'index': 0}
    def snapshot():
        snap = snapshots[min(state['index'], len(snapshots) - 1)]
        return snap
    def enum(processes):
        wins = window_sets[min(state['index'], len(window_sets) - 1)]
        state['index'] += 1
        return wins
    monkeypatch.setattr(windows, 'snapshot_processes', snapshot)
    monkeypatch.setattr(windows, 'enumerate_windows', enum)
    return game


def test_recover_step_waits_without_launching_while_target_process_exists(tmp_path, monkeypatch):
    entry = tmp_path / 'Title.exe'
    entry.write_bytes(b'x')
    game_proc = info(30, 20, GAME, 3000)
    game = make_game(monkeypatch, [{30: game_proc}], [[]], str(entry))
    launches = []
    game._start_entry = lambda: launches.append(1) or (20, 2000)
    assert game.recover_step() is False
    assert game.recovery_note == 'target process present without a window'
    assert not launches


def test_recover_step_launches_entry_then_binds_stable_candidate(tmp_path, monkeypatch):
    entry = tmp_path / 'Title.exe'
    entry.write_bytes(b'x')
    game_proc = info(30, 20, GAME, 3000)
    window = ProcessWindow(game_proc, 777, 'game', 100)
    game = make_game(monkeypatch, [{}, {30: game_proc}, {30: game_proc}],
                     [[], [window], [window]], str(entry))
    launches = []
    game._start_entry = lambda: launches.append(1) or (20, 2000)
    assert game.recover_step() is False
    assert game.recovery_note.startswith('launched')
    assert len(launches) == 1
    game.launched_after = 1500  # the launched subtree postdates this stamp
    assert game.recover_step() is False
    assert game.recovery_note == 'candidate stabilizing'
    assert game.recover_step() is True
    assert game.bound == [(30, 3000)]


def test_recover_step_binds_its_own_launched_game_at_once(tmp_path, monkeypatch):
    entry = tmp_path / 'Game.exe'
    entry.write_bytes(b'x')
    game_proc = info(20, 1, GAME, 3000)
    window = ProcessWindow(game_proc, 555, 'game', 100)
    game = make_game(monkeypatch, [{}, {20: game_proc}], [[], [window]], str(entry))
    game._start_entry = lambda: (20, 2000)
    assert game.recover_step() is False  # launches the entry (= the game itself)
    game.launched_after = 1500
    assert game.recover_step() is True   # own process: no stability wait
    assert game.bound == [(20, 3000)]


def test_recover_step_without_an_entry_waits_for_a_manual_relaunch(monkeypatch):
    game = make_game(monkeypatch, [{}], [[]], '')
    launches = []
    game._start_entry = lambda: launches.append(1) or (20, 2000)
    assert game.recover_step() is False
    assert game.recovery_note == 'no recorded launch entry'
    assert not launches
