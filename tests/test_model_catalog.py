import pytest
from selfstruct.model_catalog import ModelCatalog,files,fit,matches,parameters,quantization,repo_path
from selfstruct.activity import Activity


def model():
    return {'id':'author/Qwen-Coder-7B-GGUF','sha':'a'*40,'tags':['code','ja','gguf'], 'cardData':{'license':'apache-2.0'},'siblings':[
        {'rfilename':'model-Q4_K_M.gguf','lfs':{'size':4*1024**3,'sha256':'b'*64}},
        {'rfilename':'model-Q8_0.gguf','lfs':{'size':8*1024**3,'sha256':'c'*64}},
        {'rfilename':'model-Q4_K_M-00001-of-00002.gguf','lfs':{'size':2*1024**3,'sha256':'d'*64}}]}


def test_filters_and_fit_are_pre_download_estimates():
    data=model();pc={'ram':16,'free':20,'gpus':[{'vram_gb':8}]}
    assert parameters(data)==7
    assert quantization('model-q4_k_m.gguf')=='Q4_K_M'
    assert matches(data,{'日本語','コード生成','7B前後','Q4','〜8GB','このPCで推奨'},pc)
    assert not matches(data,{'14B前後'},pc)
    assert not matches(data,{'Q6'},pc)
    assert fit(8*1024**3,{'ram':8,'free':100})[0]=='非推奨'
    assert fit(4*1024**3,{'ram':8,'free':100})[0]=='メモリ余裕少'
    assert fit(4*1024**3,{'ram':16,'free':1})[0]=='非推奨'
    data['cardData']['license']='custom'
    assert not matches(data,{'商用利用可'},pc)
    assert matches(data,{'要確認'},pc)


def test_resolver_pins_revision_and_rejects_split_and_unverified(monkeypatch):
    catalog=ModelCatalog();monkeypatch.setattr(catalog,'detail',lambda repo:model())
    asset=catalog.resolve({'repo':model()['id'],'file':'model-Q4_K_M.gguf'})
    assert asset['sha256']=='b'*64 and '/'+('a'*40)+'/' in asset['url']
    with pytest.raises(ValueError):catalog.resolve({'repo':model()['id'],'file':'model-Q4_K_M-00001-of-00002.gguf'})
    for repo in ['../bad','a/b/c','x/y?url=evil','https://evil/x']:
        with pytest.raises(ValueError):repo_path(repo)


def test_search_uses_gguf_metadata_and_file_filters(monkeypatch):
    seen=[]
    monkeypatch.setattr('selfstruct.model_catalog.fetch_json',lambda url:seen.append(url) or [model()])
    catalog=ModelCatalog();monkeypatch.setattr(catalog,'detail',lambda repo:model())
    assert catalog.search('coder',{'Q4'},{'ram':16,'free':20})
    assert 'filter=gguf' in seen[0] and 'search=coder' in seen[0]
    assert not catalog.search('coder',{'Q6'})


def test_trace_is_bounded_immutable_and_records_real_order():
    activity=Activity();original=activity.snapshot()
    for i in range(250):activity.record('OBS',f'frame={i}')
    activity.emit('llm_start');snapshot=activity.snapshot()
    assert not original.trace
    assert len(snapshot.trace)==200
    assert snapshot.trace[-1].kind=='LLM'
    assert snapshot.trace[-1].sequence==251


@pytest.mark.skipif(__import__('os').name!='nt' and not __import__('os').environ.get('DISPLAY'),reason='Display required')
def test_browser_search_details_and_install_are_connected(tmp_path,monkeypatch):
    import tkinter as tk
    import time
    from selfstruct import gui
    from selfstruct.model_browser import ModelBrowser
    from selfstruct.config import Config
    monkeypatch.setattr(gui,'default_root',lambda:tmp_path/'settings')
    Config(root=str(tmp_path/'data')).save(tmp_path/'settings'/'host'/'settings.json')
    monkeypatch.setattr(ModelCatalog,'search',lambda *a,**k:[model()])
    monkeypatch.setattr(ModelCatalog,'detail',lambda *a:model())
    # Avoid any external README request in the UI test.
    import io
    class Opener:
        def open(self,*a,**k):return io.BytesIO(b'# Model card\nLicense: Apache-2.0')
    monkeypatch.setattr('urllib.request.build_opener',lambda *a:Opener())
    window=tk.Tk();app=gui.App(window);errors=[];window.report_callback_exception=lambda *a:errors.append(a)
    browser=ModelBrowser(app);installed=[];monkeypatch.setattr(app,'install_model',installed.append)
    def until(predicate):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            window.update()
            if predicate():return
            time.sleep(.01)
        raise AssertionError(browser.status.get())
    try:
        browser.query.set('coder');browser.search();until(lambda:len(browser.tree.get_children())==1)
        browser.tree.selection_set('0');browser.select();until(lambda:browser.current is not None)
        assert 'Q4_K_M' in browser.file.get()
        assert 'Model card' in browser.detail.get('1.0','end')
        assert all('00001-of' not in label for label in browser.file_labels)
        browser.ack.set(True);browser.install()
        assert installed[0]['file']=='model-Q4_K_M.gguf'
        assert installed[0]['license_ack']
        assert not errors
    finally:
        app.performance_monitor.stop.set();app.closing=True;window.destroy()


def test_local_gguf_import_validates_and_failed_probe_preserves_old_registration(tmp_path,monkeypatch):
    import json,threading
    from selfstruct.resources import Catalog,Gateway,ManagedLLM
    from selfstruct.config import write_json
    source=tmp_path/'user.gguf';source.write_bytes(b'GGUF'+b'x'*30)
    engine=tmp_path/'assets'/'engine'/'llama-server';engine.parent.mkdir(parents=True);engine.write_bytes(b'engine')
    monkeypatch.setattr(Catalog,'resolve_engine',lambda:{'engine':'fixture'})
    monkeypatch.setattr(Gateway,'download',lambda *a,**k:engine)
    monkeypatch.setattr(Gateway,'extract_engine',lambda *a:engine)
    llm=ManagedLLM(tmp_path,100*1024**2);old={'engine':'old','model':'old','engine_sha256':'old','model_sha256':'old'}
    write_json(llm.state_path,old);starts=[]
    monkeypatch.setattr(llm,'start',lambda state=None,**k:starts.append(state) or True)
    monkeypatch.setattr('selfstruct.research.LocalLLM.complete',lambda *a,**k:(_ for _ in ()).throw(TimeoutError('timeout')))
    with pytest.raises(TimeoutError):llm.install({'local_path':str(source)},lambda *a:None,threading.Event())
    assert json.loads(llm.state_path.read_text())==old
    assert starts[-1]==old
    monkeypatch.setattr('selfstruct.research.LocalLLM.complete',lambda *a,**k:'こんにちは')
    llm.install({'local_path':str(source)},lambda *a:None,threading.Event())
    state=json.loads(llm.state_path.read_text())
    assert state['validation']['minimal_inference']
    assert state['validation']['code']=='未試験'
    assert state['model_info']['license']=='ユーザー指定・要確認'
