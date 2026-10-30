from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading
import zipfile

from PIL import Image
import pytest
import wasmtime

from selfstruct.config import Config, RootFiles
from selfstruct.sandbox import Revisions, Sandbox, features, improves, replay
from selfstruct.store import CapacityError, Store


@pytest.fixture
def seed():
    return Path("src/selfstruct/assets/seed.wat").read_text(encoding="utf-8")


def event(store, sid, before, after, action="A", change=.1):
    eid = store.begin(sid, before, action, .06, 1, "seed", 42, -1)
    store.finish(eid, after, change, .001)
    return eid


@pytest.mark.parametrize("relative", ["../outside", "/tmp/outside", "a/../../outside", "C:\\outside", "a\\b", "x:y", ""])
def test_workspace_escape(tmp_path, relative):
    files = RootFiles(tmp_path / "agent", 1024)
    with pytest.raises(ValueError):
        files.write(relative, b"x")


def test_workspace_symlink_and_quota(tmp_path):
    files = RootFiles(tmp_path / "agent", 32)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (files.root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not permit test symlink creation")
    with pytest.raises(ValueError):
        files.write("link/evil", b"x")
    files.write("nested/ok", b"hello")
    assert files.read("nested/ok") == b"hello"
    with pytest.raises(OSError):
        files.write("too-big", b"x" * 40)
    files.delete("nested/ok")


def test_store_atomic_evidence_resume_export(tmp_path):
    store = Store(tmp_path, 64 * 1024**2)
    sid = store.session("Newborn", False)
    image = Image.new("RGB", (32, 32), "red")
    digest = store.image(image)
    assert digest == store.image(image)
    assert len(list((tmp_path / "evidence").iterdir())) == 1
    eid = event(store, sid, digest, digest)
    store.knowledge("hypothesis", "変化条件は未確認です。", "inferred", "hypothesis", [eid])
    pending = store.begin(sid, digest, "A", .1, 1, "r", 1, -1)
    jid = store.job("research", {"evidence": eid})
    store.set_job(jid, "running")
    store.export(tmp_path / "export.zip")
    with zipfile.ZipFile(tmp_path / "export.zip") as archive:
        assert "研究ノート.md" in archive.namelist()
        assert "diagnostics.sqlite3" in archive.namelist()
        assert len(archive.namelist()) == len(set(archive.namelist()))
    store.close()
    store = Store(tmp_path, 64 * 1024**2)
    assert store.session("Newborn", True) == sid
    assert store.db.execute("SELECT state FROM events WHERE id=?", (pending,)).fetchone()[0] == "interrupted"
    assert store.jobs()[0]["id"] == jid
    with pytest.raises(ValueError):
        store.knowledge("hypothesis", "fake", "inferred", "supported", [999])
    store.quota = 1
    with pytest.raises(CapacityError):
        store.image(Image.new("RGB", (32, 32), "blue"))
    assert (tmp_path / "evidence" / (digest + ".png")).exists()
    store.close()


def test_sandbox_no_os_and_finite_fuel(seed):
    agent = Sandbox(seed)
    assert agent.action(12, 6)[0] in range(6)
    with pytest.raises(ValueError, match="forbidden"):
        Sandbox('(module (import "wasi_snapshot_preview1" "path_open" (func)))')
    loop = seed.replace('(i32.const 60)', '(loop $forever (br $forever)) (i32.const 60)')
    with pytest.raises(wasmtime.Trap):
        Sandbox(loop).action(42, 2)
    with pytest.raises((wasmtime.Trap, wasmtime.WasmtimeError)):
        Sandbox(seed.replace('(memory (export "memory") 1 256)', '(memory (export "memory") 1024 1024)'))
    with pytest.raises(ValueError):
        Sandbox(seed.replace('(i32.const 60)', '(i32.const 9000)')).action(1, 2)


def test_capability_broker(seed, tmp_path):
    import wasmtime
    request = json.dumps({"op":"write", "path":"notes/a.txt", "text":"observation"}).encode()
    wat_data = ''.join('\\' + format(b, '02x') for b in request)
    mod = seed[:-2] + f'''(global $requested (mut i32) (i32.const 0))
      (data (i32.const 4096) "{wat_data}")
      (func (export "request") (result i32)
        (if (result i32) (global.get $requested) (then (i32.const 0))
          (else (global.set $requested (i32.const 1)) (i32.const {len(request)}))))
    )'''
    agent = Sandbox(mod)
    files = RootFiles(tmp_path, 8192)
    agent.requests(files, lambda n: None)
    assert files.read("notes/a.txt") == b"observation"


def test_revisions_rollback_and_integrity(tmp_path, seed):
    revisions = Revisions(tmp_path)
    stable = revisions.active
    candidate = revisions.create(seed.replace('(i32.const 60)', '(i32.const 70)'))
    revisions.promote(candidate)
    assert revisions.active == candidate and revisions.state["trial"]
    revisions.rollback()
    assert revisions.active == stable
    revisions.promote(candidate)
    revisions.confirm()
    assert Revisions(tmp_path).active == candidate
    (tmp_path / candidate / "agent.wat").write_text("corrupted")
    assert Revisions(tmp_path).active == stable


def test_replay_comparison_rejects_regressions(tmp_path, seed):
    store = Store(tmp_path, 64 * 1024**2)
    sid = store.session("Newborn", False)
    before = store.image(Image.new("RGB", (32, 32), "black"))
    after = store.image(Image.new("RGB", (32, 32), "white"))
    for _ in range(32):
        event(store, sid, before, after)
    rows = store.rows()
    prediction = seed.replace('(i32.const -1)', '(i32.const 950)')
    base = replay(seed, rows, tmp_path / "evidence", ["A"])
    candidate = replay(prediction, rows, tmp_path / "evidence", ["A"])
    assert improves(base, candidate)
    assert not improves(candidate, base)
    assert not improves(base | {"n":0}, candidate | {"n":0})
    bad = candidate | {"samples": [dict(x) for x in candidate["samples"]]}
    bad["samples"][0]["loss"] = 1
    assert not improves(candidate, bad)
    store.close()


def test_hypotheses_are_independently_refuted(tmp_path):
    store = Store(tmp_path, 64 * 1024**2)
    sid = store.session("Newborn", False)
    img = store.image(Image.new("RGB", (32, 32)))
    first = event(store, sid, img, img)
    kid = store.knowledge("hypothesis", "入力候補の後に変化が続く可能性があります。", "inferred", "hypothesis", [first])
    store.claim(kid, "A", 1, first)
    for _ in range(32):
        event(store, sid, img, img, change=0)
    store.validate_claims()
    assert store.knowledge_rows()[0]["validation"] == "refuted"
    assert store.knowledge_rows()[0]["provenance"] == "inferred"
    store.close()


def test_newborn_isolation_and_resume(tmp_path):
    from selfstruct.engine import Controller
    from selfstruct.smoke import SyntheticGame
    config = Config(root=str(tmp_path), keys=["A", "B"])
    first = Controller(config, SyntheticGame())
    initial = first.experiment
    first.close()
    resumed = Controller(replace(config, resume=True), SyntheticGame())
    assert resumed.experiment == initial
    resumed.close()
    newborn = Controller(config, SyntheticGame())
    assert newborn.experiment != initial
    assert not newborn.store.rows() and not newborn.store.knowledge_rows()
    newborn.close()


def test_offline_integration():
    from selfstruct.smoke import smoke
    smoke()


def test_checkpoint_retains_internal_state(seed):
    import zlib
    agent = Sandbox(seed)
    for _ in range(8):
        agent.action(12, 6)
    state = agent.checkpoint()
    expected = [agent.action(12, 6) for _ in range(20)]
    resumed = Sandbox(seed)
    resumed.restore(state)
    assert [resumed.action(12, 6) for _ in range(20)] == expected
    with pytest.raises((ValueError, zlib.error)):
        resumed.restore(b'not-compressed')


def test_single_writer_lock(tmp_path):
    from selfstruct.config import InstanceLock
    first = InstanceLock(tmp_path/'lock')
    try:
        with pytest.raises(ValueError):
            InstanceLock(tmp_path/'lock')
    finally:
        first.close()
    InstanceLock(tmp_path/'lock').close()


def test_requested_numpad_configuration_is_valid_and_distinct_from_number_row():
    from selfstruct.windows import KEY_CODES
    requested='UP,DOWN,LEFT,RIGHT,Num0,Num1,Num2,Num3,Num4,Num5,Num6,Num7,Num8,Tab'
    config=Config(keys=[key.strip().upper() for key in requested.split(',')])
    config.validate(require_game=False)
    assert config.keys[-1]=='TAB'
    assert [KEY_CODES[f'NUM{k}'] for k in range(10)]==list(range(0x60,0x6a))
    assert all(KEY_CODES[f'NUM{k}']!=KEY_CODES[str(k)] for k in range(10))


def test_predictor_cannot_read_future_image(tmp_path, seed):
    import random
    store = Store(tmp_path, 64*1024**2)
    sid = store.session('Newborn',False)
    black = store.image(Image.new('RGB',(32,32),'black'))
    white = store.image(Image.new('RGB',(32,32),'white'))
    rng = random.Random(93)
    for _ in range(64):
        changed = rng.randrange(2)
        event(store,sid,black,white if changed else black,change=float(changed))
    cheat = seed.replace('(i32.const -1)', '(if (result i32) (i32.gt_u (i32.load8_u (i32.const 1024)) (i32.const 100)) (then (i32.const 1000)) (else (i32.const 0)))')
    baseline = replay(seed,store.rows(),tmp_path/'evidence',['A'])
    candidate = replay(cheat,store.rows(),tmp_path/'evidence',['A'])
    assert candidate['loss'] > .25
    assert not improves(baseline,candidate)
    store.close()


def test_regression_evidence_survives_hot_window_eviction(tmp_path):
    store = Store(tmp_path,64*1024**2)
    sid=store.session('Newborn',False)
    image=store.image(Image.new('RGB',(32,32)))
    first=event(store,sid,image,image,change=1)
    with store.db:
        store.db.execute('UPDATE events SET prediction=.95 WHERE id=?',(first,))
    for _ in range(200):
        cutoff=event(store,sid,image,image,change=0)
    for _ in range(520):
        event(store,sid,image,image,change=0)
    assert not [r for r in store.rows(512) if r['id']<=cutoff]
    regression=store.regression_rows(cutoff)
    assert first in {r['id'] for r in regression}
    assert all(r['id']<=cutoff for r in regression)
    store.close()
