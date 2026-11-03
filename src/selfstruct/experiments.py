"""Durable research input plans. Only Player consumes them; Scientist never sends keys."""
import hashlib
import json
import math
import time

from .vision import integer, transform
from PIL import Image, ImageStat


def setup(db):
    db.execute('''CREATE TABLE IF NOT EXISTS research_evaluations(
      id INTEGER PRIMARY KEY, job TEXT, hypothesis INTEGER, result TEXT, created REAL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS research_experiments(
      id TEXT PRIMARY KEY, job TEXT, hypothesis INTEGER, request TEXT, state TEXT,
      events TEXT, result TEXT, attempts INTEGER, created REAL, updated REAL)''')
    db.execute("UPDATE research_experiments SET state='interrupted',updated=? WHERE state IN ('queued','running')",(time.time(),))
    db.execute("UPDATE knowledge SET validation='inconclusive' WHERE validation='testing' AND id IN (SELECT hypothesis FROM research_experiments WHERE state='interrupted')")


def validate(request,keys):
    if not isinstance(request,dict):raise ValueError('Experiment must be an object')
    action=integer(request.get('action'),0,len(keys)-1)
    duration=integer(request.get('duration',60),10,500)
    wait=integer(request.get('wait',100),10,2000)
    repeat=integer(request.get('repeat',1),1,8)
    condition=request.get('condition')
    if condition is not None:
        if not isinstance(condition,dict) or set(condition)-{'box','size','mode','operations','mean_min','mean_max','max_wait_steps'}:raise ValueError('Invalid observation condition')
        for key in ('mean_min','mean_max'):
            if key in condition and (type(condition[key]) not in (int,float) or not math.isfinite(condition[key]) or not 0<=condition[key]<=255):raise ValueError('Invalid mean range')
        integer(condition.get('max_wait_steps',32),1,128)
        if not any(k in condition for k in ('mean_min','mean_max')):raise ValueError('Condition requires a mean range')
        if 'box' in condition:
            from .vision import box_for
            box_for(condition['box'],[32768,32768])
        transform(Image.new('RGB',(1,1)),{k:v for k,v in condition.items() if k!='box'})
        if condition.get('mean_min',0)>condition.get('mean_max',255):raise ValueError('Invalid mean range')
    return {'action':action,'duration':duration,'wait':wait,'repeat':repeat,**({'condition':condition} if condition is not None else {})}


class PreregistrationRequired(ValueError):
    def __init__(self,hypothesis):
        self.hypothesis=hypothesis
        self.reasons=[{'code':'not_preregistered','detail':
            f'仮説ID {hypothesis}: action と change_probability の事前登録が必要です。その仮説のfindingsに2項目を付けて登録してから実験を要求し直してください。現行登録機構で既存IDへ追加できない場合は、供給された新しいfindings.idで主張と2項目を登録し、そのIDを指定してください。登録なしの実験結果は評価で支持・反証の根拠に使えません。'}]
        super().__init__(self.reasons[0]['detail'])


class EvaluationRequired(ValueError):
    def __init__(self,pending):
        self.pending=pending
        self.reasons=[{'code':'evaluation_required','detail':
            f'成熟した未評価仮説 {pending}。先に各hypothesisへevaluateを実行してください。評価が記録されればその仮説の制限は解除され、全件評価後に新規実験・仮説・claim登録を再開できます。'}]
        super().__init__(self.reasons[0]['detail'])


class Experiments:
    def __init__(self,store,keys,activity):self.store,self.keys,self.activity=store,keys,activity

    def require_preregistration(self,hypothesis):
        if hypothesis is None:return
        with self.store.lock:
            if not self.store.db.execute("SELECT 1 FROM knowledge WHERE id=? AND kind='hypothesis'",(hypothesis,)).fetchone():raise ValueError('Unknown hypothesis')
            claim=self.store.db.execute('SELECT action,probability FROM claims WHERE knowledge=?',(hypothesis,)).fetchone()
            if not claim or claim['action'] not in self.keys or type(claim['probability']) not in (int,float) or not math.isfinite(claim['probability']) or not 0<=claim['probability']<=1:
                raise PreregistrationRequired(hypothesis)

    def submit(self,job,token,request,hypothesis=None):
        plan=validate(request,self.keys)
        identifier=hashlib.sha256((job+':'+token).encode()).hexdigest()[:32]
        with self.store.lock,self.store.db:
            existing=self.store.db.execute('SELECT id FROM research_experiments WHERE id=?',(identifier,)).fetchone()
            if existing:return identifier
            self.require_evaluation(job)
            self.require_preregistration(hypothesis)
            used=self.store.db.execute('SELECT request FROM research_experiments WHERE job=?',(job,)).fetchall()
            if len(used)>=4 or sum(json.loads(r[0])['repeat'] for r in used)+plan['repeat']>32:raise ValueError('Experiment budget exhausted')
            self.store.reserve(16384)
            now=time.time()
            self.store.db.execute('INSERT INTO research_experiments VALUES(?,?,?,?,?,?,?,?,?,?)',(identifier,job,hypothesis,json.dumps(plan),'queued','[]','{}',0,now,now))
            if hypothesis:self.store.db.execute("UPDATE knowledge SET validation='testing' WHERE id=?",(hypothesis,))
        self.activity.record('EXPERIMENT',f'queued job={job} id={identifier} hypothesis={hypothesis} action={plan["action"]} repeat={plan["repeat"]}')
        return identifier

    def get(self,identifier):
        with self.store.lock:
            row=self.store.db.execute('SELECT * FROM research_experiments WHERE id=?',(identifier,)).fetchone()
            if not row:return None
            row=dict(row)
            if row['state'] not in ('queued','running') and row['result']=='{}':
                ids=json.loads(row['events'])
                events=[dict(r) for r in self.store.db.execute('SELECT id,action,change,before_hash,after_hash,state FROM events WHERE id IN ('+','.join('?' for _ in ids)+')',ids)] if ids else []
                row['result']=json.dumps({'experiment':identifier,'job':row['job'],'hypothesis':row['hypothesis'],'state':row['state'],'events':events,'completed':sum(r['state']=='complete' for r in events),'causality':'unverified'})
            return row

    def finish(self,identifier,state):
        with self.store.lock,self.store.db:
            row=self.get(identifier)
            if not row or row['state'] in ('complete','interrupted','expired'):return
            ids=json.loads(row['events'])
            rows=[dict(r) for r in self.store.db.execute('SELECT id,action,change,before_hash,after_hash,state FROM events WHERE id IN ('+','.join('?' for _ in ids)+')',ids)] if ids else []
            result={'experiment':identifier,'job':row['job'],'hypothesis':row['hypothesis'],'state':state,'events':rows,'completed':sum(r['state']=='complete' for r in rows),'causality':'unverified'}
            self.store.db.execute('UPDATE research_experiments SET state=?,result=?,updated=? WHERE id=?',(state,json.dumps(result),time.time(),identifier))
            if row['hypothesis']:self.store.db.execute("UPDATE knowledge SET validation='inconclusive' WHERE id=? AND validation='testing'",(row['hypothesis'],))
            complete=[r['id'] for r in rows if r['state']=='complete']
            if complete:
                self.store.knowledge('experiment','研究Experimentの結果：'+json.dumps(result,ensure_ascii=False),'action_experience','unknown',complete)
        self.activity.record('EXPERIMENT',f'{state} job={row["job"]} id={identifier} events={ids}')

    def next(self,frame):
        with self.store.lock,self.store.db:
            row=self.store.db.execute("SELECT * FROM research_experiments WHERE state IN ('queued','running') ORDER BY created LIMIT 1").fetchone()
            if not row:return None
            row=dict(row);plan=json.loads(row['request']);identifier=row['id']
            ids=json.loads(row['events'])
            if len(ids)>=plan['repeat']:
                complete=self.store.db.execute("SELECT count(*) FROM events WHERE state='complete' AND id IN ("+','.join('?' for _ in ids)+')',ids).fetchone()[0]
                self.finish(identifier,'complete' if complete==len(ids) else 'interrupted');return None
            if time.time()-row['created']>120:self.finish(identifier,'expired');return None
            condition=plan.get('condition')
            if condition:
                try:image,_=transform(frame,{**condition,'size':condition.get('size',[32,32])})
                except ValueError:
                    self.finish(identifier,'interrupted');return None
                mean=sum(ImageStat.Stat(image).mean)/len(image.getbands())
                if not condition.get('mean_min',0)<=mean<=condition.get('mean_max',255):
                    attempts=row['attempts']+1
                    self.store.db.execute('UPDATE research_experiments SET attempts=?,updated=? WHERE id=?',(attempts,time.time(),identifier))
                    if attempts>=condition.get('max_wait_steps',32):self.finish(identifier,'expired')
                    return None
            self.store.db.execute("UPDATE research_experiments SET state='running',updated=? WHERE id=?",(time.time(),identifier))
            return {**row,'plan':plan}

    def link(self,identifier,event):
        with self.store.lock,self.store.db:
            row=self.get(identifier)
            if row['state']!='running':return False
            ids=json.loads(row['events']);ids.append(event)
            self.store.pin([event],'experiment-'+identifier)
            self.store.db.execute('UPDATE research_experiments SET events=?,updated=? WHERE id=?',(json.dumps(ids),time.time(),identifier))
            return True

    def completed(self,identifier):
        row=self.get(identifier)
        if row['state']=='running' and len(json.loads(row['events']))>=json.loads(row['request'])['repeat']:self.finish(identifier,'complete')

    def evaluation_samples(self,hypothesis,job=None):
        """Read the exact evidence set used by evaluate; no evaluation or writes."""
        ids=[];conditional=False;experiment_ids=[]
        plans=self.store.db.execute('SELECT id,events,request FROM research_experiments WHERE hypothesis=?'+(' AND job=?' if job is not None else ''),
                                     (hypothesis,job) if job is not None else (hypothesis,)).fetchall()
        for r in plans:
            experiment_ids.append(r['id'])
            ids+=json.loads(r['events']);condition=json.loads(r['request']).get('condition') or {};conditional=conditional or condition.get('mean_min',0)>0 or condition.get('mean_max',255)<255
        ids=sorted(set(ids))
        rows=[dict(r) for r in self.store.db.execute('SELECT id,session,change,action FROM events WHERE state=\'complete\' AND id IN ('+','.join('?' for _ in ids)+')',ids)] if ids else []
        return rows,conditional,experiment_ids

    def mature_unevaluated(self,job):
        with self.store.lock:
            result=[]
            for claim in self.store.db.execute('SELECT * FROM claims'):
                kid=claim['knowledge']
                if self.store.db.execute('SELECT 1 FROM research_evaluations WHERE hypothesis=?',(kid,)).fetchone():continue
                rows,conditional,_=self.evaluation_samples(kid,job)
                if (len(rows)>=16 and not conditional and len({r['session'] for r in rows})==1
                    and all(r['action']==claim['action'] and r['id']>claim['after_id'] for r in rows)):
                    result.append({'hypothesis':kid,'n':len(rows),'required_samples':16})
            return result

    def require_evaluation(self,job):
        pending=self.mature_unevaluated(job)
        if pending:raise EvaluationRequired(pending)

    def evaluate(self,hypothesis,probability=None,job=None):
        if type(hypothesis)!=int or (probability is not None and (type(probability) not in (float,int) or not math.isfinite(probability) or not 0<=probability<=1)):raise ValueError('Expected hypothesis ID and optional change_probability')
        with self.store.lock,self.store.db:
            hypothesis_row=self.store.db.execute("SELECT * FROM knowledge WHERE id=? AND kind='hypothesis'",(hypothesis,)).fetchone()
            if not hypothesis_row:raise ValueError('Unknown hypothesis')
            rows,conditional,experiment_ids=self.evaluation_samples(hypothesis,job)
            # A small experiment cannot prove support. Different inputs must not be pooled.
            n=len(rows);p=sum(r['change']>.01 for r in rows)/n if n else None
            status='inconclusive';interval=None
            claim=self.store.db.execute('SELECT action,probability,after_id FROM claims WHERE knowledge=?',(hypothesis,)).fetchone()
            if probability is None and claim:probability=claim['probability']
            reasons=[]
            if not claim:reasons.append({'code':'not_preregistered','detail':'新しい仮説IDのfindingsにactionとchange_probabilityを先に登録し、その後に新しい実験を実行してください。既存結果へ事後登録はできません。'})
            elif claim['probability']!=probability:reasons.append({'code':'target_mismatch','detail':'評価対象の確率が事前登録と異なります。主張を変える場合は新しい仮説IDと新しい実験が必要です。'})
            if conditional:reasons.append({'code':'conditional_selection','detail':'選別した条件下の結果から無条件の入力確率を判定しません。'})
            if n<16:reasons.append({'code':'insufficient_samples','detail':f'完了経験は{n}件です。同じ登録済み仮説と入力で16件以上が必要です。'})
            if claim and any(r['action']!=claim['action'] for r in rows):reasons.append({'code':'action_mismatch','detail':'実験入力が登録された入力と一致しません。異なる入力を混ぜて判定しません。'})
            if claim and any(r['id']<=claim['after_id'] for r in rows):reasons.append({'code':'before_registration','detail':'事前登録より後の実験だけを評価します。'})
            if len({r['session'] for r in rows})>1:reasons.append({'code':'mixed_sessions','detail':'異なる実機セッションの経験を混ぜて判定しません。'})
            registered=bool(not conditional and claim and claim['probability']==probability and all(r['action']==claim['action'] for r in rows))
            if registered and not reasons and n>=16 and len({r['action'] for r in rows})==1:
                z=1.96;center=(p+z*z/(2*n))/(1+z*z/n);radius=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/(1+z*z/n)
                interval=[center-radius,center+radius]
                status='supported' if interval[0]<=probability<=interval[1] else 'refuted'
            evidence=list(dict.fromkeys(json.loads(hypothesis_row['evidence'])+[r['id'] for r in rows]))
            self.store.db.execute('UPDATE knowledge SET validation=?,evidence=? WHERE id=?',(status,json.dumps(evidence),hypothesis))
            result={'job':job,'hypothesis':hypothesis,'status':status,'n':n,'observed_probability':p,'expected_probability':probability,'interval95':interval,'pre_registered':registered,'required_samples':16,'reasons':reasons,'registered_target':{'action':self.keys.index(claim['action']),'change_probability':claim['probability'],'after_id':claim['after_id']} if claim else None,'experiments':experiment_ids,'sessions':sorted({r['session'] for r in rows}),'evidence':[r['id'] for r in rows],'previous_status':hypothesis_row['validation'],'limitation':'Image change reproducibility only; game meaning/causality unverified'}
            result['evaluation_id']=self.store.db.execute('INSERT INTO research_evaluations(job,hypothesis,result,created) VALUES(?,?,?,?)',(job,hypothesis,json.dumps(result),time.time())).lastrowid
        self.activity.record('EVAL',f'job={job} evaluation={result["evaluation_id"]} hypothesis={hypothesis} {status} n={n}')
        return result
