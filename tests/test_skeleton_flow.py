import json, time
import pytest
from PIL import Image
from selfstruct.config import Config
from selfstruct.engine import Controller
from selfstruct.smoke import SyntheticGame
from selfstruct import transitions, skills
from selfstruct.job_agenda import calculate
from selfstruct.single_link import assign, finish as link_finish
from selfstruct.research_actions import run_actions


@pytest.fixture
def world(tmp_path):
    c = Controller(Config(root=str(tmp_path), keys=['A', 'B'], research_every=100000), SyntheticGame())
    yield c
    c.close()


def solid(c, rgb):
    return c.store.image(Image.new('RGB', (64, 48), rgb))


def event(c, before, after, change=0.5):
    eid = c.store.begin(c.session, before, 'A', 0.05, 1, c.agent_id, 0, 0.0)
    c.store.finish(eid, after, change, 0.01)
    return eid


def test_bootstrap_registers_recurring_situations(world):
    c = world
    black = solid(c, (0, 0, 0))
    for _ in range(4):
        event(c, black, black, 0.0)
    created = transitions.bootstrap_situations(c.store)
    assert created
    row = c.store.db.execute('SELECT text FROM knowledge WHERE id=?', (created[0],)).fetchone()
    assert row[0] == ''
    assert c.store.db.execute('SELECT count(*) FROM state_frames WHERE state=?', (created[0],)).fetchone()[0] >= 3


def test_play_loop_tags_events_with_situation(tmp_path):
    c = Controller(Config(root=str(tmp_path), keys=['A'], research_every=100000), SyntheticGame())
    try:
        for _ in range(6):
            c.step()
        ids = [r['id'] for r in c.store.rows()]
        transitions.register_state(c.store, 'recent', ids[-3:])
        c.step()
        row = c.store.db.execute('SELECT situation FROM event_situations WHERE event=?', (c.last_eid,)).fetchone()
        assert row is not None and row[0] is not None
    finally:
        c.close()


def test_situation_card_flow(world):
    c = world
    black = solid(c, (0, 0, 0))
    ids = [event(c, black, black, 0.0) for _ in range(3)]
    sid = transitions.register_unnamed_state(c.store, ids)
    items = calculate(c.scientist, {})
    assert items and items[0] == {'kind': 'situation_card', 'state_id': sid}
    payload = {'assigned_link': {'kind': 'situation_card', 'state_id': sid},
               'research_rows': c.store.rows(), 'research_started': time.time(),
               'research_calls': 1, 'memory_mode': 'pull', 'situation_candidates': []}
    jid = c.store.job('research', payload)
    assign(c.scientist, payload)
    assert not link_finish(c.scientist, jid, payload)
    run_actions(c.scientist, jid, payload,
                [{'op': 'describe_situation', 'state': sid, 'label': '暗い画面',
                  'scene_kind': 'menu', 'role': 'navigation'}])
    card = transitions.cards(c.store)[str(sid)]
    assert card['label'] == '暗い画面' and card['scene_kind'] == 'menu' and card['role'] == 'navigation'
    assert link_finish(c.scientist, jid, payload)
    assert all(i.get('state_id') != sid for i in calculate(c.scientist, {}))


def test_change_rate_claim_requires_situation(world):
    c = world
    black = solid(c, (0, 0, 0))
    first = event(c, black, black, 0.0)
    kid = c.store.knowledge('hypothesis', 'h', 'inferred', 'hypothesis', [first])
    payload = {'research_rows': c.store.rows(), 'research_started': time.time(),
               'research_calls': 1, 'memory_mode': 'pull', 'situation_candidates': [],
               'hypotheses': {'h1': kid}}
    jid = c.store.job('research', payload)
    run_actions(c.scientist, jid, payload,
                [{'op': 'experiment', 'hypothesis': 'h1', 'action': 0,
                  'claim': {'type': 'change_rate', 'probability': 0.5}}])
    result = payload['action_history'][-1]['result']
    assert result['state'] == 'claim_required'
    assert any(f['field'] == 'claim.situation' for f in result['fields'])


def test_anchored_change_rate_samples(world):
    c = world
    black = solid(c, (0, 0, 0)); white = solid(c, (255, 255, 255))
    in_ids = [event(c, black, white, 0.9) for _ in range(3)]
    out_ids = [event(c, white, black, 0.9) for _ in range(2)]
    sid = transitions.register_state(c.store, 'dark', in_ids[:2])
    kid = c.store.knowledge('hypothesis', 'dark changes', 'inferred', 'hypothesis', in_ids[:1])
    refs = json.dumps(transitions.representatives(c.store))
    c.store.claim(kid, 'A', 0.5, 0, situation=sid, representatives=refs)
    with c.store.db:
        c.store.db.execute('INSERT INTO research_experiments VALUES(?,?,?,?,?,?,?,?,?,?)',
                           ('exp-anchor', 'job-x', kid, json.dumps({'action': 0, 'repeat': 5}),
                            'complete', json.dumps(in_ids + out_ids), '{}', 0, 0, 0))
    rows, _, _ = c.experiments.evaluation_samples(kid)
    assert sorted(r['id'] for r in rows) == sorted(in_ids)


def test_skill_compilation_from_supported_transition(world):
    c = world
    black = solid(c, (0, 0, 0)); white = solid(c, (255, 255, 255))
    a_ids = [event(c, black, white, 0.9) for _ in range(2)]
    b_ids = [event(c, white, black, 0.9) for _ in range(2)]
    sa = transitions.register_state(c.store, 'dark room', a_ids)
    sb = transitions.register_state(c.store, 'bright room', b_ids)
    kid = c.store.knowledge('hypothesis', 'A moves dark to bright', 'inferred', 'hypothesis', a_ids)
    transitions.register_claim(c.store, kid, 'A', {'from_state': sa, 'to_state': sb, 'probability': 0.8})
    result = {'claim_type': 'transition', 'pre_registered': True, 'n': 16, 'status': 'supported',
              'hypothesis': kid,
              'registered_target': {'from_state': sa, 'to_state': sb, 'action': 0, 'probability': 0.8, 'after_id': 0}}
    with c.store.db:
        c.store.db.execute('INSERT INTO research_evaluations(job,hypothesis,result,created) VALUES(?,?,?,?)',
                           ('job-y', kid, json.dumps(result), 0))
    created = skills.compile_supported(c.store)
    assert len(created) == 1
    found = skills.for_situation(c.store, sa)
    assert found and found[0]['to_state'] == sb and found[0]['action'] == 'A' and found[0]['name'] == 'dark room'
    assert skills.compile_supported(c.store) == []
    skills.record_use(c.store, found[0]['id'], a_ids[0], True)
    assert c.store.db.execute('SELECT arrived FROM skill_uses').fetchone()[0] == 1


def test_play_loop_uses_compiled_skill(tmp_path):
    c = Controller(Config(root=str(tmp_path), keys=['A'], research_every=100000), SyntheticGame())
    try:
        for _ in range(6):
            c.step()
        ids = [r['id'] for r in c.store.rows()]
        sid = transitions.register_state(c.store, 'recent', ids[-3:])
        with c.store.db:
            c.store.db.execute('INSERT INTO skills(name,from_state,action,to_state,claim,created) VALUES(?,?,?,?,?,?)',
                               ('recent skill', sid, 'A', sid, 0, time.time()))
        for _ in range(6):
            c.step()
        uses = c.store.db.execute('SELECT count(*) FROM skill_uses').fetchone()[0]
        assert uses >= 1
    finally:
        c.close()
