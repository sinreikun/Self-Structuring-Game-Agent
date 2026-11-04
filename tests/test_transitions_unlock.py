import json
import runpy
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from selfstruct.store import Store
from selfstruct.experiments import Experiments
from selfstruct import transitions
from selfstruct.research_actions import experiment_context


def fixture(tmp_path):
    store = Store(tmp_path, 100_000_000, record_locks=True)
    sid = store.session('test', False)
    hashes = [store.image(Image.new('RGB', (64, 64), color)) for color in ('black', 'white', 'gray')]
    def event(a, b):
        eid = store.begin(sid, hashes[a], 'A', .06, 1, 'seed', 42, -1)
        store.finish(eid, hashes[b], .5, .001)
        return eid
    first = event(0, 1)
    a = transitions.register_state(store, 'A', [first])
    b = transitions.register_state(store, 'B', [first], 'after')
    kid = store.knowledge('hypothesis', 'transition', 'inferred', 'hypothesis', [first])
    transitions.register_claim(store, kid, 'A', {'from_state': a, 'to_state': b, 'probability': 1})
    ids = [event(0, 1), event(1, 0), event(0, 2)]
    store.db.execute('INSERT INTO research_experiments VALUES(?,?,?,?,?,?,?,?,?,?)',
                     ('p', 'job', kid, json.dumps({'action': 0, 'repeat': 3}), 'complete', json.dumps(ids), '{}', 0, 1, 1))
    store.db.commit()
    ex = Experiments(store, ['A'], SimpleNamespace(record=lambda *a: None))
    return SimpleNamespace(store=store, experiments=ex, keys=['A'])


def test_fixed_context_and_scores_equal_legacy(tmp_path, monkeypatch):
    scientist = fixture(tmp_path)
    legacy = runpy.run_path(str(Path(__file__).parent/'fixtures/legacy_transition_context.py'))
    scores = []
    original = transitions.similarity
    def measure(a, b):
        result = original(a, b)
        scores.append(result)
        return result
    monkeypatch.setattr(transitions, 'similarity', measure)
    try:
        expected = legacy['experiment_context'](scientist, 'job')
        expected_scores = scores[:]
        scores.clear()
        assert experiment_context(scientist, 'job') == expected
        assert scores == expected_scores and len(scores) > 0
        assert expected['hypotheses'][0]['evaluation_requirements']['matching_post_registration_samples'] == 1
    finally:
        scientist.store.close()


def test_slow_decode_releases_store_and_freezes_metadata(tmp_path, monkeypatch):
    scientist = fixture(tmp_path)
    expected = experiment_context(scientist, 'job')
    entered, release = threading.Event(), threading.Event()
    original = Image.open
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(Image, 'open', slow)
    results, errors = [], []
    def research():
        try: results.append(experiment_context(scientist, 'job'))
        except BaseException as exc: errors.append(exc)
    worker = threading.Thread(target=research, name='slow-context')
    worker.start()
    try:
        assert entered.wait(3)
        start = time.monotonic()
        assert scientist.store.lock.acquire(timeout=.25)
        try:
            scientist.store.db.execute("UPDATE knowledge SET text='later' WHERE kind='state'")
            scientist.store.db.commit()
        finally: scientist.store.lock.release()
        assert time.monotonic()-start < .25
        time.sleep(.3)
    finally:
        release.set()
        worker.join(5)
        scientist.store.close()
    assert not worker.is_alive() and not errors
    assert results == [expected]
    records = [json.loads(s) for s in (tmp_path/'host/diagnostics.jsonl').read_text().splitlines()]
    holds = [r for r in records if r['kind']=='store_lock_release' and r['depth']==0 and r['holder']['thread']=='slow-context']
    assert holds and max(r['hold_seconds'] for r in holds) < .25
    evidence = Path('outputs/transitions-unlock-evidence')
    if evidence.exists():
        (evidence/'target-lock-diagnostics.jsonl').write_text('\n'.join(json.dumps(r) for r in records), encoding='utf-8')
