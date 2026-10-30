from dataclasses import replace
import json
from pathlib import Path
import time

from selfstruct.config import Config
from selfstruct.engine import Controller
from selfstruct.smoke import SyntheticGame


def test_autonomous_research_candidate_live_promotion_and_rollback(tmp_path, monkeypatch):
    seed = Path("src/selfstruct/assets/seed.wat").read_text(encoding="utf-8")
    # Fast observation is part of the test fixture, not a real-game performance claim.
    fast = seed.replace('(i32.const 80)', '(i32.const 10)')
    proposal = fast.replace('(i32.const -1)', '(i32.const 950)')
    calls = []
    def complete(self, prompt, **kwargs):
        calls.append(prompt)
        evidence = json.loads(prompt.rsplit('経験:\n', 1)[1].strip())[0]['id']
        return json.dumps({"findings":[{"text":"入力後の表示変化は高頻度かもしれません。次の経験で再確認します。",
                                        "evidence":[evidence],"action":0,"change_probability":.95}],
                           "problem":"予測が未確認のままです。", "reason":"独立した経験で変化の頻度を予測します。", "wat":proposal})
    monkeypatch.setattr("selfstruct.research.LocalLLM.complete", complete)
    ctrl = Controller(Config(root=str(tmp_path), keys=["A", "B"], research_every=48,
                             quota_bytes=128*1024**2, restart_after=10000), SyntheticGame())
    fast_id = ctrl.revisions.create(fast)
    ctrl.revisions.promote(fast_id)
    ctrl.revisions.confirm()
    ctrl.start()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if ctrl.store.reports() and any(r['adopted'] for r in ctrl.store.reports()) and not ctrl.revisions.state['trial']:
                break
            time.sleep(.03)
        ctrl.pause()
        assert calls, ctrl.error
        reports = ctrl.store.reports()
        assert any(row['adopted'] for row in reports), reports
        assert ctrl.revisions.active != fast_id
        assert not ctrl.revisions.state['trial']
        assert any(row['validation'] in {'supported','reproduced','refuted'} for row in ctrl.store.knowledge_rows())
        adopted = next(row for row in reports if row['adopted'])
        metrics = json.loads(adopted['metrics'])
        assert metrics['replay']['candidate']['n'] >= 48
        assert metrics['live']['agent']['n'] == 32
        assert not ctrl.game.held
    finally:
        ctrl.close()


def test_runtime_failure_releases_and_rolls_back(tmp_path):
    seed = Path('src/selfstruct/assets/seed.wat').read_text(encoding="utf-8")
    ctrl = Controller(Config(root=str(tmp_path), keys=['A','B'], research_every=10000), SyntheticGame())
    stable = ctrl.revisions.active
    bad = seed.replace('(i32.const 60)', '(loop $forever (br $forever)) (i32.const 60)')
    candidate = ctrl.revisions.create(bad)
    ctrl.revisions.promote(candidate)
    ctrl.start()
    try:
        deadline = time.monotonic()+5
        while ctrl.steps < 3 and time.monotonic() < deadline:
            time.sleep(.02)
        # The recovered player continues issuing actions; only a paused player
        # is required to have no key held at the instant we inspect it.
        ctrl.pause()
        assert ctrl.steps >= 3
        assert ctrl.revisions.active == stable
        assert not ctrl.game.held
    finally:
        ctrl.close()
