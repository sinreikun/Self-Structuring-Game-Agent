"""Read-only bookkeeping of the individual's unfinished research chains."""
import json


def calculate(scientist, payload):
    store = scientist.store
    claims = []
    with store.lock:
        for row in store.db.execute('SELECT knowledge FROM research_claims ORDER BY knowledge'):
            kid = row['knowledge']
            if store.db.execute('SELECT 1 FROM research_evaluations WHERE hypothesis=?', (kid,)).fetchone():
                continue
            status = store.db.execute('SELECT validation FROM knowledge WHERE id=?', (kid,)).fetchone()
            if not status or status[0] in ('supported', 'refuted'):
                continue
            samples, _, _ = scientist.experiments.evaluation_samples(kid)
            n = len(samples)
            if n < 16:
                claims.append({'kind': 'claim', 'hypothesis_id': kid, 'samples': n,
                               'required_samples': 16, 'missing_samples': 16-n})
        claims.sort(key=lambda item: (-item['samples'], item['hypothesis_id']))
        hypotheses = [{'kind': 'hypothesis', 'hypothesis_id': row[0], 'claim_count': 0}
                      for row in store.db.execute("""SELECT id FROM knowledge k
                          WHERE kind='hypothesis' AND validation NOT IN ('supported','refuted')
                          AND NOT EXISTS (SELECT 1 FROM research_claims c WHERE c.knowledge=k.id)
                          AND NOT EXISTS (SELECT 1 FROM research_evaluations e WHERE e.hypothesis=k.id)
                          ORDER BY id LIMIT 3""")]
    adopted = payload.get('adopted_situation_candidates', {})
    candidates = [{'kind': 'situation_candidate', 'candidate_id': c['id'],
                   'frame_count': len(c['frames'])}
                  for c in payload.get('situation_candidates', []) if c['id'] not in adopted]
    candidates.sort(key=lambda item: (-item['frame_count'], item['candidate_id']))
    return (claims + hypotheses + candidates)[:3]


def prepend(context, items):
    if not items:
        return context
    return 'Job agenda: ' + json.dumps(items, separators=(',', ':')) + '\n' + context
