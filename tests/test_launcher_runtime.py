import base64
import json
from pathlib import Path
import time
import struct

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
import pytest

from selfstruct.launch_runtime import Launcher
from selfstruct.resources import Gateway


def pe(payload=b''):
    header=bytearray(70);header[:2]=b'MZ';struct.pack_into('<I',header,0x3c,64)
    header[64:68]=b'PE\0\0';struct.pack_into('<H',header,68,0x8664)
    return bytes(header)+payload


class Child:
    def __init__(self,success,marker):
        self.exited=not success
        if success:Path(marker).write_text('healthy')
    def poll(self):return 1 if self.exited else None
    def terminate(self):self.exited=True
    def wait(self,timeout):return 1


def test_one_launcher_multiple_updates_offline_rollback_and_data_preserved(tmp_path,monkeypatch):
    key=Ed25519PrivateKey.generate();public=key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()
    body=tmp_path/'body';body.write_bytes(pe(b'body-v1'))
    monkeypatch.setattr(Gateway,'download',lambda self,item,kind:body)
    # install removes only Gateway's cached file, use a fresh copy for each generation.
    def envelope(version,sequence,expires=None,schema=1):
        payload=json.dumps({'schema':schema,'protocol':1,'expires':expires or time.time()+1000,'channel':'Development','sequence':sequence,
            'version':version,'application':{'size':body.stat().st_size,'sha256':Gateway.digest(body)}}).encode()
        return {'payload':base64.b64encode(payload).decode(),'signature':base64.b64encode(key.sign(payload)).decode()}
    first=envelope('1.0.0',1)
    calls=[];fail=set()
    def spawn(args,env):
        calls.append(args[0]);return Child(not any(v in args[0] for v in fail),args[-1])
    user=tmp_path/'experiments'/'agent';user.parent.mkdir();user.write_text('precious user agent')
    launcher=Launcher(tmp_path,public,'url',fetch=lambda u:first,spawn=spawn,timeout=.1)
    assert launcher.run();stable=launcher.state['last-known-good']
    body.write_bytes(pe(b'body-v2'));second=envelope('2.0.0',2)
    launcher.fetch=lambda u:second
    assert launcher.run() and launcher.state['last-known-good']!=stable
    assert launcher.state['previous-known-good']==stable
    stable=launcher.state['last-known-good']
    body.write_bytes(b'bad-v3');third=envelope('3.0.0',3)
    launcher.fetch=lambda u:third;fail.add('3.0.0')
    assert launcher.run() and launcher.state['last-known-good']==stable
    launcher.fetch=lambda u:(_ for _ in ()).throw(OSError('offline'))
    assert launcher.run() and '現在の安定版' in launcher.message
    corrupt=launcher.root/'versions'/stable/'SelfStruct.exe'
    corrupt.write_bytes(b'corrupted stable')
    assert launcher.run() and launcher.state['current']==launcher.state['previous-known-good']
    assert user.read_text()=='precious user agent'
    assert all('runtime' in c for c in calls)


def test_invalid_signature_and_unknown_schema_fall_back_and_expired_local_is_usable(tmp_path,monkeypatch):
    key=Ed25519PrivateKey.generate();public=key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()
    folder=tmp_path/'runtime'/'versions'/'stable';folder.mkdir(parents=True)
    binary=folder/'SelfStruct.exe';binary.write_bytes(pe(b'good'))
    manifest={'schema':1,'protocol':1,'expires':1,'channel':'Development','version':'1.0.0','application':{'size':binary.stat().st_size,'sha256':Gateway.digest(binary)}}
    payload=json.dumps(manifest).encode();envelope={'payload':base64.b64encode(payload).decode(),'signature':base64.b64encode(key.sign(payload)).decode()}
    (folder/'manifest.json').write_text(json.dumps(envelope));(tmp_path/'runtime'/'state.json').write_text(json.dumps({'last-known-good':'stable'}))
    launcher=Launcher(tmp_path,public,'url',fetch=lambda u:{'payload':'bad','signature':'bad'},spawn=lambda args,env:Child(True,args[-1]))
    assert launcher.run()
    manifest.update(schema=99,expires=time.time()+1000)
    payload=json.dumps(manifest).encode()
    launcher.fetch=lambda u:{'payload':base64.b64encode(payload).decode(),'signature':base64.b64encode(key.sign(payload)).decode()}
    assert launcher.run()
    binary.write_bytes(b'corrupt')
    with pytest.raises(RuntimeError):launcher.run()


@pytest.mark.skipif(__import__('os').name!='nt',reason='Windows application job')
def test_owned_job_stops_child_after_launcher_parent_already_exited():
    import os
    import sys
    from selfstruct.owned_process import start_application
    app=start_application([sys.executable,str(Path('tests/fixtures/launcher.py').resolve())],dict(os.environ),retain_job=True)
    try:
        assert app.wait(timeout=10)==0
        info=app.Accounting()
        assert app.api.QueryInformationJobObject(app.job,1,__import__('ctypes').byref(info),__import__('ctypes').sizeof(info),None)
        assert info.active>=1
        app.terminate_tree()
        assert app.job is None
    finally:
        app.terminate_tree()


@pytest.mark.skipif(__import__('os').name!='nt',reason='Windows application job')
def test_detaching_healthy_application_does_not_kill_it():
    import os
    import sys
    from selfstruct.owned_process import start_application
    app=start_application([sys.executable,str(Path('tests/fixtures/message_window.py').resolve())],dict(os.environ))
    try:
        app.detach()
        assert app.job is None and app.poll() is None
    finally:
        if app.poll() is None:app.child.terminate()
        app.child.wait(timeout=10)


def test_key_migration_preserves_old_cached_body_offline_and_rejects_legacy_network_updates(tmp_path, monkeypatch):
    old=Ed25519PrivateKey.generate();new=Ed25519PrivateKey.generate()
    public=lambda key:key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()
    def signed(key,body,version,sequence):
        payload=json.dumps({'schema':1,'protocol':1,'channel':'Development','sequence':sequence,
                            'expires':time.time()+1000,'version':version,
                            'application':{'size':body.stat().st_size,'sha256':Gateway.digest(body)}}).encode()
        return {'payload':base64.b64encode(payload).decode(),'signature':base64.b64encode(key.sign(payload)).decode()}
    cached=tmp_path/'runtime'/'versions'/'old-stable';cached.mkdir(parents=True)
    old_body=cached/'SelfStruct.exe';old_body.write_bytes(pe(b'previous verified application'))
    (cached/'manifest.json').write_text(json.dumps(signed(old,old_body,'0.4.1',10)))
    (tmp_path/'runtime'/'state.json').write_text(json.dumps({'last-known-good':'old-stable','sequence':10}))
    individual=tmp_path/'experiments'/'individual'/'host';individual.mkdir(parents=True)
    database=individual/'experience.sqlite3';database.write_bytes(b'preserved evidence and learned state')
    model=tmp_path/'assets'/'model'/'existing';model.mkdir(parents=True);(model/'artifact').write_bytes(b'existing model')
    calls=[]
    def spawn(args,env):
        calls.append(args[0]);return Child(True,args[-1])
    launcher=Launcher(tmp_path,public(new),'url',legacy_keys=(public(old),),
                      fetch=lambda _:(_ for _ in ()).throw(OSError('offline')),spawn=spawn,timeout=.1)
    assert launcher.run() and calls[-1]==str(old_body)
    downloaded=[]
    def download(self,item,kind):
        downloaded.append(True)
        path=tmp_path/'download';path.write_bytes(pe(b'new trusted application'))
        return path
    monkeypatch.setattr(Gateway,'download',download)
    # A legacy-signed network update must not inherit local-cache privileges.
    launcher.fetch=lambda _:signed(old,old_body,'9.0.0',100)
    assert launcher.run() and not downloaded and calls[-1]==str(old_body)
    proposed=tmp_path/'proposal';proposed.write_bytes(pe(b'new trusted application'))
    launcher.fetch=lambda _:signed(new,proposed,'0.5.0',11)
    assert launcher.run() and downloaded
    assert launcher.state['previous-known-good']=='old-stable'
    assert launcher.state['last-known-good'].startswith('0.5.0-')
    # If the new cached body is damaged, rollback still authenticates the old body.
    current=launcher.root/'versions'/launcher.state['last-known-good']/'SelfStruct.exe'
    current.write_bytes(b'corrupted')
    launcher.fetch=lambda _:(_ for _ in ()).throw(OSError('offline'))
    assert launcher.run() and calls[-1]==str(old_body)
    assert database.read_bytes()==b'preserved evidence and learned state'
    assert (model/'artifact').read_bytes()==b'existing model'
