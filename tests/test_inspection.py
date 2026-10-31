from dataclasses import replace
import json
import sqlite3
import zipfile

from PIL import Image
import pytest

from selfstruct.config import Config
from selfstruct.engine import Controller
from selfstruct.individuals import Individuals
from selfstruct.inspection import IndividualView
from selfstruct.smoke import SyntheticGame


def stored_individual(tmp_path):
    c=Controller(Config(root=str(tmp_path/'data'),keys=['A'],research_every=10000),SyntheticGame())
    c.step();event=c.store.rows(1)[0]['id']
    c.store.knowledge('hypothesis','保存済みの個体Aの仮説','inferred','hypothesis',[event])
    c.dispatch({'op':'describe','nodes':[{'id':'saved-node','description':'保存した能力','role':'観測','input':'画素','output':'特徴','uses':1,'active':True}],'edges':[]})
    c.visual.analyzers.create({'description':'保存した解析器','query':{'output':'statistics'}})
    job=c.store.job('research',{'private':'preserved'})
    c.store.set_job(job,'running')
    cfg=c.config;c.close()
    return cfg,job,event


def test_idle_records_graph_and_diagnostic_do_not_resume_or_mutate_individual(tmp_path):
    from selfstruct.diagnostics import create_diagnostics
    cfg,job,event=stored_individual(tmp_path)
    path=Individuals(cfg.root).path(cfg.agent_id)
    with sqlite3.connect(path/'host'/'experience.sqlite3') as db:
        pending=db.execute('SELECT * FROM events WHERE id=?',(event,)).fetchone()
        db.execute("UPDATE events SET state='pending' WHERE id=?",(event,))
    with IndividualView(cfg) as view:
        assert view.controller is None and '(module' in view.source()
        assert view.store.note_rows('仮説')[0]['text']=='保存済みの個体Aの仮説'
        assert {n['description'] for n in view.graph()['nodes']}=={'保存した能力','保存した解析器'}
        assert view.activity()['snapshot_kind']=='saved'
        with pytest.raises(sqlite3.OperationalError):view.store.db.execute('DELETE FROM jobs')
        archive=create_diagnostics(cfg.root,store=view.store,config=cfg,activity=view.activity(),destination=tmp_path/'idle.zip')
        assert view.store.db.total_changes==0
    with sqlite3.connect(path/'host'/'experience.sqlite3') as db:
        assert db.execute('SELECT state FROM jobs WHERE id=?',(job,)).fetchone()[0]=='running'
        assert db.execute('SELECT state FROM events WHERE id=?',(event,)).fetchone()[0]=='pending'
    with zipfile.ZipFile(archive) as z:
        assert json.loads(z.read('diagnosis.json'))['config']['agent_id']==cfg.agent_id
        target=tmp_path/'diagnostic.sqlite3';target.write_bytes(z.read('diagnostics.sqlite3'))
        with sqlite3.connect(target) as db:assert db.execute('SELECT state FROM jobs WHERE id=?',(job,)).fetchone()[0]=='running'


def test_loaded_identifier_wins_over_pointer_and_newborn_view_creates_no_runtime(tmp_path):
    cfg,_,_=stored_individual(tmp_path)
    repo=Individuals(cfg.root);other=repo.create(cfg,name='個体B')
    assert repo.active()==other
    with IndividualView(cfg) as view:
        assert view.identifier==cfg.agent_id and view.store.note_rows('仮説')
    new_path=repo.path(other)
    before={p.relative_to(new_path) for p in new_path.rglob('*')}
    with IndividualView(replace(cfg,agent_id=other)) as view:
        assert view.store is None and view.source()=='' and view.graph()['nodes']==[]
    assert before=={p.relative_to(new_path) for p in new_path.rglob('*')}
    assert not (new_path/'host').exists() and not (new_path/'agents').exists()


@pytest.mark.parametrize('when',['before_input','during_wait','after_capture'])
def test_pause_does_not_train_on_interrupted_transition(tmp_path,monkeypatch,when):
    class Game(SyntheticGame):
        captures=0
        def capture(self):
            self.captures+=1
            frame=super().capture()
            if (when=='before_input' and self.captures==1) or (when=='after_capture' and self.captures==2):c.pause()
            return frame
    game=Game();c=Controller(Config(root=str(tmp_path),keys=['A']),game)
    if when=='during_wait':
        def pause_wait(_):c.pause();return True
        monkeypatch.setattr(c.cancel_input,'wait',pause_wait)
    try:
        assert c.step()==0
        assert c.paused.is_set() and not game.held
        assert c.snapshot().phase=='一時停止中' and c.steps==0
        assert c.store.rows()==[]
        states=[r[0] for r in c.store.db.execute('SELECT state FROM events')]
        assert states==([] if when=='before_input' else ['interrupted'])
        assert not any(e.kind=='LEARN' for e in c.snapshot().trace)
        assert game.captures==(2 if when=='after_capture' else 1)
        c.activity.update(phase='入力中')  # A late worker update must not hide Pause.
        assert c.snapshot().phase=='一時停止中'
    finally:c.close()
