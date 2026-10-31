"""Reuse a pinned launcher for the current trust anchor, or build its first release."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from urllib.request import urlopen

from selfstruct.release import LAUNCHER_VERSION, UPDATE_KEY


def main():
    pin_path=Path('updates/launcher.json')
    pin=json.loads(pin_path.read_text(encoding='utf-8')) if pin_path.is_file() else {}
    if pin.get('version')!=LAUNCHER_VERSION or pin.get('public_key')!=UPDATE_KEY:
        subprocess.run([sys.executable,'tools/build_launcher.py'],check=True)
        return
    digest=pin.get('sha256','');tag=pin.get('tag','')
    import re
    if not re.fullmatch('[0-9a-f]{64}',digest) or not re.fullmatch(r'v\d+\.\d+\.\d+-dev\.\d+',tag):
        raise ValueError('Invalid pinned launcher identity')
    url=f'https://github.com/sinreikun/Self-Structuring-Game-Agent/releases/download/{tag}/SelfStructLauncher.exe'
    folder=Path('dist');folder.mkdir(exist_ok=True)
    with urlopen(url,timeout=60) as response:payload=response.read(32*1024**2+1)
    if len(payload)>32*1024**2 or hashlib.sha256(payload).hexdigest()!=digest:
        raise ValueError('Published fixed launcher digest mismatch')
    (folder/'SelfStructLauncher.exe').write_bytes(payload)
    path=folder/'build-info.json';info=json.loads(path.read_text(encoding='utf-8'))
    info.update(launcher_sha256=digest,launcher_version=LAUNCHER_VERSION,launcher_public_key=UPDATE_KEY,launcher_source=tag)
    path.write_text(json.dumps(info,indent=2),encoding='utf-8')


if __name__=='__main__':main()
