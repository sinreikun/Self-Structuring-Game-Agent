import json
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

from selfstruct import situation_candidates as candidates, transitions
from selfstruct.store import Store
from selfstruct.research_actions import record_findings, allowed_evidence
from selfstruct.research_schema import reference_domain, response_schema, validate
from selfstruct.research import PROPOSAL_SCHEMA


def seed(tmp_path):
    store = Store(tmp_path, 100_000_000)
    session = store.session('test', False)
    ids = []
    for color in (0, 100, 255):
        for delta in range(5):
            image = store.image(Image.new('RGB', (64, 64), (min(255, color+delta),)*3))
            event = store.begin(session, image, 'A', .06, 1, 'seed', 42, -1)
            store.finish(event, image, .0, .001)
            ids.append(event)
    return store, ids


def test_clusters_identify_bounds_and_numeric_records(tmp_path, monkeypatch):
    store, ids = seed(tmp_path)
    monkeypatch.setattr(candidates, 'CANDIDATE_LIMIT', 2)
    bundle = candidates.generate(store, 'job')
    assert len(bundle) == 2
    payload = {'situation_candidates': bundle}
    agenda = candidates.agenda(payload)
    assert all(set(c) == {'id', 'frames'} and all(type(i) is int for i in c['frames']) for c in bundle)
    assert all(set(c) == {'id', 'frame_count', 'representative_event'} for c in agenda)
    refs = [{'state': i+1, 'event': c['frames'][0], 'phase': 'before'} for i, c in enumerate(bundle)]
    for i, c in enumerate(bundle):
        assert 3 <= len(c['frames']) <= transitions.POOL_LIMIT
        assert all(transitions.identify(store, store.observed_image(e, 'before'), refs) == i+1 for e in c['frames'])
    transitions.register_state(store, 'model description', ids[:5])
    assert not any(set(c['frames']) & set(ids[:5]) for c in candidates.generate(store, 'job'))


def test_adoption_existing_path_and_claim_domain(tmp_path):
    store, ids = seed(tmp_path)
    payload = {'situation_candidates': candidates.generate(store, 'job')}
    scientist = SimpleNamespace(store=store, keys=['A'], experiments=None, activity=SimpleNamespace(record=lambda *a: None))
    bundle = payload['situation_candidates']
    kid = store.knowledge('hypothesis', 'model transition', 'inferred', 'hypothesis', ids[:1])
    with pytest.raises(ValueError, match='Unknown transition state'):
        transitions.register_claim(store, kid, 'A', {'from_state': bundle[0]['id'], 'to_state': bundle[1]['id'], 'probability': 1})
    assert reference_domain(store, payload)['states'] == []
    findings = [{'id': 's'+str(i), 'state': True, 'candidate_id': c['id'], 'text': 'model '+str(i), 'evidence': [c['frames'][0]], 'phase': 'before'} for i,c in enumerate(bundle[:2])]
    schema = response_schema(PROPOSAL_SCHEMA, ['A'], allowed_evidence(payload))
    validate(schema['properties']['findings'], findings)
    record_findings(scientist, 'job', payload, {'findings': findings})
    record_findings(scientist, 'job', payload, {'findings': findings})
    states = reference_domain(store, payload)['states']
    assert len(states) == 2
    for c, state in zip(bundle, states):
        refs = [r['event'] for r in transitions.representatives(store) if r['state'] == state]
        assert set(refs) == set(c['frames'])
        assert json.loads(store.db.execute('SELECT evidence FROM knowledge WHERE id=?', (state,)).fetchone()[0]) == sorted(c['frames'])
    transitions.register_claim(store, kid, 'A', {'from_state': states[0], 'to_state': states[1], 'probability': 1})
    assert store.db.execute('SELECT count(*) FROM transition_claims').fetchone()[0] == 1
    assert set(transitions.situation_map(store)) == set(map(str, states))
    assert len(json.loads(store.db.execute('SELECT representatives FROM transition_claims').fetchone()[0])) == sum(len(c['frames']) for c in bundle[:2])


def test_clustering_does_not_hold_store_lock(tmp_path, monkeypatch):
    store, _ = seed(tmp_path)
    entered, release, acquired = threading.Event(), threading.Event(), threading.Event()
    original = candidates.cluster
    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(candidates, 'cluster', blocked)
    result, errors = [], []
    def generate():
        try:result.extend(candidates.generate(store, 'job'))
        except BaseException as e:errors.append(e)
    worker = threading.Thread(target=generate)
    worker.start()
    assert entered.wait(5)
    def contender():
        with store.lock:acquired.set()
    other = threading.Thread(target=contender)
    other.start()
    try:assert acquired.wait(.25)
    finally:release.set(); worker.join(5); other.join(5)
    assert not errors and result and not worker.is_alive()
