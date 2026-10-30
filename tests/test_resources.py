import base64
import hashlib
import io
import json
from pathlib import Path
import time
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from selfstruct.resources import Gateway, SafeRedirect, check_url, verify_catalog


def test_signed_catalog_and_expiry():
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw().hex()
    payload = json.dumps({"schema":1,"expires":time.time()+100, "models":[]}).encode()
    envelope = {"payload":base64.b64encode(payload).decode(), "signature":base64.b64encode(key.sign(payload)).decode()}
    assert verify_catalog(envelope, public)["models"] == []
    envelope["payload"] = base64.b64encode(payload + b" ").decode()
    with pytest.raises(Exception):
        verify_catalog(envelope, public)
    expired = json.dumps({"schema":1,"expires":0}).encode()
    with pytest.raises(ValueError):
        verify_catalog({"payload":base64.b64encode(expired).decode(), "signature":base64.b64encode(key.sign(expired)).decode()}, public)


@pytest.mark.parametrize("url", ["http://github.com/a", "https://evil.invalid/a", "https://github.com@evil.invalid/a", "https://github.com:444/a", "file:///tmp/a"])
def test_untrusted_destinations(url):
    with pytest.raises(ValueError):
        check_url(url, {"github.com"})


def test_gateway_resume_integrity_and_limit(tmp_path, monkeypatch):
    payload = b"trusted model bytes" * 100
    sha = hashlib.sha256(payload).hexdigest()
    gateway = Gateway(tmp_path, 16 * 1024**2, {"files.example"})
    folder = tmp_path / "assets" / "model" / sha
    folder.mkdir(parents=True)
    (folder / "artifact.part").write_bytes(payload[:137])
    requests = []
    class Response(io.BytesIO):
        status = 206
        headers = {"Content-Range":f"bytes 137-{len(payload)-1}/{len(payload)}"}
    class Opener:
        def open(self, req, timeout):
            requests.append(req)
            return Response(payload[137:])
    monkeypatch.setattr("selfstruct.resources.build_opener", lambda *args: Opener())
    item = {"url":"https://files.example/model", "size":len(payload),"sha256":sha, "trust":"Trusted", "license":"Apache-2.0"}
    result = gateway.download(item, "model")
    assert result.read_bytes() == payload
    assert requests[0].get_header("Range") == "bytes=137-"
    # Cached verified bytes do not cause another request.
    gateway.download(item, "model")
    assert len(requests) == 1
    with pytest.raises(ValueError):
        gateway.download(item | {"trust":"Unknown"}, "library")
    with pytest.raises(OSError):
        Gateway(tmp_path, 1, {"files.example"}).download(item | {"sha256":"1"*64}, "model")


def test_bad_checksum_never_activates(tmp_path, monkeypatch):
    class Response(io.BytesIO):
        status, headers = 200, {}
    class Opener:
        def open(self, *args, **kwargs):
            return Response(b"evil")
    monkeypatch.setattr("selfstruct.resources.build_opener", lambda *args: Opener())
    item = {"url":"https://files.example/model", "size":4,"sha256":"1"*64,"trust":"Trusted", "license":"MIT"}
    with pytest.raises(ValueError, match="完全性"):
        Gateway(tmp_path, 16 * 1024**2, {"files.example"}).download(item, "model")
    assert not list(tmp_path.rglob("artifact"))


def test_zip_escape_is_rejected(tmp_path):
    artifact = tmp_path / "artifact"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("../escape.exe", "evil")
    with pytest.raises(ValueError):
        Gateway(tmp_path, 16 * 1024**2).extract_engine(artifact)
    assert not (tmp_path.parent / "escape.exe").exists()


def test_engine_chooses_binary_release_not_source_only(monkeypatch):
    from selfstruct.resources import Catalog
    releases = [
        {"tag_name":"source-only","assets":[]},
        {"tag_name":"binary-build","assets":[{"name":"llama-b123-bin-win-cpu-x64.zip", "digest":"sha256:"+'a'*64,
          "size":123, "browser_download_url":"https://github.com/ggml-org/llama.cpp/releases/download/b123/engine.zip"}]}
    ]
    monkeypatch.setattr('selfstruct.resources.fetch_json', lambda url: releases)
    item = Catalog.resolve_engine('nt')
    assert item['version'] == 'binary-build' and item['sha256'] == 'a'*64
    releases[1]['assets'][0]['digest'] = None
    with pytest.raises(ValueError):
        Catalog.resolve_engine('nt')


@pytest.mark.skipif(__import__('os').name == 'nt', reason='Linux engine archive')
def test_tar_library_links_materialized_without_escape(tmp_path):
    import tarfile
    artifact = tmp_path/'artifact'
    with tarfile.open(artifact,'w:gz') as archive:
        for name, content in [('build/llama-server',b'engine'),('build/libcompute.so.1',b'library')]:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member,io.BytesIO(content))
        link = tarfile.TarInfo('build/libcompute.so')
        link.type = tarfile.SYMTYPE
        link.linkname = 'libcompute.so.1'
        archive.addfile(link)
    server = Gateway(tmp_path,16*1024**2).extract_engine(artifact)
    lib = server.parent/'libcompute.so'
    assert lib.read_bytes() == b'library' and not lib.is_symlink()
    # A link outside the extraction root must never be materialized.
    with tarfile.open(artifact,'w:gz') as archive:
        link = tarfile.TarInfo('escape')
        link.type = tarfile.SYMTYPE
        link.linkname = '/etc/passwd'
        archive.addfile(link)
    (tmp_path/'expanded-files.json').unlink()
    with pytest.raises(ValueError):
        Gateway(tmp_path,16*1024**2).extract_engine(artifact)
