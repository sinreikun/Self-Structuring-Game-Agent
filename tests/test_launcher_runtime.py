import base64
import json
from pathlib import Path
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
import pytest

from selfstruct.launch_runtime import Launcher
from selfstruct.resources import Gateway


class Child:
    def __init__(self,success,marker):
        self.exited=not success
        if success:Path(marker).write_text('healthy')
    def poll(self):return 1 if self.exited else None
    def terminate(self):self.exited=True
    def wait(self,timeout):return 1


def test_one_launcher_multiple_updates_offline_rollback_and_data_preserved(tmp_path,monkeypatch):
    key=Ed25519PrivateKey.generate();public=key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()
    body=tmp_path/'body';body.write_bytes(b'body-v1')
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
    body.write_bytes(b'body-v2');second=envelope('2.0.0',2)
    launcher.fetch=lambda u:second
    assert launcher.run() and launcher.state['last-known-good']!=stable
    stable=launcher.state['last-known-good']
    body.write_bytes(b'bad-v3');third=envelope('3.0.0',3)
    launcher.fetch=lambda u:third;fail.add('3.0.0')
    assert launcher.run() and launcher.state['last-known-good']==stable
    launcher.fetch=lambda u:(_ for _ in ()).throw(OSError('offline'))
    assert launcher.run() and '現在の安定版' in launcher.message
    assert user.read_text()=='precious user agent'
    assert all('runtime' in c for c in calls)


def test_invalid_signature_and_unknown_schema_fall_back_and_expired_local_is_usable(tmp_path,monkeypatch):
    key=Ed25519PrivateKey.generate();public=key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()
    folder=tmp_path/'runtime'/'versions'/'stable';folder.mkdir(parents=True)
    binary=folder/'SelfStruct.exe';binary.write_bytes(b'good')
    manifest={'schema':1,'protocol':1,'expires':1,'channel':'Development','version':'1.0.0','application':{'size':4,'sha256':Gateway.digest(binary)}}
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
