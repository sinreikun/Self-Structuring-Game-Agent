"""Compiled skills: verified situated transitions the fast layer can reuse.

Compilation is a Host bookkeeping act over final statistical verdicts, at the
same seam as the job-end automatic evaluation. The Host authors no effect: a
skill exists only where a pre-registered transition claim was supported with
at least the required samples. Skill names come from model-authored state
text or situation cards when available.
"""
import json
import time


def setup(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS skills(id INTEGER PRIMARY KEY, name TEXT,
      from_state INTEGER, action TEXT, to_state INTEGER, claim INTEGER, created REAL);
    CREATE TABLE IF NOT EXISTS skill_uses(id INTEGER PRIMARY KEY, skill INTEGER,
      event INTEGER, arrived INTEGER, created REAL);
    """)


def _final_supported(store):
    """Final evaluations (pre-registered, n>=required, supported) per claim."""
    result = {}
    for row in store.db.execute('SELECT id,result FROM research_evaluations ORDER BY id'):
        value = json.loads(row['result'])
        if (value.get('claim_type') == 'transition' and value.get('pre_registered')
                and value.get('n', 0) >= 16 and value.get('status') == 'supported'):
            result[value['hypothesis']] = value
    return result


def compile_supported(store):
    """Create one skill per finally-supported transition claim. Idempotent.

    Caller holds the store lock (the job-end sweep in research_memory does);
    this function only reads and inserts factual bookkeeping rows.
    """
    created = []
    for kid, value in _final_supported(store).items():
        if store.db.execute('SELECT 1 FROM skills WHERE claim=?', (kid,)).fetchone():
            continue
        target = value['registered_target']
        name = None
        card = store.db.execute('SELECT label FROM situation_cards WHERE state=?',
                                (target['from_state'],)).fetchone()
        if card and card[0]:
            name = card[0]
        else:
            row = store.db.execute('SELECT text FROM knowledge WHERE id=?',
                                   (target['from_state'],)).fetchone()
            if row and row[0]:
                name = row[0]
        action = store.db.execute('SELECT action FROM transition_claims WHERE knowledge=?',
                                  (kid,)).fetchone()
        if not action:
            continue
        skill = store.db.execute(
            'INSERT INTO skills(name,from_state,action,to_state,claim,created) VALUES(?,?,?,?,?,?)',
            (name or f'skill-{kid}', target['from_state'], action[0],
             target['to_state'], kid, time.time())).lastrowid
        created.append(skill)
    return created


def for_situation(store, state):
    with store.lock:
        return [dict(r) for r in store.db.execute(
            'SELECT * FROM skills WHERE from_state=? ORDER BY id', (state,))]


def all_skills(store):
    with store.lock:
        return [dict(r) for r in store.db.execute('SELECT * FROM skills ORDER BY id')]


def record_use(store, skill, event, arrived):
    with store.lock, store.db:
        store.db.execute('INSERT INTO skill_uses(skill,event,arrived,created) VALUES(?,?,?,?)',
                         (skill, event, 1 if arrived else 0, time.time()))
