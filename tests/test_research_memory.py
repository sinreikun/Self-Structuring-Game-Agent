"""Pull memory uses real persisted records, revisions and actual visual/input paths."""
import json
import re
import threading
import time

import pytest
from PIL import Image
from selfstruct.config import Config
from selfstruct.engine import Controller
from selfstruct.smoke import SyntheticGame
from selfstruct.research_memory import search,read,note


class Dynamics(SyntheticGame):
    def capture(self):return Image.new('RGB',(160,96),(231,39,72) if self.position%2 else (7,181,213))
    def act(self,key,duration,cancel,on_sent=None):
        if cancel.is_set():return
        if on_sent:on_sent(time.time())
        self.position+=1;self.held=False


@pytest.fixture
def world(tmp_path):
    c=Controller(Config(root=str(tmp_path),keys=['A','B'],research_every=100000),Dynamics());c.step()
    job=c.store.job('research',{});p={'research_rows':c.store.rows()}
    yield c,job,p
    c.close()


def test_revision_pages_stay_consistent_and_expose_changed_primary_source(world):
    c,job,p=world
    kid=c.store.knowledge('hypothesis','版をまたいで結合しない本文'*50,'inferred','hypothesis',[c.last_eid])
    item=search(c.scientist,job,p,{'collection':'knowledge','ref':str(kid)})['records'][0]
    req={'collection':'knowledge','ref':item['ref'],'revision':item['revision'],'limit':80}
    page=read(c.scientist,job,p,req);assert page['status']=='ok' and page['next_offset']==80
    with c.store.lock,c.store.db:c.store.db.execute("UPDATE knowledge SET validation='inconclusive' WHERE id=?",(kid,))
    changed=read(c.scientist,job,p,{**req,'offset':80})
    assert changed['status']=='ok' and changed['source_changed'] and changed['revision']==item['revision']
    assert changed['source_revision']!=item['revision']
    current=search(c.scientist,job,p,{'collection':'knowledge','ref':str(kid)})['records'][0]
    fresh=read(c.scientist,job,p,{**req,'revision':current['revision'],'offset':0,'limit':1600})
    assert json.loads(fresh['content'])['validation']=='inconclusive'
    assert search(c.scientist,job,p,{'collection':'knowledge','query':"%' OR 1=1 --"})['records']==[]
    assert c.last_eid in p['memory_evidence']


def test_notes_use_compare_and_swap_and_are_available_to_a_later_job(world):
    c,job,p=world
    req={'title':'次に確認する不明点','body':'画像の左半分をまだ見ていない。次は比較する。','evidence':[c.last_eid]}
    saved=note(c.scientist,job,p,req);assert saved['status']=='saved'
    updated=note(c.scientist,job,p,{**req,'ref':saved['ref'],'revision':saved['revision'],'body':'比較を要求した。意味は未確認。'})
    assert updated['status']=='saved' and updated['revision']!=saved['revision']
    stale=note(c.scientist,job,p,{**req,'ref':saved['ref'],'revision':saved['revision'],'body':'古い版で上書きしてはいけない'})
    assert stale['status']=='stale'
    another=c.store.job('research',{})
    found=search(c.scientist,another,{}, {'collection':'note','query':'不明点'})['records'][0]
    content=read(c.scientist,another,{}, {**{k:found[k] for k in ('collection','ref','revision')},'limit':1600})
    assert json.loads(content['content'])['body']=='比較を要求した。意味は未確認。'
    c.store.knowledge('hypothesis','ノートとは別の仮説','inferred','hypothesis',[c.last_eid])
    assert c.store.db.execute('SELECT count(*) FROM claims').fetchone()[0]==0
    assert c.store.db.execute('SELECT count(*) FROM evidence_pins WHERE reason=?',('research-note-'+saved['ref'],)).fetchone()[0]==1


def test_search_watermark_does_not_shift_pages_and_research_read_excludes_prompts(world):
    c,job,p=world
    for i in range(5):c.store.knowledge('hypothesis',f'保存記録{i}','inferred','hypothesis',[c.last_eid])
    first=search(c.scientist,job,p,{'collection':'knowledge','limit':2})
    newest=c.store.knowledge('hypothesis','後から追加','inferred','hypothesis',[c.last_eid])
    second=search(c.scientist,job,p,{'collection':'knowledge','limit':2,'watermark':first['watermark'],'cursor':first['next_cursor']})
    assert not {r['ref'] for r in first['records']} & {r['ref'] for r in second['records']}
    assert str(newest) not in {r['ref'] for r in second['records']}
    c.store.set_job(job,'running',{'prompt':'DO_NOT_EXPORT_PROMPT','source':'DO_NOT_EXPORT_SOURCE','progress':{'experiments':0}})
    found=search(c.scientist,job,p,{'collection':'research','limit':1})['records'][0]
    result=read(c.scientist,job,p,{k:found[k] for k in ('collection','ref','revision')})
    assert 'DO_NOT_EXPORT' not in result['content']


@pytest.mark.parametrize('vision',[False,True])
def test_model_pulls_memory_then_eyes_experiment_evaluation_and_persistent_note(world,vision):
    c,job,p=world;c.step();older=p['research_rows'][0]['id'];eid=c.last_eid
    c.store.knowledge('observation','既存の測定記録：画面構造の意味は不明','observed','unknown',[older])
    class Client:
        calls=0
        def complete(self,prompt,**options):
            self.calls+=1;n=self.calls
            if n==1:
                assert 'memory_search' in prompt and '既存の測定記録' not in prompt
                assert '"pixels":[' not in prompt and 'images' not in options
                return json.dumps({'actions':[{'op':'memory_search','collection':'knowledge','query':'既存の測定記録'}]})
            if n==2:
                match=re.search(r'"ref":"(\d+)","revision":"([a-f0-9]{64})"',prompt);assert match
                return json.dumps({'actions':[{'op':'memory_read','collection':'knowledge','ref':match[1],'revision':match[2]}]})
            if n==3:
                assert '既存の測定記録' in prompt and 'content_sha256' in prompt
                return json.dumps({'actions':[{'op':'observe','event':eid,'phase':'before','box':[80,0,160,48],'size':[64,32],'mode':'RGB','output':'image' if vision else 'pixels','limit':64}]})
            if n==4:
                assert options.get('images') if vision else '"pixels":[' in prompt
                return json.dumps({'actions':[{'op':'compare','event':eid,'phase':'before','compare':{'event':eid,'phase':'after'},'size':[32,32],'output':'statistics'}]})
            if n==5:
                assert 'similarity' in prompt
                return json.dumps({'findings':[{'id':'r5_1','text':'同じ入力後の画像差分の再現性のみを検証する','evidence':[eid,older],'action':1,'change_probability':1}],
                    'actions':[{'op':'experiment','hypothesis':'r5_1','action':1,'duration':10,'wait':10,'repeat':8} for _ in range(2)]})
            if n==6:
                assert 'event_ids' in prompt and '"completed":8' in prompt
                return json.dumps({'actions':[{'op':'evaluate','hypothesis':'r5_1','change_probability':1}]})
            if n==7:
                assert '"status":"supported"' in prompt
                return json.dumps({'actions':[{'op':'memory_note','title':'研究を続けるための未確認事項','body':'画像変化の再現性は評価済み。ゲーム内の意味と有効な能力は未確認。','evidence':[eid,older]}]})
            if n==8:
                assert '"status":"saved"' in prompt
                return json.dumps({'actions':[{'op':'research_report','title':'対象の目的を理解するための途中結果','target_goal':'未確認','goal_status':'unknown','goal_evidence':[],
                    'findings':[{'text':'同じ入力に画像差分が再現する統計結果を得た。対象の目的を達成した証拠ではない。','status':'observation','evidence':[eid,older]}],
                    'unknowns':['対象で達成すべき目的と画像の意味は未確認'],'next_steps':['目的に関する表示を自分で観測する']}]})
            assert n==9 and '"status":"saved"' in prompt
            return '{"findings":[],"actions":[],"wat":""}'
    client=Client();client.vision=vision;c.scientist.client=client;errors=[]
    def run():
        try:c.scientist.study(c.store.rows(),job)
        except BaseException as error:errors.append(error)
    worker=threading.Thread(target=run);worker.start();deadline=time.monotonic()+15
    try:
        while worker.is_alive() and time.monotonic()<deadline:c.step();time.sleep(.005)
        worker.join(1);assert not worker.is_alive() and not errors,errors
        final=json.loads(c.store.get_job(job)['payload'])
        assert client.calls==9 and final['memory_mode']=='pull' and not final.get('initial_visual_evidence')
        assert final['progress']['memory_reads']==1 and final['progress']['memory_searches']==1
        assert final['progress']['experiments']==2 and final['progress']['hypotheses_evaluated']==1
        assert final['progress']['reports']==1 and '目的' in c.store.note_rows('研究結果',1)[0]['text']
        assert len(final['memory_results'])==4 and c.store.db.execute('SELECT count(*) FROM research_notes').fetchone()[0]==1
        from selfstruct.diagnostics import diagnostic_job_payload
        audit=json.loads(diagnostic_job_payload(json.dumps(final)))
        retrieved=next(a for a in audit['action_history'] if a['request']['op']=='memory_read')
        assert retrieved['result']['content_sha256'] and retrieved['result']['revision'] and 'content' not in retrieved['result']
    finally:c.scientist.stop_event.set();worker.join(2)


def test_retrieval_checks_live_revision_while_preserving_selected_snapshot(world):
    from selfstruct.research_actions import run_actions
    c,job,p=world;kid=c.store.knowledge('hypothesis','同期確認','inferred','hypothesis',[c.last_eid])
    item=search(c.scientist,job,p,{'collection':'knowledge','ref':str(kid)})['records'][0]
    req={'op':'memory_read',**{k:item[k] for k in ('collection','ref','revision')}}
    p['research_calls']=1;p['research_started']=time.time();run_actions(c.scientist,job,p,[req])
    with c.store.lock,c.store.db:c.store.db.execute("UPDATE knowledge SET text='更新された主張' WHERE id=?",(kid,))
    p['research_calls']=2;run_actions(c.scientist,job,p,[req])
    assert [a['state'] for a in p['action_history']]==['done','done']
    assert [a['result']['status'] for a in p['action_history']]==['ok','ok']
    assert p['action_history'][1]['result']['source_changed']
    assert p['action_history'][1]['result']['content']==p['action_history'][0]['result']['content']
    assert p['action_history'][1]['result']['source_revision']!=p['action_history'][1]['result']['revision']


def test_mission_and_human_report_preserve_uncertainty_without_promoting_claims(world,tmp_path):
    from selfstruct.research import research_prompt
    from selfstruct.research_mission import MISSION,identity
    from selfstruct.research_actions import execute
    from selfstruct.diagnostics import create_diagnostics
    import zipfile
    c,job,p=world
    prompt=research_prompt('',c.store.rows(),['A','B'],[],pull=True)
    assert MISSION in prompt and prompt.count(MISSION)==1
    assert '人間が画面を見て入力し' in prompt and 'Hostが与える既知の前提' in prompt
    assert '画像解析や道具の作成は、そのための手段' in prompt
    assert 'ゲーム・対話型プログラム' in identity()['environment']
    assert '主目的' in prompt and '副目的' in prompt and '対象の目的ではありません' in prompt
    request={'op':'research_report','title':'対象の目的はまだ不明','target_goal':'画面内の目的表示を確認する必要がある','goal_status':'unknown','goal_evidence':[],
        'findings':[{'text':'許可入力の完了経験が保存されている','status':'observation','evidence':[c.last_eid]}],
        'unknowns':['何を達成すべき対象かは未確認'],'next_steps':['目的を示す表示の有無を自分で観測する']}
    result,_,_=execute(c.scientist,job,p,request,'1:0')
    assert result['status']=='saved'
    human=c.store.note_rows('研究結果',1)[0]['text']
    assert '目的' in human and '[unknown]' in human and '未確認事項' in human and '根拠' in human
    assert c.store.db.execute('SELECT count(*) FROM claims').fetchone()[0]==0
    assert c.store.db.execute("SELECT count(*) FROM knowledge WHERE validation='supported'").fetchone()[0]==0
    bad={**request,'goal_status':'hypothesis'}
    with pytest.raises(ValueError,match='requires evidence'):execute(c.scientist,job,p,bad,'1:1')
    before=c.store.db.execute('SELECT count(*) FROM research_reports').fetchone()[0]
    with pytest.raises(ValueError,match='retrieved or observed'):
        execute(c.scientist,job,p,{**request,'goal_evidence':[999999]},'1:2')
    assert c.store.db.execute('SELECT count(*) FROM research_reports').fetchone()[0]==before
    path=create_diagnostics(c.store.root,c.store,destination=tmp_path/'report.zip')
    with zipfile.ZipFile(path) as z:
        assert '研究結果.md' in z.namelist() and '未確認事項' in z.read('研究結果.md').decode()
        assert json.loads(z.read('experiment/research-mission.json'))==identity()


def test_external_notes_survive_reopening_the_individual(world):
    from selfstruct.store import Store
    from types import SimpleNamespace
    c,job,p=world
    saved=note(c.scientist,job,p,{'title':'再開後の課題','body':'未確認の領域を次に調べる','evidence':[c.last_eid]})
    root=c.store.root;quota=c.store.quota;c.close()
    reopened=Store(root,quota)
    try:
        scientist=SimpleNamespace(store=reopened)
        entries=search(scientist,'next-job',{}, {'collection':'note','query':'再開後'})['records']
        assert entries[0]['ref']==saved['ref'] and entries[0]['revision']==saved['revision']
        doc=read(scientist,'next-job',{}, {k:entries[0][k] for k in ('collection','ref','revision')})
        assert '未確認の領域' in doc['content']
    finally:reopened.close()


def test_batched_reads_report_undelivered_pages_without_destroying_records(world,monkeypatch):
    from selfstruct.research import LocalLLM
    from selfstruct.research_actions import run_actions
    c,job,p=world;p.update(memory_mode='pull',research_calls=1,research_started=time.time())
    requests=[]
    for i in range(4):
        kid=c.store.knowledge('hypothesis',f'本文{i} '+('未確認の記録。'*350),'inferred','hypothesis',[c.last_eid])
        meta=search(c.scientist,job,p,{'collection':'knowledge','ref':str(kid)})['records'][0]
        requests.append({'op':'memory_read',**{k:meta[k] for k in ('collection','ref','revision')},'limit':1600})
    run_actions(c.scientist,job,p,requests);c.store.set_job(job,'running',p)
    original=json.dumps(p['memory_results'],sort_keys=True)
    client=LocalLLM('http://127.0.0.1:8766/v1','fixture');client.context=8192
    monkeypatch.setattr(client,'health',lambda:None);monkeypatch.setattr(client,'count_tokens',lambda text:len(text)//2)
    calls=[]
    def complete(prompt,**options):
        calls.append(prompt)
        omitted=json.loads(prompt.split('省略情報：')[-1])['omitted_memory']
        assert omitted and all(item['request']['ref'] in {r['ref'] for r in requests} for item in omitted)
        assert options['max_tokens']>=512 and 'content_sha256' in prompt
        return '{"findings":[],"actions":[],"wat":""}'
    monkeypatch.setattr(client,'complete',complete);c.scientist.client=client
    c.scientist.study(c.store.rows(),job);final=json.loads(c.store.get_job(job)['payload'])
    assert len(calls)==1 and json.dumps(final['memory_results'],sort_keys=True)==original
    delivered=final['research_steps'][-1]['delivered_memory_actions']
    assert 0<len(delivered)<4 and len(final['action_history'])==4
