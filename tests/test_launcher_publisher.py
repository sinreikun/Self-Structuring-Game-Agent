import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key, Encoding, PublicFormat

from tools.generate_signing_key import generate
from tools import reuse_stable_launcher as launcher


def test_publisher_key_is_private_exclusive_and_matches_its_public_key(tmp_path):
    path=tmp_path/'publisher'/'key.pem'
    public=generate(path)
    key=load_pem_private_key(path.read_bytes(),None)
    assert isinstance(key,Ed25519PrivateKey)
    assert key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()==public
    if __import__('os').name=='posix':assert path.stat().st_mode & 0o777 == 0o600
    original=hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):generate(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest()==original
    with pytest.raises(ValueError,match='outside'):
        generate(Path(__file__).resolve().parents[1]/'should-never-exist.pem')


def test_missing_or_stale_launcher_pin_builds_new_launcher(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls=[]
    monkeypatch.setattr(launcher.subprocess,'run',lambda command,check:calls.append(command))
    launcher.main()
    assert calls[-1][-1]=='tools/build_launcher.py'
    Path('updates').mkdir()
    Path('updates/launcher.json').write_text(json.dumps({'version':'1.0.0','public_key':'old'}))
    launcher.main()
    assert len(calls)==2


def test_matching_launcher_pin_reuses_exact_published_binary(tmp_path,monkeypatch):
    import io
    monkeypatch.chdir(tmp_path)
    body=b'pinned trusted launcher';digest=hashlib.sha256(body).hexdigest()
    Path('updates').mkdir();Path('dist').mkdir()
    Path('dist/build-info.json').write_text('{}')
    Path('updates/launcher.json').write_text(json.dumps({'version':launcher.LAUNCHER_VERSION,
        'public_key':launcher.UPDATE_KEY,'sha256':digest,'tag':'v0.5.0-dev.2'}))
    urls=[]
    def fetch(url,timeout):urls.append(url);return io.BytesIO(body)
    monkeypatch.setattr(launcher,'urlopen',fetch)
    monkeypatch.setattr(launcher.subprocess,'run',lambda *a,**k:pytest.fail('Pinned launcher should be reused'))
    launcher.main()
    assert Path('dist/SelfStructLauncher.exe').read_bytes()==body
    info=json.loads(Path('dist/build-info.json').read_text())
    assert info['launcher_sha256']==digest and info['launcher_version']=='1.1.0'
    assert info['launcher_public_key']==launcher.UPDATE_KEY
    assert '/v0.5.0-dev.2/SelfStructLauncher.exe' in urls[0]
    monkeypatch.setattr(launcher,'urlopen',lambda *a,**k:io.BytesIO(b'tampered'))
    with pytest.raises(ValueError,match='digest'):launcher.main()
