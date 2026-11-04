import json
import threading
import time

from selfstruct.lock_diagnostics import ObservedRLock


def test_holder_waiter_and_long_hold(tmp_path):
    path = tmp_path/'diagnostics.jsonl'
    lock = ObservedRLock(path)
    def records_now():
        with lock.output_lock:
            return [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []
    held = threading.Event()
    release = threading.Event()

    def owner_work():
        with lock:
            with lock:  # reentry retains the outer owner and hold clock
                held.set()
                release.wait(5)

    owner = threading.Thread(target=owner_work, name='owner-work')
    owner.start()
    assert held.wait(2)
    waiter = threading.Thread(target=lambda: (lock.acquire(), lock.release()), name='waiting-player')
    waiter.start()
    try:
        deadline = time.monotonic()+4
        while time.monotonic()<deadline:
            records = records_now()
            if any(r['kind']=='store_lock_long_wait' for r in records):
                break
            time.sleep(.02)
        long_wait = next(r for r in records if r['kind']=='store_lock_long_wait')
        assert long_wait['holder']['thread']=='owner-work'
        assert any('owner_work' in s for s in long_wait['holder']['scope'])
        assert long_wait['wait_seconds']>1
        assert any('release.wait' in s for s in long_wait['holder_stack'])
        assert any(r['kind']=='store_lock_long_hold' for r in records)
    finally:
        release.set()
        owner.join(3)
        waiter.join(3)
    deadline = time.monotonic()+2
    while time.monotonic()<deadline:
        records = records_now()
        if any(r['kind']=='store_lock_release' and r['thread']=='waiting-player' for r in records):
            break
        time.sleep(.02)
    assert any(r['kind']=='store_lock_release' and r['long_hold'] for r in records)
    assert any(r['kind']=='store_lock_acquire' and r['thread']=='waiting-player' and r['long_wait'] for r in records)


def test_native_nonblocking_and_invalid_release(tmp_path):
    import pytest
    lock = ObservedRLock(tmp_path/'diagnostics.jsonl')
    with pytest.raises(RuntimeError):
        lock.release()
    with lock:
        result = []
        worker = threading.Thread(target=lambda: result.append(lock.acquire(False)))
        worker.start()
        worker.join(2)
        assert result == [False]
        assert lock.acquire(False)
        lock.release()
    assert lock.acquire(timeout=.01)
    lock.release()


def test_long_wait_does_not_suppress_subsequent_long_hold(tmp_path):
    lock = ObservedRLock(tmp_path/'diagnostics.jsonl')
    began = threading.Event()
    acquired = threading.Event()
    finish = threading.Event()
    def wait_then_work():
        began.set()
        with lock:
            acquired.set()
            finish.wait(5)
    with lock:
        worker = threading.Thread(target=wait_then_work, name='wait-then-hold')
        worker.start()
        assert began.wait(2)
        time.sleep(1.2)
    try:
        assert acquired.wait(2)
        deadline = time.monotonic()+3
        while time.monotonic()<deadline:
            with lock.output_lock:
                records = [json.loads(s) for s in lock.path.read_text().splitlines()]
            if any(r['kind']=='store_lock_long_hold' and r['holder']['thread']=='wait-then-hold' for r in records):
                break
            time.sleep(.02)
        assert any(r['kind']=='store_lock_long_wait' and r['waiter']['thread']=='wait-then-hold' for r in records)
        assert any(r['kind']=='store_lock_long_hold' and r['holder']['thread']=='wait-then-hold' for r in records)
    finally:
        finish.set()
        worker.join(3)
        lock.flush()
