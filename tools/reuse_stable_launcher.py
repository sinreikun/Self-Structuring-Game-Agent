"""Reuse the published fixed protocol-1 launcher; body updates do not replace it."""
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

URL='https://github.com/sinreikun/Self-Structuring-Game-Agent/releases/download/v0.2.0-dev.1/SelfStructLauncher.exe'
SHA256='524bb60c78cdb213c368400e35e5350fae90130f439f0e4d576eb12088e38e06'


def main():
    folder=Path('dist');folder.mkdir(exist_ok=True)
    with urlopen(URL,timeout=60) as response:payload=response.read(32*1024**2+1)
    if len(payload)>32*1024**2 or hashlib.sha256(payload).hexdigest()!=SHA256:
        raise ValueError('Published fixed launcher digest mismatch')
    (folder/'SelfStructLauncher.exe').write_bytes(payload)
    path=folder/'build-info.json';info=json.loads(path.read_text(encoding='utf-8'))
    info['launcher_sha256']=SHA256;info['launcher_version']='1.0.0';info['launcher_source']='v0.2.0-dev.1'
    path.write_text(json.dumps(info,indent=2),encoding='utf-8')


if __name__=='__main__':main()
