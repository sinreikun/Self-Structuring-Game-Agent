"""Build on the target OS; Windows produces one downloadable EXE."""
from importlib import metadata
import os
from pathlib import Path
import subprocess
import sys
import wasmtime
import hashlib
import json
import shutil
import sysconfig

root = Path(__file__).resolve().parents[1]
os.chdir(root)
folder = root / "build"
folder.mkdir(exist_ok=True)
notices = []
runtime_licenses = [Path(sys.base_prefix)/'LICENSE.txt',Path(sysconfig.get_path('stdlib'))/'LICENSE.txt',
                    Path('/usr/share/doc')/f'python{sys.version_info.major}.{sys.version_info.minor}'/'copyright']
runtime_licenses.extend((Path(sys.base_prefix)/'tcl').rglob('license.terms'))
seen = set()
for license_path in runtime_licenses:
    if license_path.is_file():
        text = license_path.read_text(encoding='utf-8',errors='replace')
        if text not in seen:
            notices.append(f'\nRuntime license: {license_path.name}\n'+text)
            seen.add(text)
for name in ("Pillow", "wasmtime", "cryptography", "cffi", "importlib_resources", "pycparser", "pyinstaller"):
    distribution = metadata.distribution(name)
    notices.append(f"\n{'='*60}\n{name} {distribution.version}\n")
    for file in distribution.files or []:
        if "license" in file.name.lower() or "copying" in file.name.lower():
            path = distribution.locate_file(file)
            if path.is_file():
                notices.append(path.read_text(encoding="utf-8", errors="replace"))
notice = folder / "THIRD_PARTY_NOTICES.txt"
notice.write_text("\n".join(notices), encoding="utf-8")
command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--name", "SelfStruct",
           "--paths", "src", "--collect-all", "wasmtime", "--collect-all", "selfstruct",
           "--add-data", f"{notice}{os.pathsep}selfstruct/assets", "launcher.py"]
# Wasmtime loads a platform-specific library by relative path through ctypes.
# Explicitly preserve that directory; generic PyInstaller collection flattens it.
package = Path(wasmtime.__file__).parent
for library in package.glob("*/*"):
    if library.suffix in {".so", ".dll", ".dylib"}:
        command[-1:-1] = ["--add-binary", f"{library}{os.pathsep}wasmtime/{library.parent.name}"]
if os.name == "nt":
    command.insert(-1, "--windowed")
subprocess.run(command, check=True)
dist = root / 'dist'
shutil.copyfile(notice, dist / 'THIRD_PARTY_NOTICES.txt')
binary = dist / ('SelfStruct.exe' if os.name == 'nt' else 'SelfStruct')
with binary.open('rb') as stream:
    digest = hashlib.file_digest(stream,'sha256').hexdigest()
try:
    commit = subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
except (OSError,subprocess.CalledProcessError):
    commit = None
(dist/'build-info.json').write_text(json.dumps({'version':metadata.version('selfstruct'),'commit':commit,
    'platform':sys.platform,'python':sys.version.split()[0],'sha256':digest},indent=2),encoding='utf-8')
(dist/'SHA256SUMS.txt').write_text(f'{digest}  {binary.name}\n',encoding='utf-8')
