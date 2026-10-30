"""Opt-in integration check using an already acquired local model, no game labels."""
import argparse
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
    args = parser.parse_args()
    manager = ManagedLLM(args.model_root,4*1024**3)
    controller = None
    try:
        if not manager.start():
            raise RuntimeError('A managed model must already be installed')
        controller = Controller(Config(root=str(args.experiment_root),keys=['A','B'],research_every=48,
                                       quota_bytes=1024**3,restart_after=1000000),SyntheticGame())
        controller.start()
        deadline=time.monotonic()+180
        proposals=0
        terminal=False
        while time.monotonic()<deadline:
            with controller.store.lock:
                jobs=controller.store.db.execute('SELECT state FROM jobs').fetchall()
                proposals=controller.store.db.execute('SELECT count(*) FROM proposals').fetchone()[0]
            if jobs:
                # Let the real supervisor own the job; do not issue a competing manual call.
                controller.scientist.every=1000000
                terminal=jobs[0]['state'] in {'complete','failed'}
                if terminal and not controller.scientist.live:
                    break
            time.sleep(.05)
        assert proposals>=1 and terminal, 'Real research did not complete within its budget'
        print('PASS: real model research response archived; observations:',len(controller.store.knowledge_rows()),
              'evaluation reports:',len(controller.store.reports()),flush=True)
        print('Synthetic evidence only; this does not validate unknown-game learning.',flush=True)
    finally:
        if controller:
            controller.close()
        manager.stop()


if __name__=='__main__':
    main()
