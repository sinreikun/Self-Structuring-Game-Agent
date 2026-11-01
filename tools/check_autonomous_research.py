"""Opt-in blind real-model experiment. Never supplies a solution or forces tools.

Oracle state is recorded only after research finishes. A behavioral success is
necessary, not sufficient, for semantic understanding; human review is mandatory.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import platform
import subprocess
import threading
import time

from PIL import Image, ImageDraw, ImageFont
from selfstruct.config import Config, write_json
from selfstruct.engine import Controller
from selfstruct.resources import ManagedLLM
from selfstruct.smoke import SyntheticGame


class BlindEnvironment(SyntheticGame):
    """Private state; only capture/act reach the production Controller."""
    def __init__(self, seed):
        super().__init__()
        rng = random.Random(seed)
        self.direction = rng.choice((-1, 1))
        self.position, self.target = rng.choice(((1, 5), (5, 1)))
        self.won = False
        self.transitions = []

    def capture(self):
        with self.lock:
            image = Image.new('RGB', (320, 144), (12, 12, 12))
            draw = ImageDraw.Draw(image)
            font = ImageFont.load_default(size=14)
            draw.text((8, 6), 'MOVE WHITE BOX INTO GREEN FRAME', font=font, fill='white')
            draw.rectangle((10, 45, 309, 115), outline=(80, 80, 80))
            x = 20 + 40 * self.target
            draw.rectangle((x, 65, x+26, 97), outline=(0, 255, 0), width=3)
            x = 20 + 40 * self.position
            draw.rectangle((x+6, 73, x+20, 89), fill='white')
            if self.won:
                draw.rectangle((0, 118, 319, 143), fill=(0, 160, 0))
                draw.text((90, 122), 'COMPLETE', font=font, fill='white')
            return image

    def act(self, key, duration, cancel, on_sent=None):
        if cancel.is_set():return
        if key not in ('A', 'B'):raise ValueError('Input outside allowlist')
        if on_sent:on_sent(time.time())
        with self.lock:
            before = self.position
            if not self.won:
                self.position = max(0, min(6, self.position + self.direction * (1 if key == 'A' else -1)))
                self.won = self.position == self.target
            self.transitions.append({'key':key, 'before':before, 'after':self.position, 'won':self.won})
        self.release()


def assess(payload, won, proposals, reports, error=None):
    """Evidence gates, deliberately unable to declare semantic success."""
    actions = payload.get('action_history', [])
    visual = [a for a in actions if a.get('state') == 'done' and a.get('request', {}).get('op') in ('observe', 'compare', 'analyze')]
    spatial = any(set(v.get('payload_fields', [])) & {'pixels','gray','components'} or v.get('image_attached')
                  for s in payload.get('research_steps', []) for v in s.get('delivered_visual_evidence', []))
    experiments = [a for a in actions if a.get('state') == 'done' and a.get('request', {}).get('op') == 'experiment' and a.get('result', {}).get('completed', 0)]
    feedback = any(a.get('action_id') in s.get('consumed_actions', []) and s.get('step', 0) > a.get('step', 0)
                   for a in experiments for s in payload.get('research_steps', []))
    # Only a candidate explanation: text must be independently reviewed for truth.
    after = min((a['step'] for a in experiments), default=10**9)
    explanation = any(step > after and value.get('findings') for step, value in proposals)
    gates = {'observation_requested':bool(visual), 'spatial_evidence_delivered':spatial,
             'hypothesis_linked_input_executed':bool(experiments), 'same_job_result_reinference':feedback,
             'post_input_explanation_candidate':explanation, 'environment_goal_reached':bool(won),
             'human_readable_report_saved':bool(reports)}
    inconclusive = bool(error and any(k in error for k in ('deadline','TimeoutError','InterruptedError','InferenceUnavailable'))) or payload.get('research_limit')=='time'
    return {'status':'inconclusive_runtime_budget' if inconclusive else 'failed' if error or not all(gates.values()) else 'behavioral_candidate_requires_semantic_review',
            'first_unmet':next((k for k,v in gates.items() if not v), None), 'gates':gates,
            'semantic_understanding_verified':False, 'error':error}


def run_case(root, seed, timeout):
    game = BlindEnvironment(seed)
    c = Controller(Config(root=str(root), keys=['A','B'], research_every=1000000,
                          quota_bytes=1024**3, restart_after=1000000), game)
    worker = None
    errors = []
    responses = []
    try:
        game.start()
        # One real bootstrap Player input, never credited as LLM achievement.
        c.step()
        game.capture().save(root/'initial.png')
        bootstrap = len(game.transitions)
        initial_distance = abs(game.position-game.target)
        jid = c.store.job('research', {})
        original = c.scientist.complete
        def record(prompt, **options):
            # Record unchanged model I/O; do not alter prompt/schema/tool choices.
            step = len(responses)+1
            (root/f'prompt-{step}.txt').write_text(prompt, encoding='utf-8')
            value = original(prompt, **options)
            write_json(root/f'completion-{step}.json', c.scientist.client.last_completion)
            (root/f'response-{step}.txt').write_text(value, encoding='utf-8')
            responses.append((step, json.loads(value)))
            print(json.dumps({'seed':seed,'step':step,'actions':responses[-1][1].get('actions', []),
                              'reason':responses[-1][1].get('reason','')},ensure_ascii=False),flush=True)
            return value
        c.scientist.complete = record
        def study():
            try:c.scientist.study(c.store.rows(), jid)
            except Exception as exc:errors.append(type(exc).__name__+': '+str(exc))
        worker = threading.Thread(target=study)
        worker.start()
        deadline = time.monotonic()+timeout
        while worker.is_alive() and time.monotonic()<deadline:
            # The production Player executes only LLM-requested experiments here;
            # background exploratory inputs cannot earn oracle credit.
            with c.store.lock:
                pending = c.store.db.execute("SELECT 1 FROM research_experiments WHERE state IN ('queued','running') LIMIT 1").fetchone()
            if pending:c.step()
            else:
                # A real fresh capture without an input also services fresh=true.
                with c.visual_condition:
                    c.visual_frame=game.capture();c.visual_version+=1;c.visual_condition.notify_all()
                time.sleep(.05)
        if worker.is_alive():
            errors.append('Harness deadline reached');c.scientist.stop_event.set();worker.join(15)
        row = c.store.get_job(jid)
        payload = json.loads(row['payload'])
        reports = [dict(r) for r in c.store.db.execute('SELECT * FROM research_reports')]
        result = assess(payload, game.won and len(game.transitions)>bootstrap, responses, reports, '; '.join(errors) or None)
        result.update(seed=seed, job=jid, job_state=row['state'], job_outcome=payload.get('outcome'),
                      research_calls=payload.get('research_calls',0),
                      oracle={'direction_for_A':game.direction,'target':game.target,'initial_distance':initial_distance,
                              'final_distance':abs(game.position-game.target),'bootstrap_inputs':bootstrap,
                              'llm_transitions':game.transitions[bootstrap:]}, reports=reports)
        write_json(root/'job.json', payload)
        game.capture().save(root/'final.png')
        write_json(root/'result.json', result)
        return result
    finally:
        c.scientist.stop_event.set()
        if worker:worker.join(15)
        c.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-root',type=Path,required=True)
    parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seeds',type=int,nargs='+',default=[271828,314159])
    parser.add_argument('--timeout',type=int,default=660)
    args=parser.parse_args()
    if args.output.exists():raise ValueError('Use a fresh output directory; prior runs must remain immutable')
    args.output.mkdir(parents=True)
    (args.output/'harness.py').write_bytes(Path(__file__).read_bytes())
    state=json.loads(args.state.read_text())
    manager=ManagedLLM(args.model_root,24*1024**3)
    try:
        if not manager.start(state):raise RuntimeError('Model unavailable')
        write_json(args.output/'preregistration.json',{'model_sha256':state['model_sha256'],
            'engine_sha256':state['engine_sha256'],'context':state.get('context',8192),'backend':state.get('backend'),
            'runtime_capabilities':manager.active_state.get('capabilities'), 'seeds':args.seeds,
            'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
            'harness_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'platform':platform.platform(), 'projector_sha256':state.get('projector_sha256'),
            'production_prompt_unchanged':True,'production_schema_unchanged':True,'forced_tool_sequence':False,
            'oracle_available_to_model':False,'bootstrap_inputs_excluded':True,
            'success_requires':['self_selected_spatial_observation','hypothesis_linked_input','same_job_feedback',
                                'post_input_explanation','hidden_environment_goal','grounded_report','independent_semantic_review']})
        results=[run_case(args.output/f'case-{seed}',seed,args.timeout) for seed in args.seeds]
        write_json(args.output/'summary.json',{'cases':results,'real_game_validated':False,
            'full_goal_achieved':False,'limitation':'Synthetic blind task; semantic review and real-game validation remain required.'})
        print(json.dumps({'cases':[{k:r[k] for k in ('seed','status','first_unmet','gates')} for r in results]},indent=2),flush=True)
        return 0 if all(r['status']=='behavioral_candidate_requires_semantic_review' for r in results) else 1
    finally:manager.stop()


if __name__=='__main__':raise SystemExit(main())
