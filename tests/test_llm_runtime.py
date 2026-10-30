import http.server
import json
import threading
import time

import pytest
from selfstruct.research import LocalLLM,research_prompt


class Server(http.server.ThreadingHTTPServer):
    daemon_threads=True


@pytest.fixture
def endpoint():
    state={'headers_first':False,'headers_sent':False,'delay':0,'finish':'stop','content':'こんにちは','reasoning':None,'requests':[]}
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['requests'].append(payload)
            message={'content':state['content'],'reasoning_content':state['reasoning']}
            body=json.dumps({'choices':[{'finish_reason':state['finish'],'message':message}]}).encode()
            try:
                if not state['headers_first']:time.sleep(state['delay'])
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.flush()
                state['headers_sent']=True
                if state['headers_first']:time.sleep(state['delay'])
                self.wfile.write(body)
            except (OSError,BrokenPipeError):pass
    server=Server(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    yield LocalLLM(f'http://127.0.0.1:{server.server_port}/v1','fixture'),state
    server.shutdown();server.server_close();worker.join()


def test_probe_accepts_length_but_research_rejects_incomplete_code(endpoint):
    client,state=endpoint;client.managed=True;state['finish']='length'
    assert client.complete('probe',allow_partial=True)=='こんにちは'
    with pytest.raises(ValueError,match='途中'):client.complete('research')
    state['content']=None;state['reasoning']='Thinking…'
    assert client.complete('probe',allow_partial=True)=='Thinking…'
    assert state['requests'][0]['chat_template_kwargs']['enable_thinking'] is False


def test_cancel_interrupts_prefill_without_waiting_for_response(endpoint):
    client,state=endpoint;state['delay']=10;cancel=threading.Event();errors=[]
    def run():
        try:client.complete('slow prefill',cancel=cancel)
        except Exception as error:errors.append(error)
    worker=threading.Thread(target=run);worker.start()
    deadline=time.monotonic()+2
    while not state['requests'] and time.monotonic()<deadline:time.sleep(.01)
    start=time.monotonic();cancel.set();worker.join(timeout=2)
    assert not worker.is_alive() and time.monotonic()-start<2
    assert isinstance(errors[0],InterruptedError)


def test_deadline_interrupts_request(endpoint):
    client,state=endpoint;state['delay']=10
    with pytest.raises(TimeoutError):client.complete('slow',timeout=.2)


def test_real_prompt_excludes_large_report_metrics_and_limits_experiences():
    from pathlib import Path
    rows=[{'id':i,'action':'A','feature':i,'change':.1234567} for i in range(64)]
    prompt=research_prompt(Path('src/selfstruct/assets/seed.wat').read_text(),rows,['A'],[{'summary':'前回評価','metrics':'x'*100000}])
    assert len(prompt.encode())<8000
    assert '"id":48' in prompt and '"id":0,' not in prompt
    assert 'x'*100 not in prompt
    assert 'unknown' in prompt and '(export "learn")' in prompt


def test_gpu_release_selection_does_not_choose_cpu_or_cuda_dependency(monkeypatch):
    from selfstruct.resources import Catalog
    assets=[{'name':'llama-test-bin-win-cpu-x64.zip','size':1,'digest':'sha256:'+'a'*64,'browser_download_url':'https://github.com/cpu'},
            {'name':'llama-test-bin-win-vulkan-x64.zip','size':2,'digest':'sha256:'+'b'*64,'browser_download_url':'https://github.com/vulkan'}]
    monkeypatch.setattr('selfstruct.resources.fetch_json',lambda url:[{'tag_name':'fixture','assets':assets}])
    result=Catalog.resolve_engine('nt','vulkan')
    assert result['backend']=='vulkan' and result['sha256']=='b'*64


@pytest.mark.parametrize('layers',[5,0])
def test_gpu_status_comes_from_runtime_evidence_and_failure_uses_verified_cpu(tmp_path,monkeypatch,layers):
    import io
    import hashlib
    from selfstruct.resources import ManagedLLM
    from selfstruct.config import write_json
    expanded=tmp_path/'assets'/'engine'/'fixture'/'expanded';expanded.mkdir(parents=True)
    gpu=expanded/'gpu.exe';gpu.write_bytes(b'gpu')
    cpu=expanded/'cpu.exe';cpu.write_bytes(b'cpu')
    digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    write_json(expanded.parent/'expanded-files.json',{p.name:digest(p) for p in (gpu,cpu)})
    model=tmp_path/'assets'/'model'/'fixture'/'artifact';model.parent.mkdir(parents=True);model.write_bytes(b'GGUFtest')
    manager=ManagedLLM(tmp_path,100*1024**2);commands=[]
    class Process:
        def __init__(self,command,**kwargs):
            commands.append(command);self.closed=False
            n=layers if command[0]==str(gpu) else 0
            self.stdout=io.BytesIO(f'load_tensors: offloaded {n}/6 layers to GPU\n'.encode())
        def poll(self):return 0 if self.closed else None
        def terminate(self):self.closed=True
        def wait(self,timeout):return 0
    def health(client):manager.reader.join(timeout=1)
    monkeypatch.setattr('selfstruct.resources.subprocess.Popen',Process)
    monkeypatch.setattr(LocalLLM,'health',health)
    state={'engine':str(gpu),'engine_sha256':digest(gpu),'model':str(model),'model_sha256':digest(model),
           'backend':'vulkan','model_info':{'name':'test/model'},'cpu_fallback':{'engine':str(cpu),'engine_sha256':digest(cpu)}}
    try:
        manager.start(state)
        assert commands[0][commands[0].index('-ngl')+1]=='auto'
        assert manager.current_info['name']=='test/model'
        if layers:
            assert manager.current_info['gpu_layers']==5 and manager.active_state['backend']=='vulkan'
        else:
            assert manager.current_info['backend']=='cpu' and manager.current_info['gpu_error']
            assert manager.active_state['engine']==str(cpu)
            assert commands[-1][commands[-1].index('-ngl')+1]=='0'
    finally:manager.stop()


def test_cancel_interrupts_body_after_connection_close_headers(endpoint):
    client,state=endpoint;state['headers_first']=True;state['delay']=10
    cancel=threading.Event();errors=[]
    def run():
        try:client.complete('slow body',cancel=cancel)
        except Exception as error:errors.append(error)
    worker=threading.Thread(target=run);worker.start()
    deadline=time.monotonic()+2
    while not state['headers_sent'] and time.monotonic()<deadline:time.sleep(.01)
    cancel.set();worker.join(timeout=2)
    assert not worker.is_alive() and isinstance(errors[0],InterruptedError)
