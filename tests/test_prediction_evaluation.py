import pytest
from selfstruct.evaluation import sample,summarize,assessment


def measures(labels,probabilities):
    return summarize([sample(i,int(p*1000),y,'A' if i%2 else 'B') for i,(y,p) in enumerate(zip(labels,probabilities))])


def test_single_class_constant_no_longer_promoted_from_unknown():
    base=summarize([sample(i,-1,0,'A') for i in range(48)])
    candidate=measures([0]*48,[0]*48)
    assert candidate['loss']==0 and base['loss']==.25
    passed,reason=assessment(base,candidate)
    assert not passed and '件数不足' in reason


def test_imbalanced_constant_zero_misses_are_visible_and_not_adopted():
    labels=[0]*92+[1]*8
    base=summarize([sample(i,-1,y,'A') for i,y in enumerate(labels)])
    candidate=measures(labels,[0]*100)
    assert candidate['loss']==.08 and candidate['balanced_loss']==.5
    assert candidate['classes']['changed']==dict(n=8,loss=1,misses=8)
    assert not assessment(base,candidate)[0]


def test_discriminating_predictor_beats_constant_and_frequency_references():
    labels=[0,0,1,1]*16
    base=measures(labels,[0]*64)
    candidate=measures(labels,[.1 if y==0 else .9 for y in labels])
    assert assessment(base,candidate)[0]
    assert candidate['balanced_loss']==pytest.approx(.01)
    assert not assessment(candidate,base)[0]


def test_same_predictions_and_weak_changes_have_actionable_reasons():
    labels=[0,1]*16
    base=measures(labels,[0]*32)
    assert '同一' in assessment(base,base)[1]
    weak=measures(labels,[.49,.51]*16)
    assert not assessment(base,weak)[0]
    assert '比較基準' in assessment(base,weak)[1]


def test_reference_frequency_never_uses_current_or_future_outcome():
    a=measures([0,0,0,1],[0]*4)
    b=measures([0,0,0,0],[0]*4)
    # First three causal reference losses are identical; last uses only three zeros.
    expected=(.5**2+(1/3)**2+.25**2+.8**2)/4
    assert a['references']['frequency']['loss']==pytest.approx(expected)
    assert b['references']['frequency']['loss']==pytest.approx((.5**2+(1/3)**2+.25**2+.2**2)/4)


def test_mismatched_experiences_and_legacy_reports_cannot_be_promoted():
    a=measures([0,1]*16,[.1,.9]*16)
    b=measures([0,1]*16,[.1,.9]*16)
    b['samples'][0]['id']=999
    assert not assessment(a,b)[0]
    old=dict(n=32,samples=[dict(id=i,loss=0,known=True) for i in range(32)],seconds=0,unknown=0)
    assert not assessment(old,old)[0]


def test_behavior_failure_is_saved_and_returned_for_bounded_repair(tmp_path,monkeypatch):
    import json
    import selfstruct.research as research
    from selfstruct.store import Store
    from selfstruct.sandbox import Revisions
    store=Store(tmp_path,64*1024**2)
    scientist=research.Scientist(store,Revisions(tmp_path/'agents'),object(),['A'],100,lambda _:None)
    class Ready:
        def wait(self,_):return False
    scientist.stop_event=Ready()
    monkeypatch.setattr(store,'rows',lambda *a,**k:[dict(id=i,change=i%2) for i in range(1,49)])
    monkeypatch.setattr(store,'regression_rows',lambda *a,**k:[])
    monkeypatch.setattr(research,'evaluate',lambda *a,**k:dict(ok=True,passed=False,reason='現行と同一の予測',baseline={},candidate={}))
    jid=store.job('research',{})
    try:
        scientist.evaluate_proposal('preserved source',0,jid)
        job=store.get_job(jid);payload=json.loads(job['payload'])
        assert job['state']=='pending' and payload['behavior_repairs']==1
        assert 'preserved source' in payload['repair_response'] and '同一' in payload['repair_error']
        assert 'source' not in payload
        scientist.evaluate_proposal('preserved source',0,jid)
        scientist.evaluate_proposal('preserved source',0,jid)
        job=store.get_job(jid);payload=json.loads(job['payload'])
        assert job['state']=='complete' and payload['outcome']=='evaluated_not_adopted'
        assert not payload['implementation_verified'] and payload['source']=='preserved source'
        assert len(store.reports())==3
    finally:store.close()


def test_repaired_candidate_uses_only_experiences_after_generation(tmp_path,monkeypatch):
    import json
    from selfstruct.research import Scientist
    from selfstruct.store import Store
    from selfstruct.sandbox import Revisions
    class Client:
        def complete(self,*args,**kwargs):return json.dumps(dict(findings=[],wat='candidate'))
    store=Store(tmp_path,64*1024**2)
    scientist=Scientist(store,Revisions(tmp_path/'agents'),Client(),['A'],100,lambda _:None)
    jid=store.job('research',{'after':120})
    monkeypatch.setattr(store,'rows',lambda *a,**k:[dict(id=200)])
    cutoffs=[]
    scientist.evaluate_proposal=lambda source,after,job:cutoffs.append(after)
    try:
        scientist.study([dict(id=1,action='A',feature=7,change=0)],jid)
        assert cutoffs==[200]
    finally:store.close()


def test_historical_zero_baseline_does_not_block_useful_calibration():
    from selfstruct.evaluation import regression_safe
    labels=[0,0,1,1]*16
    old=measures(labels,[0]*64)
    useful=measures(labels,[.2 if y==0 else .9 for y in labels])
    assert useful['classes']['unchanged']['loss']>.02
    assert regression_safe(old,useful)
    broken=measures(labels,[.9 if y==0 else .1 for y in labels])
    assert not regression_safe(old,broken)
