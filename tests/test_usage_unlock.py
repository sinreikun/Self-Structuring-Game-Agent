import json
import os
from pathlib import Path
import threading
import time

import pytest

from selfstruct.store import CapacityError, Store


@pytest.mark.parametrize('phase', ['explicit', 'startup', 'scheduled', 'root_change'])
def test_collect_scans_allow_other_thread_to_acquire(tmp_path, monkeypatch, phase):
    store = Store(tmp_path, 64*1024**2, record_locks=True)
    entered, proceed = threading.Event(), threading.Event()
    original_rglob = Path.rglob
    errors = []
    target = tmp_path/'other' if phase == 'root_change' else tmp_path
    def rglob(path, pattern):
        if path == target and pattern == '*':
            entered.set()
            assert proceed.wait(5)
        yield from original_rglob(path, pattern)
    monkeypatch.setattr(Path, 'rglob', rglob)
    def monitor():
        try:
            if phase == 'startup':
                other = Store(tmp_path, 64*1024**2)
                other.close()
            elif phase == 'scheduled':
                from selfstruct.usage_index import RECONCILE_SECONDS
                store._usage_index.last_reconcile = time.monotonic()-RECONCILE_SECONDS-1
                store.usage()
            elif phase == 'root_change':
                store.account_root = target
            else:
                store.reconcile_usage()
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=monitor, name='telemetry')
    worker.start()
    try:
        assert entered.wait(5)
        assert store.lock.acquire(timeout=.2)
        store.lock.release()
    finally:
        proceed.set()
        worker.join(10)
        store.close()
    assert not worker.is_alive()
    assert not errors


def test_usage_refresh_quota_and_gc(tmp_path):
    store = Store(tmp_path, 64*1024**2)
    before = store.usage(True)
    path = tmp_path/'external'
    path.write_bytes(b'x'*200000)
    assert store.usage() < before+200000
    store.reconcile_usage()
    assert store.usage() >= before+200000
    path.unlink()
    assert store.reconcile_usage() < before+200000
    orphan = tmp_path/'evidence'/'orphan.png'
    orphan.write_bytes(b'x'*100000)
    os.utime(orphan, (1, 1))
    used = store.reconcile_usage()
    store.quota = int(used/.85)
    assert store.collect() == 100000
    assert not orphan.exists()
    assert store.db.execute('SELECT count(*) FROM gc_history').fetchone()[0] == 1
    store.quota = 1
    with pytest.raises(CapacityError):
        store.reserve(1)
    store.close()


def test_reentrant_calls_never_enumerate(tmp_path, monkeypatch):
    store = Store(tmp_path, 64*1024**2)
    def forbidden(*args, **kwargs):
        raise AssertionError('filesystem enumeration inside caller lock')
    monkeypatch.setattr(Path, 'rglob', forbidden)
    monkeypatch.setattr(Path, 'glob', forbidden)
    with store.lock:
        store.usage(True)
        store.collect(True)
        store.storage_snapshot()
        store.reserve(0)
    store.close()


def test_deferred_gc_survives_ratio_and_count_drop(tmp_path):
    store = Store(tmp_path, 64*1024**2)
    orphan = tmp_path/'evidence'/'orphan.png'
    orphan.write_bytes(b'x'*1000)
    os.utime(orphan, (1, 1))
    store.reconcile_usage()
    with store.lock:
        store.collect(True)
        assert orphan.exists()
    assert store.collect() == 1000
    assert not orphan.exists()
    store.close()


def test_large_tree_telemetry_hold_instrumentation(tmp_path, monkeypatch):
    store = Store(tmp_path, 64*1024**2, record_locks=True)
    tree = tmp_path/'tree'
    tree.mkdir()
    for i in range(1200):
        (tree/str(i)).write_bytes(b'x'*10)
    original = Path.stat
    def slow_stat(path, *args, **kwargs):
        if path.parent == tree:
            time.sleep(.0005)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', slow_stat)
    errors = []
    def monitor():
        try:
            store.collect(True)
        except BaseException as exc:
            errors.append(exc)
    started = time.monotonic()
    worker = threading.Thread(target=monitor, name='telemetry')
    worker.start()
    worker.join(30)
    elapsed = time.monotonic()-started
    assert not worker.is_alive()
    store.close()
    assert not errors
    records = [json.loads(line) for line in store.lock.path.read_text().splitlines()]
    holds = [r['hold_seconds'] for r in records if r['kind']=='store_lock_release'
             and r['thread']=='telemetry' and r['depth']==0]
    assert elapsed < 5
    assert holds and max(holds) < .5
    evidence = Path('outputs/usage-index-evidence')
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence/'large-tree-locks.jsonl').write_text('\n'.join(json.dumps(r) for r in records)+'\n')
    (evidence/'large-tree-summary.json').write_text(json.dumps(dict(
        files=1200, elapsed_seconds=elapsed, max_telemetry_hold_seconds=max(holds),
        telemetry_outer_holds=len(holds)), indent=2))
