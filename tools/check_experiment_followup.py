"""One real-model follow-up decision after a host-prepared real experiment.

Controlled setup, not autonomous experiment planning or full-loop validation.
The production prompt/schema are unchanged; no tool or result ID is forced.
"""
import argparse
import json
from pathlib import Path
import threading
import time

from selfstruct.config import Config,write_json
from selfstruct.engine import Controller
from selfstruct.resources import ManagedLLM
from selfstruct.research_actions import run_actions
from tools.check_autonomous_research import BlindEnvironment


class Captured(Exception):pass


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model-root',type=Path,required=True)
    p.add_argument('--state',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.output.exists():raise ValueError('Use a fresh output directory')
    args.output.mkdir(parents=True)
    manager=ManagedLLM(args.model_root,24*1024**3);c=None
    try:
        state=json.loads(args.state.read_text());manager.start(state)
        game=BlindEnvironment(271828)
        c=Controller(Config(root=str(args.output/'data'),keys=['A','B'],research_every=1000000),game)
        c.scientist.client.health()
        game.start();c.step();seed=c.last_eid
        jid=c.store.job('experiment_followup_probe',{})
        kid=c.store.knowledge('hypothesis','許可入力Bの後の状態を調べる。目的への進展は未確認。','inferred','hypothesis',[seed])
        payload={'memory_mode':'pull','research_rows':c.store.rows(1),'hypotheses':{'setup':kid},'research_calls':0}
        run_actions(c.scientist,jid,payload,[{'op':'observe','event':seed,'phase':'after','size':[320,144],'mode':'RGB','output':'image'}])
        payload['research_calls']=1;errors=[]
        def prepare():
            try:run_actions(c.scientist,jid,payload,[{'op':'experiment','hypothesis':'setup','action':1,'duration':10,'wait':10,'repeat':1}])
            except Exception as exc:errors.append(str(exc))
        worker=threading.Thread(target=prepare);worker.start();deadline=time.monotonic()+5
        while worker.is_alive() and time.monotonic()<deadline:
            with c.store.lock:pending=c.store.db.execute("SELECT 1 FROM research_experiments WHERE state IN ('queued','running')").fetchone()
            if pending:c.step()
            else:time.sleep(.01)
        worker.join(1)
        if worker.is_alive() or errors:raise RuntimeError('Setup did not complete: '+str(errors))
        event=payload['action_history'][-1]['result']['events'][0]['id']
        payload['research_started']=time.time();payload['research_phase']='evaluation'
        c.store.set_job(jid,'running',payload)
        real=c.scientist.complete;response={}
        def capture(prompt,**options):
            (args.output/'prompt.txt').write_text(prompt,encoding='utf-8')
            value=real(prompt,**options)
            (args.output/'response.txt').write_text(value,encoding='utf-8')
            response.update(json.loads(value));raise Captured()
        c.scientist.complete=capture
        error=None
        try:c.scientist.study(c.store.rows(1),jid)
        except Captured:pass
        except Exception as exc:error=type(exc).__name__+': '+str(exc)
        actions=response.get('actions',[])+response.get('visual_requests',[])
        def selects(action):
            return action.get('op','observe') in ('observe','compare','analyze','analyzer_run') and (
                action.get('event')==event or (action.get('compare') or {}).get('event')==event)
        result={'status':'selected_result_reference' if any(map(selects,actions)) else 'inconclusive' if error else 'no_result_observation_selected',
                'scripted_setup':True,'autonomous_full_loop_verified':False,'real_model_calls':1,
                'model_sha256':state['model_sha256'],'projector_sha256':state.get('projector_sha256'),
                'old_image_event':seed,'completed_experiment_event':event,'response':response,'error':error,
                'completion':c.scientist.client.last_completion,
                'limitation':'One decision from a controlled prepared state; no claim of game understanding or goal achievement.'}
        write_json(args.output/'result.json',result);print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
        return 0 if result['status']=='selected_result_reference' else 1
    finally:
        if c:c.close()
        manager.stop()


if __name__=='__main__':raise SystemExit(main())
