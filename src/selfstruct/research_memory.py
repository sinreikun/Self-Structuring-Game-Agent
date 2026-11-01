"""Pull-based research records. Primary stores stay authoritative; reads use revisions."""
import hashlib
import json
import re
import time

COLLECTIONS=('experience','knowledge','experiment','visual','research','note','report')
TABLES={'experience':'events','knowledge':'knowledge','experiment':'research_experiments','research':'jobs','note':'research_notes','report':'research_reports'}


def setup(db):
    db.execute('CREATE TABLE IF NOT EXISTS research_memory_snapshots(job TEXT, collection TEXT, ref TEXT, revision TEXT, content TEXT, created REAL, PRIMARY KEY(job,collection,ref,revision))')
    db.execute('CREATE TABLE IF NOT EXISTS research_reports(id INTEGER PRIMARY KEY, job TEXT, title TEXT, target_goal TEXT, goal_status TEXT, goal_evidence TEXT, findings TEXT, unknowns TEXT, next_steps TEXT, updated REAL)')
    db.execute('CREATE TABLE IF NOT EXISTS research_notes(id INTEGER PRIMARY KEY, job TEXT, title TEXT, body TEXT, evidence TEXT, updated REAL)')


def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def revision(value):return hashlib.sha256(encoded(value).encode()).hexdigest()


def frame_sizes(store,row):
    """Read only registered PNG headers; no pixel decoding or attention choice."""
    from PIL import Image
    sizes={}
    for phase in ('before','after'):
        digest=row.get(phase+'_hash');size=None
        if isinstance(digest,str) and re.fullmatch('[a-f0-9]{64}',digest):
            try:
                with Image.open(store.root/'evidence'/(digest+'.png')) as image:size=list(image.size)
            except (OSError,ValueError):pass
        sizes[phase+'_size']=size
    return sizes


def document(scientist,collection,ref):
    store=scientist.store
    if collection=='visual':
        job,index=ref.rsplit(':',1)
        row=store.db.execute('SELECT payload FROM jobs WHERE id=?',(job,)).fetchone()
        history=json.loads(row['payload']).get('visual_history',[]) if row else []
        index=int(index)
        if not 0<=index<len(history):return None
        return {'job':job,'index':index,**{k:v for k,v in history[index].items() if k in ('request','result','kind','step')}}
    if collection not in TABLES or not ref.isdecimal():return None
    if not 1<=int(ref)<=2**53-1:return None
    row=store.db.execute('SELECT * FROM '+TABLES[collection]+' WHERE rowid=?',(int(ref),)).fetchone()
    if not row:return None
    row=dict(row)
    if collection=='experience':
        return {**{k:v for k,v in row.items() if k in ('id','action','duration','state','feature','change','prediction','sent','observed','before_hash','after_hash')},'visual_metadata':frame_sizes(store,row)}
    if collection=='knowledge':
        row['evidence']=json.loads(row['evidence'])
        claim=store.db.execute('SELECT action,probability,after_id FROM claims WHERE knowledge=?',(row['id'],)).fetchone()
        if claim:row['preregistered']=dict(claim)
        return row
    if collection=='experiment':
        for key in ('request','events','result'):row[key]=json.loads(row[key])
        return row
    if collection=='research':
        if row['kind'] not in ('research','protocol_probe'):return None
        p=json.loads(row['payload'])
        if isinstance(p.get('progress'),dict):p['progress']={k:v for k,v in p['progress'].items() if k not in ('memory_reads','memory_searches')}
        return {'job':row['id'],'state':row['state'],**{k:v for k,v in p.items() if k in ('outcome','progress','hypotheses','experiment_context','research_limit')}}
    if collection=='report':
        for key in ('goal_evidence','findings','unknowns','next_steps'):row[key]=json.loads(row[key])
        row['attribution']='LLM research report; not an independent verification of game meaning or success'
        return row
    row['evidence']=json.loads(row['evidence']);return row


def metadata(collection,ref,doc):
    title=doc.get('title') or doc.get('text') or doc.get('action') or doc.get('job') or doc.get('id') or ref
    return {'collection':collection,'ref':ref,'revision':revision(doc),'title':str(title)[:72],
            **({'state':doc['state']} if 'state' in doc else {}),
            **({'validation':doc['validation']} if 'validation' in doc else {})}


def snapshot(scientist,jid,collection,ref,doc):
    """Bounded immutable lease, outside the model context; survives Job restart."""
    store=scientist.store;meta=metadata(collection,ref,doc);text=encoded(doc)
    if len(text.encode())>65536:return {**meta,'snapshot_retained':False}
    with store.lock,store.db:
        if store.db.execute('SELECT 1 FROM research_memory_snapshots WHERE job=? AND collection=? AND ref=? AND revision=?',(jid,collection,ref,meta['revision'])).fetchone():return {**meta,'snapshot_retained':True}
        from .store import CapacityError
        try:store.reserve(len(text.encode())+1024,critical=False)
        except CapacityError:return {**meta,'snapshot_retained':False}
        store.db.execute("DELETE FROM research_memory_snapshots WHERE job IN (SELECT id FROM jobs WHERE state='complete') AND job<>?",(jid,))
        store.db.execute('INSERT INTO research_memory_snapshots VALUES(?,?,?,?,?,?)',(jid,collection,ref,meta['revision'],text,time.time()))
        store.db.execute('DELETE FROM research_memory_snapshots WHERE job=? AND rowid NOT IN (SELECT rowid FROM research_memory_snapshots WHERE job=? ORDER BY created DESC LIMIT 32)',(jid,jid))
    return {**meta,'snapshot_retained':True}


def grant(scientist,jid,payload,ids):
    ids=[i for i in ids if type(i)==int][:64]
    if not ids:return
    valid=[r[0] for r in scientist.store.db.execute("SELECT id FROM events WHERE state='complete' AND id IN ("+','.join('?' for _ in ids)+')',ids)]
    payload['memory_evidence']=sorted(set(payload.get('memory_evidence',[])+valid))[-512:]
    scientist.store.pin(valid,'research-job-'+jid)


def search(scientist,jid,payload,request):
    collection=request['collection'];store=scientist.store
    cursor=request.get('cursor',2**53-1);watermark=request.get('watermark',2**53-1);limit=request.get('limit',4)
    query=request.get('query','').casefold();found=[]
    with store.lock:
        ref=request.get('ref')
        numeric_query=not ref and collection=='experience' and query.strip().isascii() and query.strip().isdecimal()
        if numeric_query:ref=str(int(query.strip()))
        if ref:
            if numeric_query and not (int(ref)<cursor and int(ref)<=watermark):
                return {'collection':collection,'records':[],'next_cursor':None,'detail':'Requested ID is outside this cursor/watermark. Use ref for an exact lookup; this does not mean the record is absent.'}
            try:doc=document(scientist,collection,ref)
            except (ValueError,TypeError):doc=None
            if doc and collection=='experience':grant(scientist,jid,payload,[doc['id']])
            return {'collection':collection,'records':[snapshot(scientist,jid,collection,ref,doc)] if doc else [],'next_cursor':None}
        if collection=='visual':
            history=payload.get('visual_history',[]);watermark=min(watermark,len(history))
            candidates=[(i,jid+':'+str(i)) for i in range(min(len(history),cursor,watermark)-1,-1,-1)]
        else:
            table=TABLES[collection]
            watermark=min(watermark,store.db.execute('SELECT coalesce(max(rowid),0) FROM '+table).fetchone()[0])
            # Literal substring search, bound parameters. No model SQL or filenames.
            field={'knowledge':'text','experience':'action','experiment':'id || job','research':'id','note':'title || body','report':'title || target_goal'}[collection]
            pattern='%'+query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
            rows=store.db.execute('SELECT rowid FROM '+table+' WHERE rowid<? AND rowid<=? AND '+field+" LIKE ? ESCAPE '\\' "+("AND kind IN ('research','protocol_probe') " if collection=='research' else '')+'ORDER BY rowid DESC LIMIT ?', (cursor,watermark,pattern,limit+1)).fetchall()
            candidates=[(r[0],str(r[0])) for r in rows]
        last=None
        for index,ref in candidates:
            doc=document(scientist,collection,ref)
            if doc is None:continue
            if collection=='visual' and query not in encoded(doc).casefold():continue
            if len(found)==limit:break
            found.append(snapshot(scientist,jid,collection,ref,doc));last=index
            if collection=='experience':grant(scientist,jid,payload,[doc['id']])
        more=len(candidates)>len(found)
    return {'collection':collection,'records':found,'watermark':watermark,'next_cursor':last if more and last is not None else None,
            'consistency':'watermark fixes insertion upper bound; each record revision must be checked on read'}


def read(scientist,jid,payload,request):
    collection=request['collection'];ref=request['ref'];store=scientist.store
    with store.lock:
        try:doc=document(scientist,collection,ref)
        except (ValueError,TypeError):doc=None
        current_revision=revision(doc) if doc is not None else None
        retained=store.db.execute('SELECT content FROM research_memory_snapshots WHERE job=? AND collection=? AND ref=? AND revision=?',(jid,collection,ref,request['revision'])).fetchone()
        if retained:doc=json.loads(retained['content'])
        elif doc is None:return {'status':'unavailable','collection':collection,'ref':ref,'detail':'Record and snapshot unavailable; search current index. Never infer missing content.'}
        meta=metadata(collection,ref,doc)
        if request['revision']!=meta['revision']:return {'status':'stale',**snapshot(scientist,jid,collection,ref,doc),'detail':'Snapshot expired or unavailable. Search and read the new revision from offset 0.'}
        snapshot(scientist,jid,collection,ref,doc)
        text=encoded(doc);offset=request.get('offset',0);limit=request.get('limit',1000)
        if offset>len(text):raise ValueError('Memory offset exceeds document length')
        if collection=='experience':grant(scientist,jid,payload,[doc['id']])
        elif collection=='knowledge' or collection=='note':grant(scientist,jid,payload,doc.get('evidence',[]))
        elif collection=='experiment':grant(scientist,jid,payload,doc.get('events',[]))
        elif collection=='report':grant(scientist,jid,payload,doc.get('goal_evidence',[])+[i for f in doc.get('findings',[]) for i in f['evidence']])
        elif collection=='visual':
            from .vision import evidence_ids
            grant(scientist,jid,payload,sorted(evidence_ids(doc.get('result',{}).get('evidence',{}))))
        end=min(len(text),offset+limit)
        return {'status':'ok',**meta,'source_revision':current_revision,'source_changed':current_revision!=meta['revision'],'format':'JSON text, paged by Unicode character offset','offset':offset,'total':len(text),'next_offset':end if end<len(text) else None,'content':text[offset:end]}


def note(scientist,jid,payload,request):
    from .research_actions import allowed_evidence
    store=scientist.store;ids=sorted(set(request.get('evidence',[])))
    if any(i not in allowed_evidence(payload) for i in ids):raise ValueError('Note evidence must have been retrieved or observed')
    with store.lock,store.db:
        ref=request.get('ref');old=document(scientist,'note',ref) if ref else None
        if ref and old is None:return {'status':'unavailable','collection':'note','ref':ref}
        if old and request.get('revision')!=revision(old):return {'status':'stale',**metadata('note',ref,old)}
        if not ref:
            prior=store.db.execute('SELECT id FROM research_notes WHERE job=? AND title=?',(jid,request['title'])).fetchone()
            if prior:
                ref=str(prior[0]);return {'status':'exists',**metadata('note',ref,document(scientist,'note',ref))}
        store.reserve(8192)
        if ref:
            store.db.execute('UPDATE research_notes SET title=?,body=?,evidence=?,updated=? WHERE id=?',(request['title'],request['body'],json.dumps(ids),time.time(),int(ref)))
            store.unpin('research-note-'+ref)
        else:ref=str(store.db.execute('INSERT INTO research_notes(job,title,body,evidence,updated) VALUES(?,?,?,?,?)',(jid,request['title'],request['body'],json.dumps(ids),time.time())).lastrowid)
        store.pin(ids,'research-note-'+ref)
        return {'status':'saved',**metadata('note',ref,document(scientist,'note',ref)),'validation':'unverified research note; not a supported hypothesis'}


def overview(scientist,jid,payload):
    with scientist.store.lock:
        counts={name:scientist.store.db.execute('SELECT count(*) FROM '+table).fetchone()[0] for name,table in TABLES.items()}
        counts['visual']=len(payload.get('visual_history',[]))
        goal=scientist.store.db.execute('SELECT id,target_goal,goal_status FROM research_reports ORDER BY id DESC LIMIT 1').fetchone()
        summary=payload.get('experiment_context',{})
        goal_view={'text':goal['target_goal'][:160],'text_truncated':len(goal['target_goal'])>160,'status':goal['goal_status'],'source':metadata('report',str(goal['id']),document(scientist,'report',str(goal['id'])))} if goal else {'text':'未確認','status':'unknown'}
        active=[metadata('knowledge',str(h['id']),document(scientist,'knowledge',str(h['id']))) for h in payload.get('hypothesis_context',[])]
    from .vision import MAX_PIXELS,MAX_VALUES
    from .research_actions import MAX_CALLS,MAX_ACTIONS
    return {'job':jid,'collections':counts,'seed_visual_metadata':[{'id':r['id'],**frame_sizes(scientist.store,r)} for r in payload.get('research_rows',[])[-3:]],
            'visual_limits':{'max_pixels':MAX_PIXELS,'max_values_per_request':MAX_VALUES},
            'research_budget':{'calls_including_this_turn':max(0,MAX_CALLS-payload.get('research_calls',0)),'actions_remaining':max(0,MAX_ACTIONS-len(payload.get('action_history',[])))},'seed_experience_ids':[r['id'] for r in payload.get('research_rows',[])[-3:]],
            'active_hypotheses':active,
            'current_target_goal':goal_view,
            'experiment_budget':{k:summary[k] for k in ('plans_remaining','inputs_remaining') if k in summary},
            'rule':'Index only. Retrieve exact records with memory_search/read. No initial pixels were inspected automatically. Use observe/compare for actual visual evidence.'}


def report(scientist,jid,payload,request):
    from .research_actions import allowed_evidence
    ids=sorted(set(request['goal_evidence']+[i for f in request['findings'] for i in f['evidence']]))
    if any(i not in allowed_evidence(payload) for i in ids):raise ValueError('Report evidence must have been retrieved or observed')
    if request['goal_status']!='unknown' and not request['goal_evidence']:raise ValueError('An inferred or observed target goal requires evidence')
    if not request['findings'] and not request['unknowns']:raise ValueError('A report needs findings or explicit unknowns')
    store=scientist.store
    with store.lock,store.db:
        store.reserve(16384)
        ref=str(store.db.execute('INSERT INTO research_reports(job,title,target_goal,goal_status,goal_evidence,findings,unknowns,next_steps,updated) VALUES(?,?,?,?,?,?,?,?,?)',
            (jid,request['title'],request['target_goal'],request['goal_status'],json.dumps(request['goal_evidence']),encoded(request['findings']),encoded(request['unknowns']),encoded(request['next_steps']),time.time())).lastrowid)
        store.pin(ids,'research-report-'+ref)
        return {'status':'saved',**metadata('report',ref,document(scientist,'report',ref)),
                'validation':'LLM report; existing statistical verdicts and game success unchanged'}


def render_report(doc):
    lines=['# '+doc['title'],'LLMの研究結果（記述内容の独立認証ではありません）',
           '研究対象の目的：'+(doc['target_goal'] or '未確認')+' ['+doc['goal_status']+']',
           '目的の根拠：'+str(doc['goal_evidence']),'\n分かったこと・仮説']
    for item in doc['findings']:lines.append('- ['+item['status']+'] '+item['text']+' ／ 根拠 '+str(item['evidence']))
    lines+=['\n未確認事項',*('- '+x for x in doc['unknowns']),'\n次の方針',*('- '+x for x in doc['next_steps'])]
    return '\n'.join(lines)
