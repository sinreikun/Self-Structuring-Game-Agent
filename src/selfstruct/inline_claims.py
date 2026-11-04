"""Inline preregistration: validate the whole submission before saving or queuing input."""
import hashlib
import json
import math
import time
from . import transitions


def setup(db):
    db.execute('''CREATE TABLE IF NOT EXISTS inline_claim_receipts(
      id TEXT PRIMARY KEY, job TEXT, token TEXT, hypothesis INTEGER,
      request TEXT, claim TEXT, plan TEXT, registered REAL, reused INTEGER)''')


def accept(scientist,jid,payload,request,token,hypothesis):
    store=scientist.store
    def reject(errors):
        return {'state':'claim_required','hypothesis':hypothesis,'fields':errors,
                'next':'Add or correct these claim fields in this experiment request and resend. No input executed.'}
    if hypothesis is None:
        return (reject([{'field':'hypothesis','reason':'claim requires a hypothesis'}]) if 'claim' in request else None)
    with store.lock:
        if not store.db.execute("SELECT 1 FROM knowledge WHERE id=? AND kind='hypothesis'",(hypothesis,)).fetchone():
            return reject([{'field':'hypothesis','reason':'Unknown hypothesis reference: '+repr(request.get('hypothesis'))}])
        existing=store.db.execute('SELECT * FROM research_claims WHERE knowledge=?',(hypothesis,)).fetchone()
        if existing and (type(existing['probability']) not in (int,float) or not math.isfinite(existing['probability']) or not 0<=existing['probability']<=1):
            return reject([{'field':'claim.probability','reason':'invalid existing registration'}])
        raw=request.get('claim')
        errors=[]
        if 'claim' not in request:
            if existing:
                if existing['action']!=scientist.keys[request['action']]:
                    return reject([{'field':'action','reason':'differs from registered claim'}])
                normalized={'type':existing['claim_type'],'probability':existing['probability']}
                if existing['claim_type']=='transition':normalized.update({k:existing[k] for k in ('from_state','to_state')})
            else:return reject([{'field':'claim.type','reason':'missing'},{'field':'claim.probability','reason':'missing'}])
        else:
            if not isinstance(raw,dict):return reject([{'field':'claim','reason':'expected object'}])
            kind=raw.get('type')
            if kind not in ('transition','change_rate'):errors.append({'field':'claim.type','reason':'required transition or change_rate'})
            probability=raw.get('probability')
            if type(probability) not in (int,float) or not math.isfinite(probability) or not 0<=probability<=1:
                errors.append({'field':'claim.probability','reason':'required finite number in [0,1]'})
            allowed={'type','probability'}|({'from_state','to_state'} if kind=='transition' else set())
            errors.extend({'field':'claim.'+k,'reason':'inconsistent or unknown field'} for k in raw.keys()-allowed)
            defined={r['state'] for r in transitions.representatives(store)}
            normalized={'type':kind,'probability':probability}
            for field in ('from_state','to_state') if kind=='transition' else ():
                value=raw.get(field)
                try:
                    if isinstance(value,dict):
                        if set(value)!={'text','evidence','phase'}:raise ValueError('requires text, evidence, phase only')
                        if not isinstance(value['text'],str) or not value['text'].strip() or len(value['text'])>1000:raise ValueError('requires nonempty model text up to 1000 characters')
                        ids=value['evidence']
                        if not isinstance(ids,list) or not 1<=len(ids)<=16 or any(type(i)!=int or i<1 for i in ids):raise ValueError('requires 1..16 Experience IDs')
                        if value['phase'] not in ('before','after'):raise ValueError('requires before or after phase')
                        from .research_actions import allowed_evidence
                        if not set(ids)<=set(allowed_evidence(payload)):raise ValueError('evidence must be available in this Job')
                        for eid in ids:store.observed_image(eid,value['phase'])
                    elif type(value)!=int or value not in defined:raise ValueError('Unknown state reference: '+repr(value)+'; requires existing state ID or inline {text,evidence,phase}')
                    normalized[field]=value
                except (ValueError,OSError,KeyError,TypeError) as error:errors.append({'field':'claim.'+field,'reason':str(error)})
            if errors:return reject(errors)
            # Resolve identical definitions without creating duplicate states on resubmission.
            for field in ('from_state','to_state') if kind=='transition' else ():
                value=normalized[field]
                if isinstance(value,dict):
                    match=None
                    for row in store.db.execute("SELECT id,text FROM knowledge WHERE kind='state'"):
                        refs={(r['event'],r['phase']) for r in store.db.execute('SELECT event,phase FROM state_frames WHERE state=?',(row['id'],))}
                        if row['text']==value['text'] and refs=={(i,value['phase']) for i in value['evidence']}:match=row['id'];break
                    normalized[field]=match if match is not None else value
            if existing:
                expected={'type':existing['claim_type'],'probability':existing['probability']}
                if existing['claim_type']=='transition':expected.update({k:existing[k] for k in ('from_state','to_state')})
                if normalized!=expected or existing['action']!=scientist.keys[request['action']]:
                    return reject([{'field':'claim','reason':'differs from immutable registered claim; use a new hypothesis'}])
            else:
                for field in ('from_state','to_state') if kind=='transition' else ():
                    value=normalized[field]
                    if isinstance(value,dict):normalized[field]=transitions.register_state(store,value['text'],value['evidence'],value['phase'])
                if kind=='transition':transitions.register_claim(store,hypothesis,scientist.keys[request['action']],normalized)
                else:
                    latest=store.db.execute('SELECT coalesce(max(id),0) FROM events').fetchone()[0]
                    store.claim(hypothesis,scientist.keys[request['action']],probability,latest)
        identifier=hashlib.sha256((jid+':'+token).encode()).hexdigest()[:32]
        # Persist original payload, normalized registration and future plan ID before submit.
        with store.db:
            setup(store.db)
            store.db.execute('INSERT OR IGNORE INTO inline_claim_receipts VALUES(?,?,?,?,?,?,?,?,?)',
                (identifier,jid,token,hypothesis,json.dumps(request),json.dumps(normalized),identifier,time.time(),int(existing is not None)))
        receipt={'receipt':identifier,'hypothesis':hypothesis,'claim':normalized,'plan':identifier,'reused':existing is not None}
        payload.setdefault('inline_claim_receipts',[]).append(receipt)
        store.set_job(jid,'running',payload)
        return receipt
