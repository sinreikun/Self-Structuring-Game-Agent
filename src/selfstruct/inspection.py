"""Loaded-individual records without starting a controller, LLM, or recovery writes."""
from contextlib import AbstractContextManager
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading

from .individuals import Individuals
from .store import Store


class ReadOnlyRecords:
    usage=Store.usage
    storage_snapshot=Store.storage_snapshot
    storage_breakdown=Store.storage_breakdown
    note_rows=Store.note_rows
    research_history=Store.research_history
    knowledge_rows=Store.knowledge_rows
    reports=Store.reports
    book=Store.book
    export_research=Store.export_research

    def __init__(self,root,account_root,quota):
        self.root=Path(root);self.account_root=Path(account_root);self.quota=quota
        self.lock=threading.RLock()
        self._usage_bytes=0;self._usage_at=0.;self._tracked_db_bytes=0
        self.storage_state='停止中の保存記録';self.emergency_fraction=.05
        self.db=sqlite3.connect((self.root/'host'/'experience.sqlite3').as_uri()+'?mode=ro',uri=True,check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA query_only=ON')

    def rows(self,limit=512):
        with self.lock:
            return [dict(r) for r in reversed(self.db.execute("SELECT * FROM events WHERE state='complete' ORDER BY id DESC LIMIT ?",(limit,)).fetchall())]

    def counts(self):
        # Never reconcile lifetime counters or reset pending/running rows on read.
        with self.lock:
            result={'experience':self.db.execute("SELECT count(*) FROM events WHERE state<>'compacted'").fetchone()[0]+self.db.execute('SELECT coalesce(sum(count),0) FROM compacted').fetchone()[0],
                    'improvements':self.db.execute('SELECT count(*) FROM reports WHERE adopted=1').fetchone()[0]}
            for name,condition in {'discoveries':"kind='observation'",'hypotheses':"kind='hypothesis'",'supported':"validation IN ('supported','reproduced')",'refuted':"validation='refuted'",'skills':"kind='skill'"}.items():
                result[name]=self.db.execute('SELECT count(*) FROM knowledge WHERE '+condition).fetchone()[0]
            return result

    def close(self):self.db.close()


def analyzer_nodes(records):
    nodes=[]
    for record in records:
        definition=record['definition'];metrics=record['metrics']
        nodes.append({'id':record['id'],'description':definition['description'],
                      'role':'個体の視覚解析器（外部由来）' if definition.get('source') else '個体が定義した視覚解析器',
                      'input':'視覚Evidence','output':definition['query'].get('output',''),
                      'uses':record['uses'],'active':record['status']=='promoted',
                      'effectiveness':metrics.get('reason','未評価'),
                      'status':{'candidate':'評価待ち','promoted':'採用済み','rejected':'不採用','retired':'廃止'}.get(record['status'],record['status'])})
    return nodes


class IndividualView(AbstractContextManager):
    def __init__(self,config,controller=None):
        self.repo=Individuals(config.root)
        self.identifier=config.agent_id or self.repo.active()
        self.root=self.repo.path(self.identifier) if self.identifier else None
        self.controller=controller if controller and not controller.closed and controller.config.agent_id==self.identifier else None
        self.store=self.controller.store if self.controller else None
        self.owned=False
        if self.store is None and self.root and (self.root/'host'/'experience.sqlite3').is_file():
            self.store=ReadOnlyRecords(self.root,self.repo.root,config.quota_bytes);self.owned=True

    def __exit__(self,*args):
        if self.owned:self.store.close()

    @property
    def revision(self):
        if self.controller:return self.controller.agent_id
        path=self.root/'agents'/'revisions'/'state.json' if self.root else None
        if not path or not path.is_file():return ''
        value=json.loads(path.read_text(encoding='utf-8')).get('active','')
        if not re.fullmatch('[0-9a-f]{64}',value):raise ValueError('保存されたAgent世代が不正です。')
        return value

    def source(self):
        revision=self.revision
        if not revision:return ''
        path=self.root/'agents'/'revisions'/revision/'agent.wat'
        if path.stat().st_size>256*1024:raise ValueError('保存されたAgentが大きすぎます。')
        source=path.read_text(encoding='utf-8')
        if hashlib.sha256(source.encode()).hexdigest()!=revision:raise ValueError('保存されたAgentの完全性を確認できません。')
        return source

    def activity(self):
        if self.controller:return asdict(self.controller.snapshot())
        path=self.root/'host'/'activity.json' if self.root else None
        value=json.loads(path.read_text(encoding='utf-8')) if path and path.is_file() else {}
        return {**value,'phase':'停止中','next':'STARTで研究を再開します。','snapshot_kind':'saved'}

    def graph(self):
        from .capabilities import validate_description
        revision=self.revision
        path=self.root/'agents'/'revisions'/revision/'capabilities.json' if revision else None
        data=validate_description(json.loads(path.read_text(encoding='utf-8')),revision) if path and path.is_file() else {'nodes':[],'edges':[]}
        if self.store:
            with self.store.lock:
                exists=self.store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='visual_analyzers'").fetchone()
                rows=self.store.db.execute('SELECT * FROM visual_analyzers ORDER BY created DESC LIMIT 64').fetchall() if exists else []
            records=[{**dict(r),'definition':json.loads(r['definition']),'metrics':json.loads(r['metrics'])} for r in rows]
            existing={n['id'] for n in data['nodes']}
            data['nodes'] += [n for n in analyzer_nodes(records) if n['id'] not in existing]
        return data
