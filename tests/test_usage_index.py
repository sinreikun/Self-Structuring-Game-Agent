import json
import io
import hashlib
import os
from pathlib import Path
import threading
import time
import zipfile

from PIL import Image
import pytest

from selfstruct import usage_index as accounting
from selfstruct.config import atomic_write, RootFiles
from selfstruct.store import Store, CapacityError


def measured(root):
    return sum(p.stat().st_size for p in root.rglob('*')
               if p.is_file() and not p.is_symlink())


def test_product_mutations_equal_filesystem(tmp_path):
    store = Store(tmp_path, 64*1024**2)
    files = RootFiles(tmp_path/'agents'/'workspace', 1000000)
    for i in range(12):
        store.image(Image.new('RGB', (16, 16), (i, 0, 0)))
        files.write('memo', b'x'*(i*1024))
        atomic_write(tmp_path/'host'/'state.json', b'y'*(i*19))
        model = tmp_path/'assets'/'model'/'artifact'
        model.parent.mkdir(parents=True, exist_ok=True)
        with accounting.writing(model.with_suffix('.part')) as out:
            out.write(b'z'*(i*111))
        accounting.replace(model.with_suffix('.part'), model)
        if i % 2:
            files.delete('memo')
            accounting.unlink(model)
        assert store.usage() == measured(tmp_path)
        assert sum(store.storage_breakdown().values()) == measured(tmp_path)
    store.close()


def test_steady_collect_never_enumerates_even_forced(tmp_path, monkeypatch):
    store = Store(tmp_path, 64*1024**2)
    scans = []
    def forbidden(*args, **kwargs):
        scans.append(str(args))
        raise AssertionError('steady-state filesystem enumeration')
    monkeypatch.setattr(Path, 'rglob', forbidden)
    monkeypatch.setattr(Path, 'glob', forbidden)
    for _ in range(20):
        store.collect()
        store.storage_snapshot()
        store.storage_breakdown()
        store.usage(True)
    store.collect(True)
    assert scans == []
    evidence = Path('outputs/usage-index-evidence')
    evidence.mkdir(exist_ok=True, parents=True)
    (evidence/'steady-scan-count.json').write_text(json.dumps(dict(
        collect_calls=21, forced_collect_calls=1, recursive_scan_calls=len(scans),
        startup_scans=store._usage_index.scans)), encoding='utf-8')
    store.close()


def test_gc_candidates_accounting_and_protection_without_scan(tmp_path, monkeypatch):
    store = Store(tmp_path, 64*1024**2)
    protected = store.image(Image.new('RGB', (16, 16), (1, 2, 3)))
    sid = store.session('Newborn', False)
    eid = store.begin(sid, protected, 'A', .01, 1, 'seed', 1, -1)
    store.pin([eid], 'test')
    orphan = tmp_path/'evidence'/'orphan.png'
    partial = tmp_path/'assets'/'model'/'old.part'
    fresh = tmp_path/'evidence'/'fresh.png'
    for path, size in ((orphan, 1000), (partial, 2000), (fresh, 3000)):
        atomic_write(path, b'x'*size)
    for path in (orphan, partial, tmp_path/'evidence'/(protected+'.png')):
        os.utime(path, (1, 1))
        accounting.changed(path)
    scans = store._usage_index.scans
    def forbidden(*args, **kwargs): raise AssertionError('GC enumerated')
    monkeypatch.setattr(Path, 'rglob', forbidden)
    monkeypatch.setattr(Path, 'glob', forbidden)
    assert store.collect(True) == 3000
    assert not orphan.exists() and not partial.exists()
    assert fresh.exists() and (tmp_path/'evidence'/(protected+'.png')).exists()
    assert store._usage_index.scans == scans
    monkeypatch.undo()
    assert store.usage() == measured(tmp_path)
    store.quota = 1
    with pytest.raises(CapacityError): store.reserve(1)
    store.close()


def test_startup_repairs_external_addition_deletion_and_overwrite(tmp_path):
    store = Store(tmp_path, 64*1024**2)
    atomic_write(tmp_path/'deleted', b'x'*50)
    atomic_write(tmp_path/'overwritten', b'x'*100)
    store.close()
    (tmp_path/'deleted').unlink()
    (tmp_path/'added').write_bytes(b'a'*1234)
    (tmp_path/'overwritten').write_bytes(b'b'*2345)
    reopened = Store(tmp_path, 64*1024**2)
    assert reopened.usage() == measured(tmp_path)
    assert 'deleted' not in reopened._usage_index.entries
    assert reopened._usage_index.entries['added'][0] == 1234
    assert reopened._usage_index.entries['overwritten'][0] == 2345
    reopened.close()


def test_reconcile_concurrent_product_mutation_wins(tmp_path, monkeypatch):
    store = Store(tmp_path, 64*1024**2)
    path = tmp_path/'sample'
    atomic_write(path, b'old')
    entered, resume = threading.Event(), threading.Event()
    original = store._usage_index.sample
    def sample(key):
        result = original(key)
        if key == 'sample' and threading.current_thread().name == 'reconcile':
            entered.set()
            assert resume.wait(5)
        return result
    monkeypatch.setattr(store._usage_index, 'sample', sample)
    errors = []
    def reconcile():
        try: store.reconcile_usage()
        except BaseException as error: errors.append(error)
    thread = threading.Thread(target=reconcile, name='reconcile')
    thread.start()
    assert entered.wait(5)
    assert store.lock.acquire(timeout=.2)
    store.lock.release()
    atomic_write(path, b'new'*1000)
    resume.set()
    thread.join(10)
    assert not thread.is_alive() and not errors
    assert store.usage() == measured(tmp_path)
    store.close()


def test_reconciliation_interval_not_force_controls_scans(tmp_path):
    store = Store(tmp_path, 64*1024**2)
    index = store._usage_index
    count = index.scans
    (tmp_path/'external').write_bytes(b'x'*2000)
    store.collect(True)
    assert index.scans == count
    index.last_reconcile = time.monotonic()-accounting.RECONCILE_SECONDS-1
    store.usage()
    assert index.scans == count+1
    assert store.usage() == measured(tmp_path)
    store.close()


def test_tree_copy_move_delete_use_affected_paths(tmp_path, monkeypatch):
    store = Store(tmp_path, 64*1024**2)
    atomic_write(tmp_path/'source'/'one', b'x'*100)
    atomic_write(tmp_path/'source'/'deep'/'two', b'y'*200)
    def forbidden(*args, **kwargs): raise AssertionError('accounting enumerated')
    monkeypatch.setattr(Path, 'rglob', forbidden)
    accounting.copytree(tmp_path/'source', tmp_path/'copy')
    accounting.replace(tmp_path/'copy', tmp_path/'moved')
    accounting.rmtree(tmp_path/'source')
    accounting.rmtree(tmp_path/'moved')
    monkeypatch.undo()
    assert store.usage() == measured(tmp_path)
    store.close()


def test_account_root_change_initializes_before_nested_use(tmp_path):
    store = Store(tmp_path/'individual', 64*1024**2)
    atomic_write(tmp_path/'global', b'x'*400)
    store.account_root = tmp_path
    with store.lock:
        assert store.usage() == measured(tmp_path)
        store.reserve(1)
    store.close()


def test_gateway_download_extract_and_remove_without_accounting_scan(tmp_path, monkeypatch):
    from selfstruct.resources import Gateway
    store = Store(tmp_path, 64*1024**2)
    gateway = Gateway(tmp_path, store.quota, {'files.example'})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('bin/llama-server.exe' if os.name == 'nt' else 'bin/llama-server', b'engine')
        archive.writestr('bin/library.dll', b'library')
    payload = buffer.getvalue()
    class Response(io.BytesIO):
        status, headers = 200, {}
    class Opener:
        def open(self, *args, **kwargs): return Response(payload)
    monkeypatch.setattr('selfstruct.resources.build_opener', lambda *args: Opener())
    def forbidden(*args, **kwargs): raise AssertionError('gateway accounting enumerated')
    monkeypatch.setattr(Path, 'rglob', forbidden)
    item = dict(url='https://files.example/engine', size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(), trust='Trusted', license='MIT')
    artifact = gateway.download(item, 'engine')
    server = gateway.extract_engine(artifact)
    assert server.read_bytes() == b'engine'
    assert gateway.extract_engine(artifact) == server
    monkeypatch.undo()
    assert store.usage() == measured(tmp_path)
    accounting.rmtree(artifact.parent)
    assert store.usage() == measured(tmp_path)
    store.close()


def test_append_and_in_progress_write_accounting(tmp_path):
    store = Store(tmp_path, 64*1024**2)
    log = tmp_path/'host'/'fixture.log'
    accounting.watch(log)
    with log.open('ab') as stream:
        stream.write(b'x'*2000)
        stream.flush()
        assert store.usage() == measured(tmp_path)
    partial = tmp_path/'assets'/'model'/'artifact.part'
    partial.parent.mkdir(parents=True)
    with accounting.writing(partial) as stream:
        stream.write(b'p'*1234)
        stream.flush()
        assert store.usage() == measured(tmp_path)
    assert store.usage() == measured(tmp_path)
    store.close()


def test_atomic_temporary_sample_is_removed_from_reconcile(tmp_path, monkeypatch):
    store = Store(tmp_path, 64*1024**2)
    original_replace = os.replace
    def replace(source, destination):
        # Reconcile after mkstemp/write, before the atomic rename.
        assert store.usage() == measured(tmp_path)
        store.reconcile_usage()
        return original_replace(source, destination)
    monkeypatch.setattr(os, 'replace', replace)
    atomic_write(tmp_path/'record', b'x'*100)
    assert not any('.pending-' in key for key in store._usage_index.entries)
    assert store.usage() == measured(tmp_path)
    store.close()


def test_saved_record_views_use_ledger_without_scans_or_experience_writes(tmp_path, monkeypatch):
    from selfstruct.inspection import ReadOnlyRecords
    store = Store(tmp_path, 64*1024**2)
    job = store.job('research', {'fixture': True})
    store.set_job(job, 'running')
    store.close()
    del store
    # Append-only diagnostic output can finish after the experience DB closes.
    (tmp_path/'host'/'diagnostics.jsonl').write_bytes(b'x'*1234)
    def forbidden(*args, **kwargs): raise AssertionError('saved view enumerated')
    monkeypatch.setattr(Path, 'rglob', forbidden)
    for _ in range(2):
        view = ReadOnlyRecords(tmp_path, tmp_path, 64*1024**2)
        assert view.storage_snapshot()['used'] == sum(view.storage_breakdown().values())
        assert view.db.execute('SELECT state FROM jobs WHERE id=?', (job,)).fetchone()[0] == 'running'
        assert view.db.total_changes == 0
        view.close()
    monkeypatch.undo()
    assert view.usage() == measured(tmp_path)
