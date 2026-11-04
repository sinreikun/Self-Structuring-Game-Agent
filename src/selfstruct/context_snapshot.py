"""Detached, read-only metadata for advisory research context construction."""
import copy
import json
import sqlite3
import threading
from types import SimpleNamespace

from .store import Store


def snapshot(store, job):
    tables = ('research_experiments', 'claims', 'transition_claims',
              'transition_granularity', 'state_frames', 'state_baselines',
              'knowledge', 'research_evaluations', 'corrected_changes')
    with store.lock:
        captured = {}
        ids = {r[0] for r in store.db.execute(
            "SELECT id FROM events WHERE state='complete' ORDER BY id DESC LIMIT 64")}
        for table in tables:
            sql = 'SELECT * FROM ' + table
            args = ()
            if table == 'research_experiments':
                sql += ' WHERE job=?'; args = (job,)
            cursor = store.db.execute(sql, args)
            rows = cursor.fetchall()
            captured[table] = ([c[0] for c in cursor.description], [tuple(r) for r in rows])
            if table == 'research_experiments':
                for row in rows: ids.update(json.loads(row['events']))
            elif table == 'state_frames':
                ids.update(row['event'] for row in rows)
            elif table == 'transition_claims':
                for row in rows:
                    ids.update(r['event'] for r in json.loads(row['representatives']))
        cursor = store.db.execute('SELECT * FROM events WHERE id IN (' + ','.join('?' for _ in ids) + ')', tuple(ids))
        captured['events'] = ([c[0] for c in cursor.description], [tuple(r) for r in cursor])
        root = store.root
    # Build the private query view and decode images only after releasing Store.
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    try:
        for table, (columns, rows) in captured.items():
            db.execute('CREATE TABLE ' + table + ' (' + ','.join('"'+c+'"' for c in columns) + ')')
            db.executemany('INSERT INTO ' + table + ' VALUES (' + ','.join('?' for _ in columns) + ')', rows)
        db.execute('''CREATE VIEW research_claims AS
            SELECT knowledge,action,probability,after_id,'change_rate' claim_type,NULL from_state,NULL to_state,NULL representatives FROM claims
            UNION ALL SELECT knowledge,action,probability,after_id,'transition',from_state,to_state,representatives FROM transition_claims''')
        view = SimpleNamespace(db=db, root=root, lock=threading.RLock(), context_snapshot=True)
        view.observed_image = lambda event, phase: Store.observed_image(view, event, phase)
        return view
    except BaseException:
        db.close()
        raise


def scientist_view(scientist, store):
    view = copy.copy(scientist)
    view.store = store
    if getattr(scientist, 'experiments', None) is not None:
        view.experiments = copy.copy(scientist.experiments)
        view.experiments.store = store
    return view
