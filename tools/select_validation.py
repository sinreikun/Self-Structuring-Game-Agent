"""Honor the requested focused checks; fall back to full checks for other code changes."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from tools.inference_identity import component_hashes

def git_read(ref,name):
    return subprocess.check_output(['git','show',f'{ref}:{name}'])

def scope():
    path=Path('.github/validation-scope.json')
    if not path.exists():return 'full'
    spec=json.loads(path.read_text(encoding='utf-8'))
    if spec.get('scope')!='target-process':return 'full'
    changed=subprocess.check_output(['git','diff','--name-only',spec['baseline'],'HEAD'],text=True).splitlines()
    permitted={'src/selfstruct/windows.py','src/selfstruct/processes.py','src/selfstruct/process_picker.py',
               'src/selfstruct/config.py','src/selfstruct/engine.py','src/selfstruct/gui.py',
               'src/selfstruct/smoke.py','src/selfstruct/__main__.py'}
    if any(name.startswith('src/') and name not in permitted for name in changed):return 'full'
    if component_hashes(lambda name:Path(name).read_bytes())!=component_hashes(lambda name:git_read(spec['baseline'],name)):
        return 'full'
    return 'target-process'

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--reuse-report',type=Path)
    args=parser.parse_args();selected=scope()
    if args.reuse_report:
        if selected!='target-process':raise ValueError('Inference implementation changed; a new inference test is required')
        spec=json.loads(Path('.github/validation-scope.json').read_text(encoding='utf-8'))
        args.reuse_report.parent.mkdir(parents=True,exist_ok=True)
        args.reuse_report.write_text(json.dumps({'status':'reused','baseline_tag':spec['baseline'],
            'reason':'Process selection only; unrelated inference test was not rerun.',
            'component_hashes':component_hashes(lambda name:Path(name).read_bytes())},indent=2),encoding='utf-8')
    else:
        print(selected)
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'],'a',encoding='utf-8') as output:output.write('scope='+selected+'\n')

if __name__=='__main__':main()
