"""Publish only an existing, successful same-repository CI artifact.

Uses GitHub's repository-scoped workflow token. Never edits published assets.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile

ASSETS = ('SelfStruct.exe','SHA256SUMS.txt','THIRD_PARTY_NOTICES.txt','build-info.json',
          'window-validation.json','inference-validation.json')


def gh(*args: str) -> str:
    return subprocess.check_output(['gh',*args],text=True,encoding='utf-8')


def api(path: str):
    return json.loads(gh('api',path))


def verify_artifacts(folder: Path, commit: str) -> dict[str,str]:
    hashes={}
    for name in ASSETS:
        path=folder/name
        if not path.is_file() or path.is_symlink():
            raise ValueError('Missing or unsafe release asset: '+name)
        with path.open('rb') as stream:
            hashes[name]=hashlib.file_digest(stream,'sha256').hexdigest()
    info=json.loads((folder/'build-info.json').read_text(encoding='utf-8'))
    expected=(folder/'SHA256SUMS.txt').read_text(encoding='utf-8').split()[0]
    if not (info['platform']=='win32' and info['commit']==commit and info['sha256']==expected==hashes['SelfStruct.exe']):
        raise ValueError('Artifact identity or digest does not match verified CI')
    with (folder/'SelfStruct.exe').open('rb') as stream:
        header=stream.read(64)
        if header[:2]!=b'MZ':raise ValueError('Not a Windows executable')
        stream.seek(struct.unpack_from('<I',header,0x3c)[0])
        pe=stream.read(6)
        if pe[:4]!=b'PE\0\0' or struct.unpack_from('<H',pe,4)[0]!=0x8664:
            raise ValueError('Not a Windows x64 executable')
    for name in ('window-validation.json','inference-validation.json'):
        if json.loads((folder/name).read_text(encoding='utf-8')).get('status')!='passed':
            raise ValueError('Required validation did not pass')
    return hashes


def main():
    request=json.loads(Path('.github/release-request.json').read_text(encoding='utf-8'))
    tag,run_id=request['tag'],request['run_id']
    if not re.fullmatch(r'v\d+\.\d+\.\d+-dev\.\d+',tag) or not isinstance(run_id,int) or run_id<=0:
        raise ValueError('Invalid development release request')
    repository=os.environ['GITHUB_REPOSITORY']
    prefix=f'repos/{repository}'
    run=api(f'{prefix}/actions/runs/{run_id}')
    if run['conclusion']!='success' or run['name']!='Test and Windows EXE' or run['head_repository']['full_name']!=repository:
        raise ValueError('Required same-repository CI did not pass')
    obj=api(f'{prefix}/git/ref/tags/{tag}')['object']
    for _ in range(4):
        if obj['type']=='commit':break
        if obj['type']!='tag':raise ValueError('Invalid tag object')
        obj=api(f"{prefix}/git/tags/{obj['sha']}")['object']
    if obj['type']!='commit' or obj['sha']!=run['head_sha']:
        raise ValueError('Release tag differs from verified CI commit')
    notes=Path('docs/releases')/(tag+'.md')
    if not notes.is_file():raise ValueError('Reviewed release notes are missing')
    with tempfile.TemporaryDirectory(prefix='selfstruct-release-') as temp:
        folder=Path(temp)
        gh('run','download',str(run_id),'--repo',repository,'--name','SelfStruct-Windows-x64','--dir',str(folder))
        hashes=verify_artifacts(folder,obj['sha'])
        releases=api(f'{prefix}/releases?per_page=100')
        release=next((item for item in releases if item['tag_name']==tag),None)
        if release and not release['draft']:
            existing={item['name']:item.get('digest') for item in release['assets']}
            if any(existing.get(name)!='sha256:'+digest for name,digest in hashes.items()):
                raise ValueError('Published release exists with different assets; refusing changes')
            print('Verified existing published release:',release['html_url'])
            return
        if release is None:
            gh('release','create',tag,'--repo',repository,'--verify-tag','--draft','--prerelease',
               '--latest=false','--title',f'SelfStruct {tag} — 背面動作対応の開発版','--notes-file',str(notes))
        current=json.loads(gh('release','view',tag,'--repo',repository,'--json','isDraft'))
        if not current['isDraft']:raise ValueError('Release became public before upload; refusing changes')
        gh('release','upload',tag,*[str(folder/name) for name in ASSETS],'--repo',repository,'--clobber')
        uploaded=api(f'{prefix}/releases?per_page=100')
        release=next(item for item in uploaded if item['tag_name']==tag)
        existing={item['name']:item.get('digest') for item in release['assets']}
        if any(existing.get(name)!='sha256:'+digest for name,digest in hashes.items()):
            raise ValueError('Uploaded asset hashes did not match')
        gh('release','edit',tag,'--repo',repository,'--draft=false','--prerelease','--latest=false','--notes-file',str(notes))
        print('Published verified development release:',f'https://github.com/{repository}/releases/tag/{tag}')


if __name__=='__main__':
    main()
