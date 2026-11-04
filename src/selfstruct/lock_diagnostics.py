"""Observation only: native RLock acquisition and critical sections are unchanged."""
import itertools
import json
import logging
import queue
import sys
import threading
import time
import traceback
import weakref

_events = queue.SimpleQueue()
_locks = weakref.WeakSet()
_registry = threading.Lock()
_ids = itertools.count(1)


def _service():
    while True:
        try:
            sink, record = _events.get(timeout=.1)
            sink(record)
        except queue.Empty:
            pass
        except Exception:
            logging.getLogger(__name__).exception('Lock diagnostic write failed')
        with _registry:
            locks = list(_locks)
        for lock in locks:
            lock.sample()


class ObservedRLock:
    threshold = 1.0

    def __init__(self, path):
        self._native = threading.RLock()
        self._guard = threading.Lock()
        self._depth = 0
        self._owner = None
        self._waiters = {}
        self.path = path
        self.output_lock = threading.Lock()
        with _registry:
            _locks.add(self)

    def _sink(self, record):
        with self.output_lock:
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')

    def _emit(self, kind, **data):
        _events.put((self._sink, dict(kind=kind, wall_time=time.time(),
                                     monotonic=time.monotonic(), lock='Store', **data)))

    def _holder(self):
        return dict(self._owner) if self._owner else None

    def acquire(self, blocking=True, timeout=-1):
        started = time.monotonic()
        thread = threading.current_thread()
        # Read frame metadata only; linecache/source loading on every hot-path
        # acquisition would itself become significant observer overhead.
        frame = sys._getframe(1)
        scope = []
        while frame and len(scope) < 4:
            filename = frame.f_code.co_filename.replace('\\', '/')
            if not filename.endswith('/lock_diagnostics.py'):
                scope.append(f'{filename.rsplit("/", 1)[-1]}:{frame.f_lineno}:{frame.f_code.co_name}')
            frame = frame.f_back
        del frame
        attempt = dict(id=next(_ids), thread=thread.name, thread_id=thread.ident,
                       scope=scope, started=started, wall_started=time.time())
        with self._guard:
            holder = self._holder()
            self._waiters[attempt['id']] = attempt
            self._emit('store_lock_wait_start', **attempt, holder=holder, wait_seconds=0.)
        try:
            acquired = self._native.acquire(blocking, timeout)
        except BaseException:
            with self._guard:
                self._waiters.pop(attempt['id'], None)
            raise
        with self._guard:
            self._waiters.pop(attempt['id'], None)
            now = time.monotonic()
            if acquired:
                if not self._depth:
                    self._owner = dict(attempt, acquired=now, acquired_wall=time.time())
                self._depth += 1
            self._emit('store_lock_acquire' if acquired else 'store_lock_not_acquired',
                       **attempt, holder=self._holder(), depth=self._depth,
                       wait_seconds=now-started, long_wait=now-started>self.threshold,
                       holder_at_wait_start=holder)
        return acquired

    def release(self):
        with self._guard:
            # Native release enforces ownership and raises on invalid release.
            owner = self._holder()
            now = time.monotonic()
            self._native.release()
            self._depth -= 1
            if not self._depth:
                self._owner = None
            self._emit('store_lock_release', thread=threading.current_thread().name,
                       thread_id=threading.get_ident(), holder=owner, depth=self._depth,
                       hold_seconds=now-owner['acquired'], long_hold=now-owner['acquired']>self.threshold)

    def sample(self):
        with self._guard:
            now = time.monotonic()
            owner = self._owner
            if not owner:
                return
            if now-owner['acquired'] <= self.threshold:
                return
            frame = sys._current_frames().get(owner['thread_id'])
            for waiter in self._waiters.values():
                if now-waiter['started']>self.threshold and not waiter.get('reported'):
                    waiter['reported'] = True
                    self._emit('store_lock_long_wait', waiter=dict(waiter), holder=self._holder(),
                               wait_seconds=now-waiter['started'], hold_seconds=now-owner['acquired'],
                               holder_stack=traceback.format_stack(frame) if frame else [])
            if now-owner['acquired']>self.threshold and not owner.get('hold_reported'):
                owner['hold_reported'] = True
                self._emit('store_lock_long_hold', holder=self._holder(),
                           hold_seconds=now-owner['acquired'],
                           holder_stack=traceback.format_stack(frame) if frame else [])

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.release()

    def flush(self):
        """Drain earlier records after Store.close has released the lock."""
        done = threading.Event()
        _events.put((lambda record: done.set(), {}))
        if not done.wait(5):
            logging.getLogger(__name__).warning('Lock diagnostic flush timed out')


threading.Thread(target=_service, name='lock-diagnostics', daemon=True).start()
