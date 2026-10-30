"""Build only on Windows when launcher protocol/security changes."""
import os
from pathlib import Path
import subprocess
import sys

root=Path(__file__).resolve().parents[1]
os.chdir(root)
diagnostic='--diagnostic' in sys.argv
name='SelfStructLauncherDiagnostic' if diagnostic else 'SelfStructLauncher'
command=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onefile',
    '--name',name,'--paths','src','stable_launcher.py']
if not diagnostic:command.insert(-1,'--windowed')
subprocess.run(command,check=True)
if diagnostic:sys.exit(0)
import hashlib,json
info=root/'dist'/'build-info.json'
metadata=json.loads(info.read_text(encoding='utf-8'))
metadata['launcher_sha256']=hashlib.sha256((root/'dist'/'SelfStructLauncher.exe').read_bytes()).hexdigest()
info.write_text(json.dumps(metadata,indent=2),encoding='utf-8')
