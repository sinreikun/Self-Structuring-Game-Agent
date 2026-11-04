"""Bounded numeric seeds; only model adoption creates knowledge."""
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
