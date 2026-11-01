"""Exact revision binding, bounded optional acquisition, and safe text fallback."""
import hashlib
import io
import threading
from types import SimpleNamespace

import pytest

from selfstruct.config import write_json
from selfstruct.model_vision import prepare_projector, resolve_projector, text_fallback
from selfstruct.resources import ManagedLLM


REV='a'*40
SHA='b'*64
INFO={'url':f'https://huggingface.co/owner/model/resolve/{REV}/model-Q4.gguf','version':REV,
      'sha256':SHA,'license':'apache-2.0','trust':'Verified Candidate'}


def metadata():
    return {'sha':REV,'siblings':[{'rfilename':'model-Q4.gguf','lfs':{'sha256':SHA}},
                                 {'rfilename':'mmproj-F16.gguf','lfs':{'sha256':'c'*64,'size':123}}]}


def test_projector_uses_pinned_revision_and_exact_weight_identity(monkeypatch):
    calls=[]
    def fetch(url):calls.append(url);return metadata()
    monkeypatch.setattr('selfstruct.model_vision.fetch_json',fetch)
    item=resolve_projector(INFO)
    assert calls==[f'https://huggingface.co/api/models/owner/model/revision/{REV}?blobs=true']
    assert item['paired_model_sha256']==SHA and item['role']=='vision_projector'
    assert f'/resolve/{REV}/mmproj-F16.gguf' in item['url']
    for data in ({**metadata(),'sha':'d'*40},
                 {'sha':REV,'siblings':[{'rfilename':'model-Q4.gguf','lfs':{'sha256':'e'*64}}]}):
        monkeypatch.setattr('selfstruct.model_vision.fetch_json',lambda url:data)
        with pytest.raises(ValueError):resolve_projector(INFO)
    assert resolve_projector({**INFO,'url':'https://other.invalid/model.gguf'}) is None
    assert resolve_projector({**INFO,'version':'mutable-main'}) is None


def test_no_projector_and_failed_acquisition_remain_explicitly_text_only(tmp_path,monkeypatch):
    manager=SimpleNamespace(root=tmp_path,quota=1024**3)
    state={'model_sha256':SHA,'model_info':INFO}
    monkeypatch.setattr('selfstruct.model_vision.fetch_json',lambda url:{'sha':REV,'siblings':metadata()['siblings'][:1]})
    no=prepare_projector(manager,state)
    assert no['vision_resolution']['status']=='not_available' and 'projector' not in no
    monkeypatch.setattr('selfstruct.model_vision.fetch_json',lambda url:(_ for _ in ()).throw(OSError('offline')))
    assert prepare_projector(manager,no)==no  # No repeated network on ordinary starts.
    failed=prepare_projector(manager,state)
    assert failed['vision_resolution']['status']=='unavailable' and 'offline' in failed['vision_resolution']['error']
    monkeypatch.setattr('selfstruct.model_vision.fetch_json',lambda url:metadata())
    monkeypatch.setattr('selfstruct.model_vision.Gateway.download',lambda *a,**kw:(_ for _ in ()).throw(InterruptedError('cancelled')))
    with pytest.raises(InterruptedError):prepare_projector(manager,failed,cancel=threading.Event())


def test_projector_is_not_registered_as_a_language_model(tmp_path,monkeypatch):
    manager=SimpleNamespace(root=tmp_path,quota=1024**3)
    seen=[]
    monkeypatch.setattr('selfstruct.model_vision.fetch_json',lambda url:metadata())
    def download(gateway,item,kind,*args):seen.append(kind);return tmp_path/'assets/library/projector'
    monkeypatch.setattr('selfstruct.model_vision.Gateway.download',download)
    ready=prepare_projector(manager,{'model_sha256':SHA,'model_info':INFO})
    assert seen==['library'] and ready['projector_sha256']=='c'*64
    failed=text_fallback(ready,OSError('incompatible architecture'))
    assert 'projector' not in failed and failed['vision_resolution']['status']=='runtime_failed'


@pytest.mark.parametrize('failure',[None,'tampered','wrong_pair','outside'])
def test_runtime_loads_only_integrity_checked_model_bound_projector(tmp_path,monkeypatch,failure):
    engine=tmp_path/'assets/engine/test/expanded/llama-server';engine.parent.mkdir(parents=True);engine.write_bytes(b'engine')
    model=tmp_path/'assets/model/test/artifact';model.parent.mkdir(parents=True);model.write_bytes(b'GGUFmodel')
    projector=tmp_path/'assets/library/test/artifact';projector.parent.mkdir(parents=True);projector.write_bytes(b'GGUFprojector')
    digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    write_json(engine.parent.parent/'expanded-files.json',{engine.name:digest(engine)})
    state={'engine':str(engine),'engine_sha256':digest(engine),'model':str(model),'model_sha256':digest(model),
           'projector':str(projector),'projector_sha256':digest(projector),
           'projector_info':{'paired_model_sha256':digest(model)},'backend':'cpu'}
    if failure=='tampered':projector.write_bytes(b'changed')
    if failure=='wrong_pair':state['projector_info']['paired_model_sha256']='f'*64
    if failure=='outside':state['projector']=str(tmp_path.parent/'outside')
    commands=[]
    class Process:
        def __init__(self,command,**kwargs):commands.append(command);self.stdout=io.BytesIO();self.closed=False
        def poll(self):return 0 if self.closed else None
        def terminate(self):self.closed=True
        def wait(self,timeout):return 0
    monkeypatch.setattr('selfstruct.resources.subprocess.Popen',Process)
    def health(client):client.capabilities={'input':'vision','status':'runtime_reported'}
    monkeypatch.setattr('selfstruct.research.LocalLLM.health',health)
    manager=ManagedLLM(tmp_path,1024**3)
    try:
        if failure:
            with pytest.raises((ValueError,FileNotFoundError)):manager.start(state)
            assert not commands
        else:
            assert manager.start(state)
            assert commands[0][commands[0].index('--mmproj')+1]==str(projector)
            assert '--no-mmproj-offload' in commands[0]
            assert manager.active_state['capabilities']['input']=='vision'
    finally:manager.stop()


def test_existing_model_start_recovers_to_text_and_retains_reason(tmp_path,monkeypatch):
    from selfstruct.model_library import ModelLibrary
    from selfstruct.config import Config
    identifier='a'*64
    artifact=tmp_path/'assets/model'/identifier/'artifact';artifact.parent.mkdir(parents=True);artifact.write_bytes(b'GGUFfixture')
    state={'model_sha256':identifier,'model':str(artifact),'engine':'engine','engine_sha256':'hash',
           'backend':'cpu','validation':{'minimal_inference':True},'projector':'projector','projector_sha256':'b'*64}
    library=ModelLibrary(tmp_path);library.register(state)
    class Runtime:
        root=tmp_path;quota=1024**3;state_path=tmp_path/'host/llm.json'
        process=None;active_state=None;starts=[]
        def stop(self):self.process=None
        def start(self,state,**kwargs):
            self.starts.append(state)
            if state.get('projector'):raise OSError('unsupported projector')
            self.active_state=state;self.process=SimpleNamespace(poll=lambda:None);return True
    llm=Runtime()
    library.prepare(llm,Config(root=str(tmp_path),model_id=identifier),threading.Event())
    assert len(llm.starts)==2 and 'projector' not in llm.active_state
    assert library.get(identifier)['vision_resolution']['status']=='runtime_failed'
    assert 'unsupported projector' in library.get(identifier)['vision_resolution']['error']
    library.prepare(llm,Config(root=str(tmp_path),model_id=identifier),threading.Event())
    assert len(llm.starts)==2


def test_ambiguous_projector_names_are_not_guessed(monkeypatch):
    data=metadata();data['siblings'].append({'rfilename':'mmproj-another-model-F16.gguf','lfs':{'sha256':'d'*64,'size':123}})
    monkeypatch.setattr('selfstruct.model_vision.fetch_json',lambda url:data)
    assert resolve_projector(INFO) is None
