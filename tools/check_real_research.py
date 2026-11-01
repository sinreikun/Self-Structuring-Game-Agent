"""Opt-in integration check using an already acquired local model, no game labels."""
import argparse
import json
from pathlib import Path
import time

from selfstruct.config import Config
from selfstruct.engine import Controller
from selfstruct.resources import ManagedLLM
from selfstruct.smoke import SyntheticGame


def protocol_probe(controller):
    """Directed real-model wire-format test, not a claim of autonomous tool choice."""
    from copy import deepcopy
    from selfstruct.research import Scientist,LocalLLM
    client=LocalLLM(controller.config.llm_url,controller.config.llm_model)
    client.context=controller.config.llm_context
    scientist=Scientist(controller.store,controller.revisions,client,controller.config.keys,1000000,lambda _:None)
    scientist.visual=controller.visual
    rows=controller.store.rows(16);calls=[]
    complete=scientist.complete
    def directed(prompt,**options):
        schema=deepcopy(options['schema']);schema['required']=list(dict.fromkeys([*schema['required'],'actions']))
        schema['properties']['wat']={'type':'string','enum':['']}
        schema['properties']['findings']['maxItems']=0
        step=len(calls)
        if step<3:
            op=('memory_search','memory_read','observe')[step]
            variant=next(v for v in schema['properties']['actions']['items']['anyOf'] if v['properties']['op']['enum']==[op])
            if step==0:
                values={'collection':'experience','ref':str(rows[-1]['id'])}
            elif step==1:
                record=json.loads(controller.store.get_job(jid)['payload'])['memory_results'][-1]['result']['records'][0]
                values={'collection':'experience','ref':record['ref'],'revision':record['revision'],'limit':256}
            else:values={'event':rows[-1]['id'],'phase':'before','size':[16,16],'mode':'RGB','output':'pixels','limit':64}
            variant['properties']={k:v for k,v in variant['properties'].items() if k in ('op',*values)}
            for key,value in values.items():variant['properties'][key]={'enum':[value],**({'type':'integer'} if type(value)==int else {'type':'string'} if isinstance(value,str) else {})}
            variant['required']=['op',*values]
            schema['properties']['actions'].update(items=variant,minItems=1,maxItems=1)
            prompt+='\nこれは取得式メモリと観測APIの接続検証です。指定schemaどおり'+op+'を1件だけ要求してください。WATは不要です。'
        else:
            schema['properties']['actions'].update(minItems=0,maxItems=0)
            prompt+='\n接続検証の取得結果を受け取りました。追加要求はせず、この結果で分かることを短く返してください。WATは不要です。'
        options['schema']=schema;calls.append(True)
        return complete(prompt,**options)
    scientist.complete=directed
    with controller.store.lock:
        jid=controller.store.job('protocol_probe',{'redirected':True})
        controller.store.set_job(jid,'running')
    scientist.study(rows,jid)
    row=controller.store.get_job(jid);payload=json.loads(row['payload'])
    done=[a for a in payload.get('action_history',[]) if a['state']=='done' and a.get('result',{}).get('evidence')]
    assert row['state']=='complete' and done and len(calls)>=4, 'Real model typed-action/result round trip failed'
    assert any(s.get('consumed_actions') for s in payload['research_steps'][1:]), 'Real model did not consume tool results in same Job'
    assert payload['progress']['memory_reads']==1 and payload['progress']['memory_searches']==1
    return {'memory_reads':1,'memory_searches':1,'status':'passed','directed':True,'autonomous_choice_verified':False,'same_job':True,'research_calls':len(calls),'executed_observations':len(done)}


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
                terminal=jobs[0]['state']=='complete'
                if terminal and not controller.scientist.live:
                    break
            time.sleep(.05)
        assert proposals>=1 and terminal, 'Real research did not complete within its budget'
        observed=[entry for entry in payload.get('visual_history',[]) if entry.get('result',{}).get('evidence')]
        assert payload.get('memory_mode')=='pull' and not payload.get('initial_visual_evidence'), 'Research must start from an index without automatic image injection'
        with controller.store.lock:
            response=controller.store.db.execute('SELECT response FROM proposals ORDER BY id DESC LIMIT 1').fetchone()['response']
        proposal=json.loads(response)
        from selfstruct.research import parse_proposal
        with controller.store.lock:
            evidence={r['id'] for r in controller.store.db.execute('SELECT id FROM events')}
        parse_proposal(response,evidence,['A','B'])
        probe=protocol_probe(controller)
        if args.report:
            data=json.loads(args.report.read_text(encoding='utf-8'))
            data['checks'].append('real research prompt completed with structured proposal')
            data['directed_protocol_probe']=probe
            data['research']={'status':'passed','synthetic_only':True,'valid_structured_proposal':True,'adoption_required':False,'job_preserved':True,'memory_mode':'pull','initial_visual_evidence':False,'visual_observations':len(observed),'repair_failures':payload.get('repair_failures',0),'job_outcome':payload.get('outcome','evaluated'),'adoption_verified':False,'research_calls':payload.get('research_calls',0),'research_progress':payload.get('progress',{}),'executed_actions':sum(e.get('state')=='done' for e in payload.get('action_history',[])),'stalled':payload.get('stalled',False),'limit':payload.get('research_limit'),'same_job_reinference':any(e.get('consumed_actions') for e in payload.get('research_steps',[]))}
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
