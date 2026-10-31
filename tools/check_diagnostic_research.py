"""Offline regression check of a curated diagnostic ZIP; never executes attached code."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import zipfile

from PIL import Image
from selfstruct.vision import transform,describe


def check(path):
    with zipfile.ZipFile(path) as z,tempfile.TemporaryDirectory() as temporary:
        if sum(i.file_size for i in z.infolist())>64*1024**2:raise ValueError('Diagnostic input exceeds inspection budget')
        info=json.loads(z.read('diagnosis.json'))
        dbpath=Path(temporary)/'diagnostics.sqlite3';dbpath.write_bytes(z.read('diagnostics.sqlite3'))
        db=sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
        try:
            before=hashlib.sha256(dbpath.read_bytes()).hexdigest()
            jobs=[{**dict(r),'payload':json.loads(r['payload'])} for r in db.execute('SELECT * FROM jobs ORDER BY updated DESC')]
            events=[dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC')]
            images={}
            for name in z.namelist():
                if not re.fullmatch(r'evidence/[a-f0-9]{64}\.png',name):continue
                raw=z.read(name);assert hashlib.sha256(raw).hexdigest()==Path(name).stem
                with Image.open(io.BytesIO(raw)) as im:
                    image=im.copy()
                # Actual original pixels through the current region/analysis pipeline.
                w,h=image.size;region=[w//2,0,w,max(1,h//2)]
                for output in ('pixels','statistics','histogram','components'):
                    converted,box=transform(image,{'box':region,'size':[64,32],'mode':'RGB','operations':[{'op':'edge'}]})
                    result=describe(converted,{'output':output,'limit':64})
                    assert result and box==region
                images[Path(name).stem]=image.size
            pairs=sum(r.get('before_hash') in images and r.get('after_hash') in images for r in events if r['state']=='complete')
            finite=[j for j in jobs if j['payload'].get('outcome')=='repair_exhausted']
            assert all(j['state']=='complete' and j['payload'].get('repair_failures',0)<=3 for j in finite)
            activity=info.get('activity',{})
            pause_consistent=not str(info.get('gui',{}).get('state','')).startswith('一時停止中') or activity.get('phase')=='一時停止中'
            assert pause_consistent
            assert hashlib.sha256(dbpath.read_bytes()).hexdigest()==before
            return {'source_version':info.get('version'),'source_sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                    'read_only':True,'sampled_jobs':len(jobs),'terminal_jobs':sum(j['state']=='complete' for j in jobs),
                    'finite_repair_jobs':len(finite),'original_images_verified':len(images),'before_after_pairs_available':pairs,
                    'region_rgb_edge_pixels_statistics_histogram_components':'passed','pause_consistent':pause_consistent,
                    'limitations':'Recorded diagnostic replay only; no live game or new model inference. Curated diagnostics are not full individual backups.'}
        finally:db.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('zip',type=Path);parser.add_argument('--report',type=Path)
    args=parser.parse_args();result=check(args.zip)
    if args.report:args.report.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(result,indent=2,ensure_ascii=False))


if __name__=='__main__':main()
