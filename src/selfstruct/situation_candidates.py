"""Bounded numeric seeds; model adoption or model claims create knowledge."""
from . import transitions
from .context_snapshot import snapshot

EVENT_LIMIT = 64
CANDIDATE_LIMIT = 6
MIN_FRAMES = 3


def cluster(frames, existing):
    # A leader ball uses exactly the product's distance and identification cutoff.
    groups = []
    for event, value in frames:
        if any(transitions.similarity(value, ref) >= transitions.THRESHOLD for ref in existing):
            continue
        ranked = [(transitions.similarity(value, g[0][1]), i) for i, g in enumerate(groups)]
        if ranked and max(ranked)[0] >= transitions.THRESHOLD:
            groups[max(ranked)[1]].append((event, value))
        else:
            groups.append([(event, value)])
    groups = sorted((g for g in groups if len(g) >= MIN_FRAMES), key=lambda g: (-len(g), g[0][0]))[:CANDIDATE_LIMIT]
    # Reassign against the actual presented representatives (same tie ordering as identify).
    assigned = [[] for _ in groups]
    for event, value in frames:
        if any(transitions.similarity(value, ref) >= transitions.THRESHOLD for ref in existing):continue
        ranked = sorted(((transitions.similarity(value, g[0][1]), i) for i, g in enumerate(groups)), key=lambda p: (-p[0], p[1]))
        if ranked and ranked[0][0] >= transitions.THRESHOLD:assigned[ranked[0][1]].append(event)
    result = []
    for group, ids in zip(groups, assigned):
        representative = group[0][0]
        if representative not in ids or len(ids) < MIN_FRAMES:continue
        ids = [representative] + [i for i in ids if i != representative]
        result.append({'id': 'sc-'+str(representative), 'frames': ids[:transitions.POOL_LIMIT]})
    return result


def generate(store, job):
    view = snapshot(store, job)
    try:
        refs = transitions.representatives(view)
        excluded = {(r['event'], r['phase']) for r in refs}
        existing = []
        for ref in refs:
            try:existing.append(transitions.pixels(view.observed_image(ref['event'], ref['phase'])))
            except (ValueError, OSError):pass
        frames = []
        for row in view.db.execute("SELECT id FROM events WHERE state='complete' ORDER BY id DESC LIMIT ?", (EVENT_LIMIT,)):
            if (row['id'], 'before') in excluded:continue
            try:frames.append((row['id'], transitions.pixels(view.observed_image(row['id'], 'before'))))
            except (ValueError, OSError):pass
        return cluster(frames, existing)
    finally:
        view.db.close()


def agenda(payload):
    return [{'id': c['id'], 'frame_count': len(c['frames']), 'representative_event': c['frames'][0]}
            for c in payload.get('situation_candidates', [])]


def adopt(store, payload, finding):
    if finding.get('state_id') is not None:raise ValueError('Candidate adoption creates a new state')
    if finding.get('phase', 'before') != 'before':raise ValueError('Candidate frames use before phase')
    candidate = next((c for c in payload.get('situation_candidates', []) if c['id'] == finding['candidate_id']), None)
    if candidate is None:raise ValueError('Unknown situation candidate')
    adopted = payload.setdefault('adopted_situation_candidates', {})
    if candidate['id'] in adopted:return adopted[candidate['id']]
    state = transitions.register_state(store, finding['text'], candidate['frames'])
    adopted[candidate['id']] = state
    return state


def claim_candidates(payload):
    return {c['id']: c for c in payload.get('situation_candidates', [])}


def validate_claim_reference(store, payload, value):
    if type(value) is str and value in claim_candidates(payload):
        for event in claim_candidates(payload)[value]['frames']:
            store.observed_image(event, 'before')
        return
    if type(value) is not int or value not in {r['state'] for r in transitions.representatives(store)}:
        raise ValueError('Unknown transition state: '+repr(value))


def register_bundled_claim(store, payload, job, kid, action, claim):
    """Validate first; adoption, provenance and frozen claim share one savepoint.

    Existing Store helpers use connection context managers internally. The local
    facade suppresses their nested commits, without changing Store or its locks.
    Payload references are published only after the savepoint succeeds.
    """
    if not any(type(claim.get(k)) is str for k in ('from_state', 'to_state')):
        transitions.register_claim(store, kid, action, claim)
        return dict(claim)
    import copy
    import json
    import math
    import time
    probability = claim.get('probability')
    if type(probability) not in (int, float) or not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError('Invalid transition probability')
    class Connection:
        def __getattr__(self, key):return getattr(store.db, key)
        def __enter__(self):return self
        def __exit__(self, *args):return False
    with store.lock:
        for field in ('from_state', 'to_state'):
            validate_claim_reference(store, payload, claim[field])
        if not store.db.execute("SELECT 1 FROM knowledge WHERE id=? AND kind='hypothesis'", (kid,)).fetchone():
            raise ValueError('Unknown hypothesis')
        store.reserve(8192 * 2)
        view = copy.copy(store)
        view.db = Connection()
        adopted = dict(payload.get('adopted_situation_candidates', {}))
        normalized = dict(claim)
        store.db.execute('SAVEPOINT candidate_claim')
        try:
            store.db.execute("""CREATE TABLE IF NOT EXISTS candidate_adoptions(
                job TEXT, candidate TEXT, state INTEGER, via TEXT, created REAL,
                PRIMARY KEY(job,candidate))""")
            for field in ('from_state', 'to_state'):
                value = normalized[field]
                if type(value) is not str:continue
                if value not in adopted:
                    frames = claim_candidates(payload)[value]['frames']
                    # No semantic text is manufactured for a model-selected candidate.
                    sid = store.db.execute("INSERT INTO knowledge(kind,text,provenance,validation,evidence,created) VALUES('state','','self_generated_model','hypothesis',?,?)",
                                           (json.dumps(frames), time.time())).lastrowid
                    transitions.register_state(view, '', frames, state=sid)
                    store.db.execute('INSERT INTO candidate_adoptions VALUES(?,?,?,?,?)',
                                     (job, value, sid, 'claim_bundle', time.time()))
                    adopted[value] = sid
                normalized[field] = adopted[value]
            transitions.register_claim(view, kid, action, normalized)
            store.db.execute('RELEASE candidate_claim')
        except BaseException:
            store.db.execute('ROLLBACK TO candidate_claim')
            store.db.execute('RELEASE candidate_claim')
            raise
        payload['adopted_situation_candidates'] = adopted
        return normalized
