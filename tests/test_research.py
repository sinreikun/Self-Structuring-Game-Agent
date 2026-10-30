from pathlib import Path

from PIL import Image
import pytest

from selfstruct.research import LocalLLM, PROMPT, evaluate
from selfstruct.store import Store


def test_prompt_is_well_formed():
    prompt = PROMPT.format(source="(module)", rows="[]")
    assert '"findings"' in prompt


def test_remote_inference_rejected():
    with pytest.raises(ValueError):
        LocalLLM("https://remote.invalid/v1", "model")


def test_isolated_evaluation_real_process(tmp_path):
    seed = Path("src/selfstruct/assets/seed.wat").read_text(encoding="utf-8")
    store = Store(tmp_path, 64 * 1024**2)
    sid = store.session("Newborn", False)
    before = store.image(Image.new("RGB", (32, 32), "black"))
    after = store.image(Image.new("RGB", (32, 32), "white"))
    for _ in range(32):
        eid = store.begin(sid, before, "A", .06, 1, "seed", 42, -1)
        store.finish(eid, after, 1, .01)
    source = seed.replace('(i32.const -1)', '(i32.const 950)')
    result = evaluate(source, seed, store.rows(), tmp_path / "evidence", ["A"])
    assert result["passed"]
    with pytest.raises(ValueError):
        evaluate(seed.replace('(i32.const 60)', '(loop $forever (br $forever)) (i32.const 60)'), seed,
                 store.rows(), tmp_path / "evidence", ["A"])
    store.close()


def test_managed_inference_recovery_keeps_pending_work(tmp_path):
    import time
    from urllib.error import URLError
    from selfstruct.research import Scientist
    from selfstruct.sandbox import Revisions
    store = Store(tmp_path,64*1024**2)
    sid = store.session('Newborn',False)
    digest = store.image(Image.new('RGB',(32,32)))
    for _ in range(48):
        eid = store.begin(sid,digest,'A',.06,1,'seed',1,-1)
        store.finish(eid,digest,0,.001)
    scientist = Scientist(store,Revisions(tmp_path/'revisions'),LocalLLM('http://127.0.0.1:8766/v1','local'),['A'],48,lambda message:None)
    calls, recoveries = [], []
    def study(rows,jid):
        calls.append(jid)
        if len(calls)<=2:
            raise URLError('inference process terminated')
        store.set_job(jid,'complete')
    scientist.retry_base_delay = .01  # Exercise retry/recovery without production backoff delays.
    scientist.study = study
    scientist.recover_llm = lambda:recoveries.append(True)
    scientist.start()
    try:
        deadline = time.monotonic()+3
        while len(calls)<3 and time.monotonic()<deadline:
            scientist.tick()
            time.sleep(.01)
        assert len(calls)==3 and len(set(calls))==1
        assert len(recoveries)==1
        assert not store.jobs()
    finally:
        scientist.close()
        store.close()
