import time
import pytest
from PIL import Image
from selfstruct.config import Config
from selfstruct.engine import Controller
from selfstruct.smoke import SyntheticGame
from selfstruct import transitions
from selfstruct.research_actions import run_actions, action_budget_used


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


def tag(c, eid, situation):
    with c.store.db:
        c.store.db.execute('INSERT OR REPLACE INTO event_situations VALUES(?,?)', (eid, situation))


def payload_for(c, kid):
    return {'research_rows': c.store.rows(), 'research_started': time.time(),
            'research_calls': 1, 'memory_mode': 'pull', 'situation_candidates': [],
            'hypotheses': {'h1': kid}}


def test_experiment_refused_when_current_situation_differs(world):
    c = world
    black = solid(c, (0, 0, 0)); white = solid(c, (255, 255, 255))
    dark_ids = [event(c, black, black, 0.0) for _ in range(3)]
    bright_ids = [event(c, white, white, 0.0) for _ in range(3)]
    dark = transitions.register_state(c.store, 'dark', dark_ids)
    bright = transitions.register_state(c.store, 'bright', bright_ids)
    kid = c.store.knowledge('hypothesis', 'A changes dark', 'inferred', 'hypothesis', dark_ids[:1])
    refs = transitions.representatives(c.store)
    import json
    c.store.claim(kid, 'A', 0.5, 0, situation=dark,
                  representatives=json.dumps([r for r in refs if r['state'] == dark]))
    tag(c, bright_ids[-1], bright)  # the game now stands in the bright situation
    payload = payload_for(c, kid)
    jid = c.store.job('research', payload)
    run_actions(c.scientist, jid, payload,
                [{'op': 'experiment', 'hypothesis': 'h1', 'action': 0, 'repeat': 2}])
    result = payload['action_history'][-1]['result']
    assert result['code'] == 'situation_mismatch'
    assert result['claim_situation'] == dark and result['current_situation'] == bright
    assert result['action_charged'] is False
    assert action_budget_used(payload) == 0
    assert c.store.db.execute('SELECT count(*) FROM research_experiments').fetchone()[0] == 0
    # Same claim, but the game is back in the anchored situation: it runs.
    # (research_calls advances between batches, as in production, so the
    # second batch's action tokens differ from the first batch's.)
    tag(c, bright_ids[-1], dark)
    payload['research_calls'] = 2
    run_actions(c.scientist, jid, payload,
                [{'op': 'experiment', 'hypothesis': 'h1', 'action': 0, 'repeat': 1}])
    result = payload['action_history'][-1]['result']
    assert result.get('code') != 'situation_mismatch'
    assert c.store.db.execute('SELECT count(*) FROM research_experiments').fetchone()[0] == 1


def test_experiment_allowed_when_current_situation_unknown(world):
    c = world
    black = solid(c, (0, 0, 0)); white = solid(c, (255, 255, 255))
    white_ids = [event(c, white, white, 0.0) for _ in range(3)]
    bright = transitions.register_state(c.store, 'bright', white_ids)
    kid = c.store.knowledge('hypothesis', 'A changes bright', 'inferred', 'hypothesis', white_ids[:1])
    import json
    refs = transitions.representatives(c.store)
    c.store.claim(kid, 'A', 0.5, 0, situation=bright,
                  representatives=json.dumps([r for r in refs if r['state'] == bright]))
    event(c, black, black, 0.0)  # newest event: black, untagged, identifies to nothing
    payload = payload_for(c, kid)
    jid = c.store.job('research', payload)
    run_actions(c.scientist, jid, payload,
                [{'op': 'experiment', 'hypothesis': 'h1', 'action': 0, 'repeat': 1}])
    result = payload['action_history'][-1]['result']
    assert result.get('code') != 'situation_mismatch'
    assert c.store.db.execute('SELECT count(*) FROM research_experiments').fetchone()[0] == 1


def test_transition_claim_not_blocked_by_mismatch(world):
    # Scope: the refusal covers change-rate claims only. Transition claims
    # verify every sample's from-state at counting time, so an experiment
    # elsewhere simply yields non-counting samples, as before.
    c = world
    black = solid(c, (0, 0, 0)); white = solid(c, (255, 255, 255))
    dark_ids = [event(c, black, black, 0.0) for _ in range(3)]
    bright_ids = [event(c, white, white, 0.0) for _ in range(3)]
    dark = transitions.register_state(c.store, 'dark', dark_ids)
    bright = transitions.register_state(c.store, 'bright', bright_ids)
    kid = c.store.knowledge('hypothesis', 'A moves dark to bright', 'inferred', 'hypothesis', dark_ids[:1])
    transitions.register_claim(c.store, kid, 'A', {'from_state': dark, 'to_state': bright, 'probability': 0.8})
    tag(c, bright_ids[-1], bright)
    payload = payload_for(c, kid)
    jid = c.store.job('research', payload)
    run_actions(c.scientist, jid, payload,
                [{'op': 'experiment', 'hypothesis': 'h1', 'action': 0, 'repeat': 1}])
    result = payload['action_history'][-1]['result']
    assert result.get('code') != 'situation_mismatch'
    assert c.store.db.execute('SELECT count(*) FROM research_experiments').fetchone()[0] == 1
