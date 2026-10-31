"""An unsigned preparation path must never make a release public."""
import json
from pathlib import Path

import pytest

from tools import publish_release as publisher


@pytest.fixture
def release_world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('GITHUB_REPOSITORY', 'test/repository')
    Path('.github').mkdir(); Path('docs/releases').mkdir(parents=True)
    Path('docs/releases/v0.5.0-dev.1.md').write_text('Verified notes')
    calls = []
    release = {'tag_name': 'v0.5.0-dev.1', 'draft': True, 'html_url': 'https://github.com/test/repository/releases/1',
               'assets': [{'name': 'SelfStruct.exe', 'digest': 'sha256:verified'}]}
    def api(path):
        if '/actions/runs/' in path:
            return {'conclusion': 'success', 'name': 'Test and Windows EXE', 'head_sha': 'f'*40,
                    'head_repository': {'full_name': 'test/repository'}}
        if '/git/ref/tags/' in path:
            return {'object': {'type': 'commit', 'sha': 'f'*40}}
        if '/releases?' in path:
            return [release]
        raise AssertionError(path)
    def gh(*args):
        calls.append(args)
        if args[:2] == ('run', 'download'):
            folder = Path(args[args.index('--dir')+1])
            (folder/'SelfStruct.exe').write_bytes(b'verified-test-body')
            (folder/'inference-validation.json').write_text('{"status":"passed"}')
        if args[:2] == ('release', 'view'):
            return '{"isDraft":true}'
        return ''
    monkeypatch.setattr(publisher, 'api', api)
    monkeypatch.setattr(publisher, 'gh', gh)
    monkeypatch.setattr(publisher, 'ASSETS', ('SelfStruct.exe',))
    monkeypatch.setattr(publisher, 'verify_artifacts', lambda folder, commit, allow_reuse: {'SelfStruct.exe': 'verified'})
    def request(**values):
        Path('.github/release-request.json').write_text(json.dumps({'tag': 'v0.5.0-dev.1', 'run_id': 123, **values}))
    return calls, release, request


def test_preparation_uploads_verified_artifacts_and_never_publishes_without_signatures(release_world):
    calls, _, request = release_world
    request(draft_only=True)
    publisher.main()
    assert any(call[:2] == ('release', 'upload') for call in calls)
    assert not any(call[:2] == ('release', 'edit') for call in calls)


def test_normal_publication_still_requires_signed_manifests_before_upload(release_world):
    calls, _, request = release_world
    request()
    with pytest.raises(FileNotFoundError): publisher.main()
    assert not any(call[:2] == ('release', 'upload') for call in calls)
    assert not any(call[:2] == ('release', 'edit') for call in calls)


def test_preparation_cannot_modify_an_already_published_release(release_world):
    calls, release, request = release_world
    release['draft'] = False
    request(draft_only=True)
    with pytest.raises(ValueError, match='published'): publisher.main()
    assert not any(call[:2] in {('release', 'upload'), ('release', 'edit')} for call in calls)


def test_preparation_flag_must_be_an_explicit_boolean(release_world):
    calls, _, request = release_world
    request(draft_only='true')
    with pytest.raises(ValueError, match='flag'): publisher.main()
    assert calls == []
