"""Shared model inventory; individual references never own the model binaries."""
import json
from pathlib import Path
import re
import shutil
import time

from .config import write_json
from .individuals import Individuals, safe_files

MANAGED_URL='http://127.0.0.1:8766/v1'


class MissingModel(ValueError):
    def __init__(self, identifier):
        self.identifier=identifier
        super().__init__('この個体が使用していたLLMが現在保存されていません。')


class ModelLibrary:
    def __init__(self, root):
        self.root=Path(root).resolve()
        self.folder=self.root/'host'/'models'

    def record_path(self, identifier):
        if not re.fullmatch('[0-9a-f]{64}',identifier or ''): raise ValueError('モデルIDが不正です。')
        if self.folder.is_symlink():raise ValueError('モデル管理先にリンクは使用できません。')
        return self.folder/(identifier+'.json')

    def register(self,state):
        identifier=state['model_sha256']
        clean={k:v for k,v in state.items() if k!='previous'}
        clean['registered_at']=time.time()
        write_json(self.record_path(identifier),clean)

    def discover(self):
        """Retain every installed model, including previous successful registrations."""
        selected=self.root/'host'/'llm.json';state={}
        if selected.is_file():state=json.loads(selected.read_text(encoding='utf-8'))
        cursor=state;seen=set()
        while cursor and cursor.get('model_sha256') not in seen:
            identifier=cursor.get('model_sha256','');seen.add(identifier)
            if re.fullmatch('[0-9a-f]{64}',identifier) and not self.record_path(identifier).exists():self.register(cursor)
            cursor=cursor.get('previous') or {}
        template={k:v for k,v in state.items() if k in ('engine','engine_sha256','backend','cpu_fallback')}
        models=self.root/'assets'/'model'
        if models.is_dir():
            for p in models.iterdir():
                if not re.fullmatch('[0-9a-f]{64}',p.name) or p.is_symlink() or not (p/'receipt.json').is_file(): continue
                record=self.record_path(p.name)
                if record.exists(): continue
                info=json.loads((p/'receipt.json').read_text(encoding='utf-8'))
                self.register({**template,'model':str(p/'artifact'),'model_sha256':p.name,'model_info':info})

    def list(self):
        self.discover()
        return [json.loads(p.read_text(encoding='utf-8')) for p in sorted(self.folder.glob('*.json'))]

    def get(self,identifier):
        try: record=self.record_path(identifier)
        except ValueError: raise MissingModel(identifier)
        if not record.exists(): raise MissingModel(identifier)
        state=json.loads(record.read_text(encoding='utf-8'))
        expected=self.root/'assets'/'model'/identifier/'artifact'
        for parent in (expected.parent,expected.parent.parent,expected.parent.parent.parent):
            if parent.is_symlink() or (hasattr(parent,'is_junction') and parent.is_junction()):raise ValueError('モデル保存先にリンクは使用できません。')
        if Path(state.get('model','')).resolve()!=expected.resolve() or not expected.is_file():raise MissingModel(identifier)
        list(safe_files(expected.parent))
        return state

    def prepare(self,llm,config,cancel):
        self.discover()
        identifier=config.model_id
        if not identifier:
            if not llm.state_path.is_file():raise MissingModel('')
            identifier=json.loads(llm.state_path.read_text(encoding='utf-8')).get('model_sha256','')
        state=self.get(identifier)
        state={**state,'context':config.llm_context,'threads':config.llm_threads}
        desired=state.get('backend','cpu') if config.llm_backend=='auto' else config.llm_backend
        if state.get('backend')!=desired or not state.get('engine'):
            from .resources import Gateway, Catalog
            gateway=Gateway(self.root,llm.quota);item=Catalog.resolve_engine(backend=desired)
            engine=gateway.extract_engine(gateway.download(item,'engine',cancel=cancel))
            state.update(engine=str(engine),engine_sha256=Gateway.digest(engine,cancel),backend=desired)
        live=llm.active_state or {}
        alive=llm.process is not None and llm.process.poll() is None
        same=alive and all(live.get(k)==state.get(k) for k in ('model_sha256','context','threads','backend'))
        if not same:
            llm.stop()  # Single loaded model policy: no unbounded VRAM standby pool.
            try:
                if not llm.start(state,cancel=cancel,auto_gpu=config.llm_backend=='auto'):
                    raise OSError('モデルをロードできませんでした。')
            except Exception as error:
                self.register({**state,'last_error':str(error)[:1000]})
                raise
        state.pop('last_error',None)
        if llm.active_state:llm.active_state.pop('last_error',None)
        write_json(llm.state_path,llm.active_state or state)
        self.register(llm.active_state or state)
        return identifier

    def users(self,identifier):
        repo=Individuals(self.root)
        return [p['name'] for p in repo.list() if repo.settings(p['id']).get('model_id')==identifier]

    def status(self,state,llm,using=False):
        if not Path(state.get('model','')).is_file():return '異常'
        live=llm.active_state or {}
        if live.get('model_sha256')==state['model_sha256'] and llm.process and llm.process.poll() is None:
            return '使用中' if using else 'スタンバイ'
        if live.get('model_sha256')==state['model_sha256'] and llm.current_info.get('status') not in (None,'停止') and llm.process and llm.process.poll() is not None:return '異常'
        if state.get('last_error'):return '異常'
        return '停止' if state.get('validation',{}).get('minimal_inference') else '未検証'

    def delete(self,identifier,llm):
        # Same exclusive lock as a running individual: files cannot disappear mid-request.
        with Individuals(self.root).exclusive():
            if llm.active_state and llm.active_state.get('model_sha256')==identifier and llm.process and llm.process.poll() is None:
                raise ValueError('使用中・スタンバイ中のモデルは削除できません。先に停止してください。')
            record=self.record_path(identifier)
            state=self.get(identifier);folder=self.root/'assets'/'model'/identifier
            list(safe_files(folder));shutil.rmtree(folder);record.unlink()
            selection=self.root/'host'/'llm.json'
            if selection.is_file():
                saved=json.loads(selection.read_text(encoding='utf-8'))
                if saved.get('model_sha256')==identifier:selection.unlink()
            # Individual profiles and runtime assets are intentionally independent.
