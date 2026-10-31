"""Prediction evaluation. References observe labels only after predicting each row."""
import math
import statistics


def sample(identifier, prediction, outcome, action):
    probability=.5 if prediction==-1 else prediction/1000
    return dict(id=identifier,prediction=probability,outcome=outcome,action=action,
                loss=(probability-outcome)**2,known=prediction>=0)


def summarize(samples, seconds=0):
    result=dict(n=len(samples),loss=statistics.mean(s['loss'] for s in samples) if samples else 0,
                unknown=statistics.mean(not s['known'] for s in samples) if samples else 1,
                seconds=seconds,samples=samples)
    classes={}
    for outcome,name in ((0,'unchanged'),(1,'changed')):
        rows=[s for s in samples if s['outcome']==outcome]
        classes[name]=dict(n=len(rows),loss=statistics.mean(s['loss'] for s in rows) if rows else None,
                           misses=sum(s['prediction']<.5 for s in rows) if outcome else None)
        if not outcome:classes[name]['false_positives']=sum(s['prediction']>=.5 and s['known'] for s in rows)
    result['classes']=classes
    result['balanced_loss']=balanced(samples)
    known=[s['prediction'] for s in samples if s['known']]
    result['prediction_range']=max(known)-min(known) if known else 0
    totals=[0,0];actions={};references={k:[] for k in ('zero','half','one','frequency','action_frequency')}
    for row in samples:
        counts=actions.setdefault(row['action'],[0,0])
        probs=dict(zero=0,half=.5,one=1,frequency=(totals[1]+1)/(sum(totals)+2),
                   action_frequency=(counts[1]+1)/(sum(counts)+2))
        for name,p in probs.items():references[name].append(dict(row,loss=(p-row['outcome'])**2))
        totals[row['outcome']]+=1;counts[row['outcome']]+=1
    result['references']={name:dict(loss=statistics.mean(r['loss'] for r in rows) if rows else 0,
                                   balanced_loss=balanced(rows)) for name,rows in references.items()}
    return result


def balanced(rows):
    groups=[[s['loss'] for s in rows if s['outcome']==y] for y in (0,1)]
    return (statistics.mean(groups[0])+statistics.mean(groups[1]))/2 if all(groups) else None


def assessment(base, candidate):
    a,b=base.get('samples',[]),candidate.get('samples',[])
    if len(a)<24 or len(a)!=len(b) or base['n']!=len(a) or candidate['n']!=len(b):
        return False,'比較件数不足（24件以上が必要）'
    if any(x.get('id')!=y.get('id') or x.get('outcome')!=y.get('outcome') for x,y in zip(a,b)):
        return False,'比較経験が一致していません'
    if any('outcome' not in x or 'prediction' not in x for x in a+b):return False,'旧形式の評価です。新しい経験で再評価が必要です'
    if min(sum(s['outcome']==y for s in b) for y in (0,1))<4:
        return False,'変化あり・なしの比較件数不足（各4件以上が必要）'
    if all(x['prediction']==y['prediction'] and x['known']==y['known'] for x,y in zip(a,b)):
        return False,'現行と同一の予測です。説明だけでなく出力を変える実装が必要です'
    cm=summarize(b,candidate['seconds']);bm=summarize(a,base['seconds'])
    if cm['prediction_range']<.01:return False,'定数に近い予測です。状態・入力・経験に応じた予測の違いを確認できません'
    reference=min(v['balanced_loss'] for v in cm['references'].values())
    if cm['balanced_loss']>=reference-.01:return False,'定数・過去頻度の比較基準を上回っていません'
    gains=[]
    for y in (0,1):
        group=[x['loss']-z['loss'] for x,z in zip(a,b) if z['outcome']==y]
        gains.append(group)
    mean=sum(statistics.mean(g) for g in gains)/2
    bound=1.96*math.sqrt(sum((statistics.variance(g)/len(g)) for g in gains)/4)
    if mean<=max(.02,bound):return False,'変化あり・なしを均等に評価した改善が統計的に不足しています'
    if candidate['unknown']>base['unknown']:return False,'未確認率が増えています'
    if candidate['seconds']>max(.005,base['seconds']*2):return False,'評価の実行時間が増えすぎています'
    return True,'両クラスと比較基準で改善を確認しました'


def regression_safe(base, candidate):
    """Permit calibration tradeoffs against an all-zero historical baseline.

    Balanced performance must remain stable; protect each sufficiently observed
    class from a large deterioration rather than preserving every zero prediction.
    """
    before,after=base.get('balanced_loss'),candidate.get('balanced_loss')
    if before is not None and after is not None and after>before+.02:return False
    for label in ('changed','unchanged'):
        old=base.get('classes',{}).get(label,{})
        new=candidate.get('classes',{}).get(label,{})
        if min(old.get('n',0),new.get('n',0))>=4 and new['loss']>old['loss']+.16:return False
    return True
