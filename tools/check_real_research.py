"""Opt-in integration check using an already acquired local model, no game labels."""
import argparse
import json
from pathlib import Path
import time

from selfstruct.config import Config
from selfstruct.engine import Controller
from selfstruct.resources import ManagedLLM
from selfstruct.smoke import SyntheticGame


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model-root',type=Path,required=True)
    parser.add_argument('--experiment-root',type=Path,required=True)
    parser.add_argument('--report',type=Path)
    args = parser.parse_args()
    manager = ManagedLLM(args.model_root,4*1024**3)
    controller = None
    try:
        if not manager.start():
            raise RuntimeError('A managed model must already be installed')
        controller = Controller(Config(root=str(args.experiment_root),keys=['A','B'],research_every=48,
                                       quota_bytes=1024**3,restart_after=1000000),SyntheticGame())
        controller.start()
        deadline=time.monotonic()+660
        proposals=0
        terminal=False
        while time.monotonic()<deadline:
            with controller.store.lock:
                jobs=controller.store.db.execute('SELECT state,payload FROM jobs').fetchall()
                proposals=controller.store.db.execute('SELECT count(*) FROM proposals').fetchone()[0]
            if jobs:
                # Let the real supervisor own the job; do not issue a competing manual call.
                controller.scientist.every=1000000
                payload=json.loads(jobs[0]['payload'])
                terminal=jobs[0]['state']=='complete' or (jobs[0]['state']=='pending' and bool(payload.get('repair_error')) and bool(controller.store.reports()))
                if terminal and not controller.scientist.live:
                    break
            time.sleep(.05)
        assert proposals>=1 and terminal, 'Real research did not complete within its budget'
        with controller.store.lock:
            response=controller.store.db.execute('SELECT response FROM proposals ORDER BY id DESC LIMIT 1').fetchone()['response']
        proposal=json.loads(response)
        from selfstruct.research import parse_proposal
        with controller.store.lock:
            evidence={r['id'] for r in controller.store.db.execute('SELECT id FROM events')}
        parse_proposal(response,evidence,['A','B'])
        if args.report:
            data=json.loads(args.report.read_text(encoding='utf-8'))
            data['checks'].append('real research prompt completed with structured proposal')
            data['research']={'status':'passed','synthetic_only':True,'valid_structured_proposal':True,'adoption_required':False,'job_preserved':True,'job_outcome':payload.get('outcome','evaluated'),'adoption_verified':False}
            from selfstruct.config import write_json
            write_json(args.report,data)
        print('PASS: real model research response archived; observations:',len(controller.store.knowledge_rows()),
              'evaluation reports:',len(controller.store.reports()),flush=True)
        print('Synthetic evidence only; this does not validate unknown-game learning.',flush=True)
    finally:
        if controller:
            controller.close()
        manager.stop()


if __name__=='__main__':
    main()
