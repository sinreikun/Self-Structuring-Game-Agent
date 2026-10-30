"""Release-only real model acquisition check; normal tests remain offline."""
import argparse
import json
from pathlib import Path
import threading

from selfstruct.config import write_json
from selfstruct.research import LocalLLM
from selfstruct.resources import Catalog, ManagedLLM


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    args = parser.parse_args()
    (args.root/'host').mkdir(parents=True,exist_ok=True)
    catalog = Catalog(args.root/'host'/'catalog.json')
    selected = next(model for model in catalog.data['models'] if model['id']=='light')
    manager = ManagedLLM(args.root,4*1024**3)
    progress_bucket = -1
    def progress(done,total):
        nonlocal progress_bucket
        bucket = int(done*10/total)
        if bucket!=progress_bucket:
            print(f'Acquisition progress: {done*100/total:.0f}%',flush=True)
            progress_bucket=bucket
    try:
        manager.install(selected,progress,threading.Event())
        result = json.loads(LocalLLM('http://127.0.0.1:8766/v1','local').complete(
            'JSONだけを返してください。statusというキーの値はreadyとしてください。',max_tokens=64,json_mode=True))
        if result.get('status')!='ready':
            raise AssertionError('Managed model did not return the expected structured response')
        write_json(args.report,{'status':'passed','model_id':selected['id'],
                               'checks':['verified acquisition','engine startup','real inference','structured response']})
        print('PASS: managed engine/model installation and real local inference',flush=True)
    finally:
        manager.stop()


if __name__=='__main__':
    main()
