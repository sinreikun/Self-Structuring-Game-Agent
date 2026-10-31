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
    from PIL import Image
    from selfstruct.sandbox import features
    black=features(Image.new('RGB',(128,96),'black'))[0]
    white=features(Image.new('RGB',(128,96),'white'))[0]
    prediction=f'(if (result i32) (i32.eq (local.get $action) (i32.const 0)) (then (i32.mul (i32.const 1000) (i32.eq (local.get $feature) (i32.const {white})))) (else (i32.mul (i32.const 1000) (i32.and (i32.eq (local.get $action) (i32.const 1)) (i32.eq (local.get $feature) (i32.const {black}))))))'
    proposal = fast.replace('(i32.const -1)', prediction)
    class ConditionalGame(SyntheticGame):
        # A/B set a binary visible state; C leaves it unchanged. Outcome depends
        # on pre-input pixels as well as the action; frequency alone cannot solve it.
        def capture(self):return Image.new('RGB',(128,96),'white' if self.position else 'black')
        def act(self,key,duration,cancel,on_sent=None):
            if cancel.is_set():return
            if on_sent:on_sent(time.time())
            if key=='A':self.position=0
            elif key=='B':self.position=1
            self.held=False
    calls = []
    def complete(self, prompt, **kwargs):
        calls.append(prompt)
        evidence = json.JSONDecoder().raw_decode(prompt.rsplit('経験:\n', 1)[1].strip())[0][0]['id']
        return json.dumps({"findings":[{"text":"入力後の表示変化は高頻度かもしれません。次の経験で再確認します。",
                                        "evidence":[evidence],"action":0,"change_probability":.95}],
                           "problem":"予測が未確認のままです。", "reason":"独立した経験で変化の頻度を予測します。", "wat":proposal})
    monkeypatch.setattr("selfstruct.research.LocalLLM.complete", complete)
    monkeypatch.setattr("selfstruct.research.LocalLLM.count_tokens",lambda self,prompt:2000)
    ctrl = Controller(Config(root=str(tmp_path), keys=["A", "B", "C"], research_every=48,
                             quota_bytes=128*1024**2, restart_after=10000), ConditionalGame())
    fast_id = ctrl.revisions.create(fast)
    ctrl.revisions.promote(fast_id)
    ctrl.revisions.confirm()
    ctrl.start()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            validated=any(row['validation'] in {'supported','reproduced','refuted'} for row in ctrl.store.knowledge_rows())
            if ctrl.store.reports() and any(r['adopted'] for r in ctrl.store.reports()) and not ctrl.revisions.state['trial'] and validated:
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
        assert 32 <= metrics['live']['agent']['n'] <= 128
        assert metrics['live']['agent']['classes']['changed']['n']>=4
        assert metrics['live']['agent']['classes']['unchanged']['n']>=4
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
