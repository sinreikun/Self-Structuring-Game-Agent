"""Durable individuals. Existing experiment directories remain in place (no image copies).

Profiles/settings belong to an individual; engines, models and UI settings never do.
All mutations are serialized by the same root lock as Controller.
"""
from contextlib import contextmanager, closing
from dataclasses import asdict, replace
import json
from pathlib import Path
import re
import shutil
import sqlite3
import time
import uuid
import zipfile

from .config import Config, InstanceLock, write_json

INDIVIDUAL_FIELDS = frozenset('game_exe game_args connection_mode target_pid target_created target_exe target_hwnd window_title keys quota_bytes llm_url llm_model model_id model_name model_quant llm_context llm_backend llm_threads mode research_every restart_after max_restarts'.split())


def compatible_keys(old, new):
    return len(new) >= len(old) and new[:len(old)] == old


def safe_files(folder):
    """Reject links/junctions, including directories; never follow an external tree."""
    if folder.is_symlink() or (hasattr(folder, 'is_junction') and folder.is_junction()):
        raise ValueError('リンクを含む個体は操作できません。')
    for child in folder.iterdir():
        if child.is_symlink() or (hasattr(child, 'is_junction') and child.is_junction()):
            raise ValueError('リンクを含む個体は操作できません。')
        if child.is_dir():
            yield from safe_files(child)
        elif child.is_file():
            yield child


class Individuals:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.folder = self.root / 'experiments'
        self.pointer = self.root / 'host' / 'experiment.json'

    @contextmanager
    def exclusive(self, locked=False):
        lock = None if locked else InstanceLock(self.root / 'host' / 'experiment.lock')
        try:
            yield
        finally:
            if lock: lock.close()

    def path(self, identifier):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', identifier or ''):
            raise ValueError('個体IDが不正です。')
        path = self.folder / identifier
        for p in (self.folder, path):
            if p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()):
                raise ValueError('個体フォルダにリンクは使用できません。')
        if not path.resolve().is_relative_to(self.root): raise ValueError('個体フォルダが不正です。')
        return path

    def active(self):
        return json.loads(self.pointer.read_text(encoding='utf-8')).get('id', '') if self.pointer.exists() else ''

    def profile(self, identifier):
        return json.loads((self.path(identifier) / 'profile.json').read_text(encoding='utf-8'))

    def settings(self, identifier):
        return json.loads((self.path(identifier) / 'settings.json').read_text(encoding='utf-8'))

    def migrate(self, config, *, locked=False):
        """Register pre-profile experiments without touching evidence or database contents.

        Older inactive settings were never saved. Mark those unknown instead of
        claiming today's configuration is their historical configuration.
        """
        with self.exclusive(locked):
            if not self.folder.exists(): return
            active = self.active()
            pointer = json.loads(self.pointer.read_text(encoding='utf-8')) if active else {}
            for folder in sorted(self.folder.iterdir(), key=lambda p:p.stat().st_mtime):
                if not folder.is_dir() or folder.name.startswith('.'): continue
                folder = self.path(folder.name)
                if (folder / 'profile.json').exists(): continue
                dbfile = folder / 'host' / 'experience.sqlite3'
                if not dbfile.is_file(): continue
                mode, created, last = 'Newborn', folder.stat().st_mtime, folder.stat().st_mtime
                with closing(sqlite3.connect(dbfile.as_uri()+'?mode=ro', uri=True)) as db:
                    row = db.execute('SELECT mode,started FROM sessions ORDER BY started LIMIT 1').fetchone()
                    if row: mode, created = row
                    row = db.execute('SELECT max(coalesce(ended,started)) FROM sessions').fetchone()
                    if row and row[0]: last = row[0]
                settings = {k:v for k,v in asdict(config).items() if k in INDIVIDUAL_FIELDS} if folder.name == active else {}
                if folder.name == active:
                    settings.update({k:v for k,v in pointer.items() if k in INDIVIDUAL_FIELDS})
                settings['mode'] = mode
                llm = self.root/'host'/'llm.json'
                if folder.name == active and llm.is_file() and not settings.get('model_id'):
                    state=json.loads(llm.read_text(encoding='utf-8'))
                    if settings.get('llm_url')=='http://127.0.0.1:8766/v1': settings['model_id']=state.get('model_sha256','')
                write_json(folder/'settings.json',settings)
                write_json(folder/'profile.json',dict(schema=1,id=folder.name,name=self.next_name(),created=created,last_used=last,parent=None,creation='legacy',settings_review_required=folder.name!=active,elapsed=0))

    def next_name(self):
        numbers=[int(m[1]) for p in self.list() if (m:=re.match(r'GEN-(\d+)',p['name']))]
        return f'GEN-{max(numbers,default=0)+1:04d}'

    def list(self):
        if not self.folder.exists(): return []
        result=[]
        for p in self.folder.iterdir():
            if p.name.startswith('.'): continue
            if p.is_dir() and (p/'profile.json').is_file():
                result.append(self.profile(p.name))
        return sorted(result,key=lambda x:x['created'])

    def _point(self, identifier, settings):
        write_json(self.pointer, {'id':identifier, **{k:v for k,v in settings.items() if k in ('mode','keys','game_exe','target_exe')}})

    def create(self, config, *, name=None, parent=None, creation='newborn', locked=False):
        with self.exclusive(locked):
            identifier=uuid.uuid4().hex; folder=self.path(identifier);folder.mkdir(parents=True)
            settings={k:v for k,v in asdict(config).items() if k in INDIVIDUAL_FIELDS}
            if settings['mode']=='Experienced': settings['mode']='Newborn'
            now=time.time()
            write_json(folder/'settings.json',settings)
            write_json(folder/'profile.json',dict(schema=1,id=identifier,name=name or self.next_name(),created=now,last_used=now,parent=parent,creation=creation,settings_review_required=False,elapsed=0))
            self._point(identifier,settings)
            return identifier

    def load(self, identifier, common, *, locked=False):
        with self.exclusive(locked):
            profile=self.profile(identifier)
            if profile.get('settings_review_required'):
                raise ValueError('この旧個体は当時の設定が保存されていません。個体管理の「設定を補完」で許可キー順序・ゲーム・LLMを確認してください。')
            settings=self.settings(identifier)
            config=replace(common, **{k:v for k,v in settings.items() if k in INDIVIDUAL_FIELDS},agent_id=identifier,resume=True,auto_run=False)
            # Guided provenance must never become Experienced implicitly.
            if config.mode != 'Guided': config=replace(config,mode='Experienced')
            self._point(identifier,settings)
            profile['last_used']=time.time();write_json(self.path(identifier)/'profile.json',profile)
            return config

    def save_settings(self, identifier, config, *, locked=False, review=False, validate_only=False):
        with self.exclusive(locked):
            previous=self.settings(identifier);profile=self.profile(identifier)
            if not review:
                if not compatible_keys(previous.get('keys',config.keys), config.keys):
                    raise ValueError('既存キーの削除・並べ替えは入力番号を変えます。キーは末尾へ追加するか、設定のみの複製／NEWBORNを作成してください。')
                old_target=previous.get('target_exe') or previous.get('game_exe')
                new_target=config.target_exe or config.game_exe
                if old_target and old_target.casefold()!=new_target.casefold():
                    raise ValueError('対象ゲームが異なります。設定のみの複製またはNEWBORNを作成してください。')
                if previous.get('mode')=='Guided' and config.mode!='Guided':
                    raise ValueError('人間知識を含む個体はGuidedとして継続してください。')
                if previous.get('mode')!='Guided' and config.mode=='Guided':
                    raise ValueError('知識モード変更には設定のみの複製を使用してください。')
            if validate_only:return
            settings={k:v for k,v in asdict(config).items() if k in INDIVIDUAL_FIELDS}
            if settings['mode']=='Experienced': settings['mode']=previous.get('mode','Newborn')
            write_json(self.path(identifier)/'settings.json',settings)
            profile['settings_review_required']=False;write_json(self.path(identifier)/'profile.json',profile)
            if self.active()==identifier: self._point(identifier,settings)

    def rename(self, identifier, name):
        if not name.strip() or len(name)>100: raise ValueError('個体名は1〜100文字で指定してください。')
        with self.exclusive():
            profile=self.profile(identifier);profile['name']=name.strip();write_json(self.path(identifier)/'profile.json',profile)

    def finish(self, identifier, config, elapsed, *, locked=False):
        with self.exclusive(locked):
            self.save_settings(identifier,config,locked=True)
            profile=self.profile(identifier);profile['last_used']=time.time();profile['elapsed']=profile.get('elapsed',0)+elapsed
            write_json(self.path(identifier)/'profile.json',profile)

    def clone(self, identifier, common, *, settings_only=False):
        with self.exclusive():
            source=self.path(identifier);profile=self.profile(identifier)
            if profile.get('settings_review_required'): raise ValueError('先に旧個体の設定を補完してください。')
            cfg=replace(common,**{k:v for k,v in self.settings(identifier).items() if k in INDIVIDUAL_FIELDS})
            if settings_only: return self.create(cfg,name=profile['name']+'（設定）',parent=identifier,creation='settings',locked=True)
            list(safe_files(source))
            identifier_new=uuid.uuid4().hex;target=self.path(identifier_new)
            staging=self.folder/('.copy-'+identifier_new)
            try:
                shutil.copytree(source,staging)
                # SQLite backup includes committed WAL pages. Never copy stale lock/WAL files.
                dbpath=source/'host'/'experience.sqlite3'
                if dbpath.exists():
                    for suffix in ('-wal','-shm'): (staging/'host'/('experience.sqlite3'+suffix)).unlink(missing_ok=True)
                    with closing(sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(staging/'host'/'experience.sqlite3')) as dst: src.backup(dst)
                now=time.time();profile.update(id=identifier_new,name=profile['name']+'（複製）',parent=identifier,creation='full-copy',created=now,last_used=now)
                write_json(staging/'profile.json',profile);staging.rename(target)
            finally:
                if staging.exists(): shutil.rmtree(staging)
            return identifier_new

    def delete(self, identifier):
        with self.exclusive():
            if identifier==self.active(): raise ValueError('現在ロード中の個体は削除できません。別の個体をロードしてください。')
            path=self.path(identifier);self.profile(identifier);list(safe_files(path))
            shutil.rmtree(path)

    def export(self, identifier, destination):
        destination=Path(destination)
        if destination.resolve().is_relative_to(self.root): raise ValueError('エクスポート先は作業フォルダの外を選択してください。')
        with self.exclusive():
            folder=self.path(identifier);items=list(safe_files(folder))
            temporary=destination.with_name(destination.name+'.pending')
            try:
                with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as out:
                    for p in items:
                        if p.name.endswith(('-wal','-shm')): continue
                        if p==folder/'host'/'experience.sqlite3':
                            import tempfile
                            with tempfile.TemporaryDirectory(dir=destination.parent) as d:
                                backup=Path(d)/'experience.sqlite3'
                                with closing(sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(backup)) as dst: src.backup(dst)
                                out.write(backup,'host/experience.sqlite3')
                        else: out.write(p,p.relative_to(folder))
                temporary.replace(destination)
            finally: temporary.unlink(missing_ok=True)

    def summary(self, identifier):
        folder=self.path(identifier);profile=self.profile(identifier);settings=self.settings(identifier)
        size=sum(p.stat().st_size for p in safe_files(folder))
        counts={};revision='未開始';dbfile=folder/'host'/'experience.sqlite3'
        if dbfile.exists():
            with closing(sqlite3.connect(dbfile.as_uri()+'?mode=ro',uri=True)) as db:
                row=db.execute("SELECT value FROM meta WHERE key='events_lifetime'").fetchone()
                counts['experience']=int(row[0]) if row else db.execute('SELECT count(*) FROM events').fetchone()[0]
                counts['improvements']=db.execute('SELECT count(*) FROM reports WHERE adopted=1').fetchone()[0]
                for key,cond in {'discoveries':"kind='discovery'",'hypotheses':"kind='hypothesis'",'supported':"validation IN ('supported','reproduced')",'refuted':"validation='refuted'",'skills':"kind='skill'"}.items(): counts[key]=db.execute('SELECT count(*) FROM knowledge WHERE '+cond).fetchone()[0]
        checkpoint=folder/'host'/'checkpoint.json'
        if checkpoint.exists(): revision=json.loads(checkpoint.read_text(encoding='utf-8')).get('revision',revision)
        return dict(profile=profile,settings=settings,counts=counts,bytes=size,revision=revision)
