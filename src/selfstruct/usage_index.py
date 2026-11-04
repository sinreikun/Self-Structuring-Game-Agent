"""Persistent file accounting. Product mutations publish exact affected paths.

No Store lock is acquired here. Reconciliation enumerates outside either lock;
mutations concurrent with that enumeration win over the filesystem sample.
"""
from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
import sqlite3
import stat
import threading
import time
import weakref


RECONCILE_SECONDS = 24 * 60 * 60
_indexes = weakref.WeakSet()
_registry_lock = threading.RLock()
_reconciled_at = {}


def for_root(root, *, startup=False, snapshot=False):
    root = Path(root).resolve()
    with _registry_lock:
        for index in _indexes:
            if index.root == root: break
        else:
            due = time.monotonic()-_reconciled_at.get(root, float('-inf')) >= RECONCILE_SECONDS
            return UsageIndex(root, reconcile=not snapshot or due)
    if startup: index.reconcile()
    return index


def covering(root):
    root = Path(root).resolve()
    with _registry_lock:
        return next((index for index in _indexes if root.is_relative_to(index.root)), None)


def category(path):
    parts = path.parts
    if path.name.endswith('.part'): return 'partial_downloads'
    if parts[:2] == ('assets', 'model'): return 'models'
    if parts[:2] == ('assets', 'engine'):
        return 'engine_runtime' if 'expanded' in parts else 'engine_archives'
    if 'evidence' in parts: return 'evidence_images'
    if '.sqlite3' in path.name: return 'database_and_wal'
    if path.name.startswith('checkpoint-'): return 'checkpoint_cache'
    if 'agents' in parts: return 'agent_files'
    if parts[0] == 'exports': return 'exports'
    if parts[0] == 'runtime': return 'application_runtime'
    return 'settings_logs_other'


class UsageIndex:
    def __init__(self, root, *, reconcile=True):
        self.root = Path(root).resolve()
        self.lock = threading.RLock()
        self.scan_lock = threading.Lock()
        self.path = self.root/'host'/'usage-index.sqlite3'
        bootstrap = not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not reconcile and not bootstrap:
            self.db = sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, check_same_thread=False)
        else:
            self.db = sqlite3.connect(self.path, check_same_thread=False)
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,kind TEXT NOT NULL)')
            self.db.commit()
        self.entries = {r[0]: r[1:] for r in self.db.execute('SELECT * FROM files')}
        self.db.close()
        self.db = None
        self.mutable = set()
        self.changed_during_scan = None
        self.last_reconcile = 0.
        self.scans = 0
        with _registry_lock:
            _indexes.add(self)
        if reconcile or bootstrap:
            self.reconcile()
        else:
            # Saved-record views load the existing ledger without a startup scan
            # on every UI poll. Mutable paths still get their exact-size refresh.
            for key in self.entries:
                if '.sqlite3' in Path(key).name or Path(key).suffix in {'.log', '.jsonl'}:
                    self.mutable.add(key)
            hosts = {Path('host')}
            hosts.update(Path(key).parent for key in self.entries
                         if Path(key).name == 'experience.sqlite3')
            for host in hosts:
                for name in ('host.log', 'host.log.1', 'host.log.2', 'llm.log',
                             'llm.log.1', 'llm.log.2', 'diagnostics.jsonl'):
                    self.mutable.add((host/name).as_posix())
            self.last_reconcile = _reconciled_at[self.root]

    @contextmanager
    def transaction(self):
        # Do not keep a Windows handle alive after the owner stops or is deleted.
        connection = sqlite3.connect(self.path, check_same_thread=False)
        self.db = connection
        try:
            with connection: yield
        finally:
            self.db = None
            connection.close()

    def key(self, path):
        # absolute() is lexical; never traverse symlink targets to account them.
        path = Path(path).absolute()
        try: return path.relative_to(self.root).as_posix()
        except ValueError: return None

    def sample(self, key):
        try:
            info = (self.root/key).lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode): return None
        return info.st_size, info.st_mtime_ns, category(Path(key))

    def _own(self, key):
        return key in {'host/usage-index.sqlite3', 'host/usage-index.sqlite3-journal'}

    def _set(self, key, value):
        if self.changed_during_scan is not None:
            self.changed_during_scan.add(key)
        if value is None:
            self.entries.pop(key, None)
            self.mutable.discard(key)
            if not self._own(key): self.db.execute('DELETE FROM files WHERE path=?', (key,))
        else:
            self.entries[key] = value
            if not self._own(key):
                self.db.execute('''INSERT INTO files VALUES(?,?,?,?) ON CONFLICT(path)
                    DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,kind=excluded.kind
                    WHERE size<>excluded.size OR mtime_ns<>excluded.mtime_ns OR kind<>excluded.kind''', (key, *value))
            # Only open-ended writers require bounded per-path stat refresh.
            if '.sqlite3' in Path(key).name or Path(key).suffix in {'.log', '.jsonl'}:
                self.mutable.add(key)

    def record(self, *paths):
        with self.lock, self.transaction():
            for path in paths:
                key = self.key(path)
                if key is not None: self._set(key, self.sample(key))

    def watch(self, *paths):
        with self.lock:
            for path in paths:
                key = self.key(path)
                if key is not None: self.mutable.add(key)

    def reconcile(self):
        """Startup/explicit maintenance only, never while a caller owns Store."""
        with self.scan_lock:
            with self.lock:
                self.changed_during_scan = set()
            try:
                sample = {}
                for path in self.root.rglob('*'):
                    key = self.key(path)
                    value = self.sample(key)
                    if value is not None: sample[key] = value
                with self.lock, self.transaction():
                    changed = self.changed_during_scan
                    self.changed_during_scan = None
                    for key in self.entries.keys() | sample.keys():
                        if key not in changed: self._set(key, sample.get(key))
                    self.last_reconcile = time.monotonic()
                    self.scans += 1
                with _registry_lock:
                    _reconciled_at[self.root] = self.last_reconcile
            finally:
                with self.lock: self.changed_during_scan = None

    def total(self):
        with self.lock:
            # SQLite/WAL and append streams cannot publish through atomic_write.
            # Refresh only registered mutable paths, never enumerate directories.
            for key in tuple(self.mutable):
                value = self.sample(key)
                if value is None:
                    self.entries.pop(key, None)
                else: self.entries[key] = value
            for key in ('host/usage-index.sqlite3', 'host/usage-index.sqlite3-journal'):
                value = self.sample(key)
                if value is None: self.entries.pop(key, None)
                else: self.entries[key] = value
            return sum(row[0] for row in self.entries.values())

    def candidates(self):
        with self.lock:
            return [(self.root/key, *value) for key, value in self.entries.items()
                    if value[2] in {'partial_downloads', 'evidence_images'}]

    def breakdown(self):
        self.total()
        with self.lock:
            result = {}
            for size, _, kind in self.entries.values():
                result[kind] = result.get(kind, 0) + size
            return result

    def subtree_total(self, root):
        self.total()
        key = self.key(root)
        with self.lock:
            return sum(value[0] for name, value in self.entries.items()
                       if key == '.' or name == key or name.startswith(key+'/'))

    def paths_under(self, root):
        key = self.key(root)
        with self.lock:
            return [self.root/name for name in self.entries
                    if key == '.' or name == key or name.startswith(key+'/')]


def changed(*paths):
    with _registry_lock: indexes = list(_indexes)
    for index in indexes:
        relevant = [path for path in paths if index.key(path) is not None]
        if relevant: index.record(*relevant)


def watch(*paths):
    with _registry_lock: indexes = list(_indexes)
    for index in indexes: index.watch(*paths)


def unlink(path, **kwargs):
    path = Path(path)
    path.unlink(**kwargs)
    changed(path)


def replace(source, destination):
    import os
    with _registry_lock: indexes = list(_indexes)
    moved = []
    for index in indexes:
        key = index.key(source)
        if key is not None:
            with index.lock:
                moved.extend((index.root/name, Path(destination)/Path(name).relative_to(key))
                             for name in index.entries if name.startswith(key+'/'))
    os.replace(source, destination)
    changed(source, destination)
    for old, new in moved: changed(old, new)


@contextmanager
def writing(path, mode='wb'):
    path = Path(path)
    try:
        with path.open(mode) as stream:
            watch(path)
            try: yield stream
            finally:
                stream.flush()
                changed(path)
    finally:
        with _registry_lock: indexes = list(_indexes)
        for index in indexes:
            with index.lock: index.mutable.discard(index.key(path))


def copy2(source, destination, **kwargs):
    import shutil
    target = Path(destination)
    if target.is_dir(): target = target/Path(source).name
    try: return shutil.copy2(source, destination, **kwargs)
    finally: changed(target)


def copytree(source, destination):
    import shutil
    return shutil.copytree(source, destination, copy_function=copy2)


def rmtree(path):
    import shutil
    try: shutil.rmtree(path)
    finally:
        # Also account partial deletion if removal failed; only indexed paths.
        with _registry_lock: indexes = list(_indexes)
        for index in indexes:
            if index.root.is_relative_to(Path(path).absolute()) and not index.path.exists():
                with _registry_lock: _indexes.discard(index)
                continue
            key = index.key(path)
            if key is not None:
                with index.lock:
                    paths = [index.root/name for name in index.entries
                             if name == key or name.startswith(key+'/')]
                index.record(*paths)
