"""Behavioral coverage for shared visual access, causal replay and durable research."""
import json
from pathlib import Path

import pytest
from PIL import Image

from selfstruct.activity import Activity
from selfstruct.research import Scientist
from selfstruct.sandbox import Revisions, Sandbox, replay
from selfstruct.store import Store
from selfstruct.vision import Eyes, VisualAccess, ReplayEyes, VISUAL_INSTRUCTIONS


@pytest.fixture
def world(tmp_path):
    store = Store(tmp_path, 128*1024**2)
    sid = store.session('Newborn', False)
    def add(before, after=None, change=0):
        a = store.image(before)
        b = store.image(after if after is not None else before)
        event = store.begin(sid, a, 'A', .01, 1, 'seed', 0, -1)
        store.finish(event, b, change, .001)
        return event
    yield store, add, VisualAccess(Eyes(store, Activity()))
    store.close()


def test_rgb_native_crop_and_paged_pixels_preserve_source_coordinates(world):
    store, add, access = world
    image = Image.new('RGB', (160, 120))
    image.paste((255, 30, 70), (80, 0, 160, 60))
    event = add(image)
    query = dict(op='observe', event=event, box=[80, 0, 160, 60], size='native', mode='RGB')
    values = []
    offset = 0
    while offset is not None:
        result, attachment = access.handle({**query, 'offset': offset, 'limit': 2048})
        assert attachment is None
        values.extend(result['pixels'])
        offset = result['next_offset']
    assert bytes(values) == image.crop((80, 0, 160, 60)).tobytes()
    assert result['evidence']['event'] == event
    assert result['coordinates'] == {'origin': [80, 0], 'scale': [1., 1.]}
    assert result['size'] == [80, 60] and result['width'] == 160
    pixel, _ = access.handle({**query, 'output': 'pixel', 'point': [3, 4]})
    assert pixel['value'] == (255, 30, 70)


def test_multiscale_diff_comparison_and_generic_geometry(world):
    _, add, access = world
    before = Image.new('RGB', (128, 128))
    after = before.copy(); after.paste('white', (16, 16, 32, 32))
    event = add(before, after, 1)
    result, _ = access.handle(dict(event=event, phase='diff', size=[128, 128],
                                  operations=[{'op': 'threshold', 'value': 10}], output='components'))
    assert result['component_count'] == 1
    assert result['components'] == [{'box': [16, 16, 32, 32], 'area': 256}]
    compared, _ = access.handle(dict(event=event, phase='before', size=[64, 64], output='statistics',
                                    compare={'event': event, 'phase': 'after'}))
    assert compared['evidence']['left']['phase'] == 'before'
    assert compared['evidence']['right']['phase'] == 'after'
    assert 0 < compared['evidence']['similarity'] < 1
    assert compared['mean'][0] > 0


@pytest.mark.parametrize('invalid', [
    {'size': [1000000, 1000000]}, {'size': [True, 32]}, {'size': [-1, 32]},
    {'box': [-1, 0, 10, 10]}, {'box': [0, 0, 1000, 1000]},
    {'mode': 'RGBA'}, {'operations': [{'op': 'exec', 'code': 'anything'}]},
    {'operations': [{'op': 'blur', 'radius': 100}]}, {'limit': 200000},
    {'phase': 'future'}, {'event': 999999},
])
def test_visual_boundary_rejects_invalid_sources_and_unbounded_work(world, invalid):
    _, add, access = world
    event = add(Image.new('RGB', (32, 32)))
    with pytest.raises(ValueError):
        access.handle({'event': event, **invalid})


def test_current_observation_is_revisitable_without_another_capture_and_keep_survives_gc(world):
    store, _, _ = world
    calls = []
    def capture():
        calls.append(True)
        return Image.new('RGB', (64, 64), (len(calls), 23, 45))
    eyes = Eyes(store, current=capture, fresh=capture)
    access = VisualAccess(eyes)
    result, _ = access.handle({'fresh': True, 'output': 'statistics'}, pin='research-job-one')
    observation = result['evidence']['observation']
    original, _ = access.handle({'observation': observation, 'output': 'statistics'})
    assert len(calls) == 1 and original['mean'] == result['mean']
    access.handle({'op': 'evidence_keep', 'observation': observation, 'reason': '再現確認の根拠'})
    store.unpin('research-job-one')
    for _ in range(140):
        eyes.query({'output': 'statistics'})
    store.collect(True)
    assert access.handle({'observation': observation, 'output': 'statistics'})[0]['mean'] == result['mean']
    assert not store.db.execute("SELECT 1 FROM visual_observation_pins WHERE reason='research-job-one'").fetchone()


def test_nonvision_image_request_returns_real_pixels_and_vision_gets_only_requested_image(world):
    _, add, access = world
    event = add(Image.new('RGB', (128, 96), 'red'))
    query = dict(event=event, output='image', mode='RGB', box=[32, 16, 96, 80], size=[64, 64])
    text, attachment = access.handle(query)
    assert attachment is None and text['pixels'][:3] == [255, 0, 0]
    vision, attachment = access.handle(query, native=True)
    assert attachment.startswith('data:image/png;base64,')
    assert 'pixels' not in vision and vision['evidence'] == text['evidence']
    assert vision['size'] == [64, 64]


def test_scientist_iterates_from_overview_to_detail_and_stores_inference_as_hypothesis(world):
    store, add, access = world
    event = add(Image.new('RGB', (128, 128), (15, 30, 45)))
    class Client:
        vision = False
        def __init__(self): self.calls = []
        def complete(self, prompt, **kwargs):
            self.calls.append((prompt, kwargs))
            if len(self.calls) == 1:
                return json.dumps({'visual_requests': [{'op': 'observe', 'event': event, 'size': [64, 64], 'mode': 'RGB', 'output': 'histogram'}]})
            if len(self.calls) == 2:
                assert 'histogram' in prompt and '"evidence":{"event":' in prompt
                return json.dumps({'visual_requests': [{'op': 'observe', 'event': event, 'size': [128, 128], 'box': [64, 0, 128, 64], 'limit': 32}]})
            assert '"size":[128,128]' in prompt and '"pixels":[' in prompt
            return json.dumps({'findings': [{'text': '領域の色が状態量に対応する可能性があります。新しい経験で反証します。', 'evidence': [event]}], 'wat': ''})
    client = Client()
    scientist = Scientist(store, Revisions(store.root/'agents'/'revisions'), client, ['A'], 48, lambda _: None)
    scientist.visual = access
    job = store.job('research', {})
    scientist.study(store.rows(), job)
    payload = json.loads(store.get_job(job)['payload'])
    assert len(client.calls) == 3 and len(payload['visual_history']) == 2
    assert sum(entry.get('kind')=='initial_evidence' for entry in payload['visual_history']) == 0
    assert all('images' not in kwargs for _, kwargs in client.calls)
    note = next(row for row in store.knowledge_rows() if row['kind'] == 'hypothesis')
    assert note['provenance'] == 'inferred' and note['validation'] == 'hypothesis'
    assert VISUAL_INSTRUCTIONS in client.calls[0][0]


def test_native_research_selects_images_and_persists_references_without_base64(world):
    store, add, access = world
    event = add(Image.new('RGB', (64, 64), 'blue'))
    class Client:
        vision = True
        calls = 0
        def complete(self, prompt, **kwargs):
            self.calls += 1
            if self.calls == 1:
                assert 'images' not in kwargs and '"pixels":[' not in prompt
                return json.dumps({'visual_requests': [{'event': event, 'output': 'image', 'size': [64, 64]}]})
            assert len(kwargs['images']) == 1
            return '{"findings":[],"wat":""}'
    client = Client()
    scientist = Scientist(store, Revisions(store.root/'revisions'), client, ['A'], 48, lambda _: None)
    scientist.visual = access
    job = store.job('research', {})
    scientist.study(store.rows(), job)
    assert client.calls == 2
    assert 'base64' not in store.get_job(job)['payload']


def test_analyzer_requires_new_evidence_and_useful_prediction_then_survives_model_change(world):
    store, add, access = world
    black, white = Image.new('RGB', (32, 32)), Image.new('RGB', (32, 32), 'white')
    def experiences(n):
        return [add(white if i % 2 else black, black, i % 2) for i in range(n)]
    training = experiences(32)
    definition = {'description': '領域平均の再利用可能な比較', 'query': {'size': [64, 64], 'output': 'statistics'}}
    identifier = access.analyzers.create(definition)
    with pytest.raises(ValueError, match='not passed'):
        access.analyzers.run(identifier, {'event': training[0]})
    with pytest.raises(ValueError, match='Held-out'):
        access.analyzers.evaluate(identifier, training[:16], training[16:])
    testing = experiences(32)
    result = access.analyzers.evaluate(identifier, training, testing)
    assert result['status'] == 'promoted'
    assert result['metrics']['candidate']['balanced_loss'] == 0
    assert result['metrics']['reproducible'] and result['metrics']['unknown'] == 0
    with pytest.raises(ValueError, match='Held-out'):
        access.analyzers.evaluate(identifier, training, testing)
    # New broker/client/runtime selection reuses the individual's durable DB assets.
    other = VisualAccess(Eyes(store))
    assert other.analyzers.run(identifier, {'event': testing[1]})['mean'] == [255.]
    assert other.analyzers.graph()[0]['active']
    other.analyzers.retire(identifier)
    with pytest.raises(ValueError):
        other.analyzers.run(identifier, {'event': testing[0]})
    assert other.analyzers.get(identifier)['metrics']['test'] == testing


def test_constant_analyzer_is_not_promoted_and_cannot_execute_code(world):
    _, add, access = world
    image = Image.new('RGB', (32, 32))
    training = [add(image, change=i % 2) for i in range(32)]
    identifier = access.analyzers.create({'description': '同じ画像からの定数', 'query': {'output': 'statistics'}})
    testing = [add(image, change=i % 2) for i in range(32)]
    assert access.analyzers.evaluate(identifier, training, testing)['status'] == 'rejected'
    with pytest.raises(ValueError):
        access.analyzers.create({'description': '危険な要求', 'query': {'output': 'statistics', 'operations': [{'op': 'python', 'code': 'import os'}]}})


def test_replay_rejects_future_after_diff_and_observation_ids(world):
    store, add, _ = world
    first = add(Image.new('RGB', (32, 32)), Image.new('RGB', (32, 32), 'white'), 1)
    second = add(Image.new('RGB', (32, 32), 'white'))
    eyes = ReplayEyes(store.root/'evidence', store.rows())
    eyes.frame = Image.new('RGB', (32, 32)); eyes.current_id = first
    assert eyes.query({'event': first, 'output': 'statistics'})[0]['mean'] == [0.]
    for request in ({'event': first, 'phase': 'after'}, {'event': first, 'phase': 'diff'},
                    {'event': second}, {'observation': 1}, {'fresh': True}):
        with pytest.raises(ValueError): eyes.query(request)
    eyes.revealed.add(first); eyes.current_id = second
    assert eyes.query({'event': first, 'phase': 'after', 'output': 'statistics'})[0]['mean'] == [255.]


def test_generated_wat_receives_high_resolution_observation_during_replay(world):
    store, add, _ = world
    event = add(Image.new('RGB', (96, 96), 'white'))
    seed = Path('src/selfstruct/assets/seed.wat').read_text()
    request = json.dumps({'op': 'observe', 'size': [64, 64], 'output': 'statistics'}, separators=(',', ':'))
    escaped = ''.join('\\%02x' % b for b in request.encode())
    code = seed[:-2] + f'''(data (i32.const 4096) "{escaped}")
      (func (export "request") (result i32) (i32.const {len(request)})))'''
    # Replay must call the capability protocol, not merely deliver the old 32x32 ABI.
    results = replay(code, store.rows(), store.root/'evidence', ['A'])
    assert results['n'] == 1
    # Running the same real WASM protocol directly yields the same source dimensions.
    agent = Sandbox(code)
    from selfstruct.vision import ReplayFiles
    eyes = ReplayEyes(store.root/'evidence', store.rows()); eyes.current_id = event; eyes.frame = Image.new('RGB', (96, 96), 'white')
    files = ReplayFiles()
    agent.requests(files, files.deny, lambda q: eyes.query(q)[0], eyes.dispatch)
    size = int.from_bytes(agent.memory.read(agent.store, 16384, 16388), 'little')
    response = json.loads(bytes(agent.memory.read(agent.store, 16388, 16388+size)))
    assert response['ok'] and response['size'] == [64, 64] and response['mean'] == [255.]


def test_library_requests_are_durable_and_unverified_native_libraries_never_execute(world):
    store, _, access = world
    response, _ = access.handle({'op': 'library_request', 'name': 'generic-pattern-tools',
                                'purpose': '模様比較の候補', 'reason': '観測した規則性を再検証するため'})
    assert response['state'] == 'awaiting_verified_catalog'
    assert store.db.execute('SELECT count(*) FROM visual_library_requests').fetchone()[0] == 1
    folder = store.account_root/'host'
    (folder/'visual-library-catalog.json').write_text('{"payload":"e30=","signature":""}')
    with pytest.raises(Exception):
        access.handle({'op': 'library_request', 'name': 'untrusted.dll', 'purpose': 'test', 'reason': 'test'})


def test_signed_library_gateway_imports_only_bounded_unevaluated_pipelines(world, monkeypatch):
    import base64
    import hashlib
    import time
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from selfstruct import visual_libraries
    store, _, access = world
    artifact = store.root/'library-test.json'
    bundle = {'runtime': 'selfstruct-visual-pipeline-v1', 'analyzers': [
        {'description': '署名付き汎用色分布', 'query': {'output': 'histogram', 'mode': 'RGB'}}]}
    artifact.write_text(json.dumps(bundle))
    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(visual_libraries, 'UPDATE_KEY', key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex())
    item = {'name': 'color-tools', 'runtime': bundle['runtime'], 'size': artifact.stat().st_size,
            'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(), 'license': 'Test-only',
            'trust': 'Trusted', 'url': 'https://github.com/test/color-tools.json'}
    def catalog(value):
        raw = json.dumps({'schema': 1, 'expires': time.time()+60, 'libraries': [value]}).encode()
        (store.root/'host'/'visual-library-catalog.json').write_text(json.dumps({
            'payload': base64.b64encode(raw).decode(), 'signature': base64.b64encode(key.sign(raw)).decode()}))
    catalog(item)
    downloaded = []
    def download(self, metadata, kind):
        downloaded.append((metadata, kind))
        assert metadata == item and kind == 'library'
        return artifact
    monkeypatch.setattr('selfstruct.resources.Gateway.download', download)
    request = dict(op='library_request', name='color-tools', purpose='色分布の比較', reason='観測から必要性を提案')
    result, _ = access.handle(request)
    assert result['state'] == 'acquired_unverified_analyzers'
    assert result['signature'] == 'verified_catalog'
    record = access.analyzers.get(result['analyzers'][0])
    assert record['status'] == 'candidate' and record['definition']['source']['provenance'] == 'external'
    catalog({**item, 'runtime': 'native-dll'})
    with pytest.raises(ValueError, match='declarative'):
        access.handle(request)
    assert len(downloaded) == 1  # Even signed native executables never reach download/execution.


def test_resumed_comparison_reuses_exact_current_observations(world):
    from selfstruct.vision import resolved_request
    store, _, _ = world
    calls = []
    def current():
        calls.append(True)
        return Image.new('RGB', (32, 32), (len(calls)*30, 0, 0))
    eyes = Eyes(store, current=current)
    request = {'output': 'image', 'compare': {'phase': 'before'}}
    result, original = eyes.query(request, pin='research-job-compare', native=True)
    resolved = resolved_request(request, result['evidence'])
    repeated, attachment = eyes.query(resolved, native=True)
    assert len(calls) == 2 and original == attachment
    assert repeated['evidence'] == result['evidence']
    assert store.db.execute('SELECT count(*) FROM visual_observation_pins').fetchone()[0] == 2


def test_scientist_can_cite_requested_older_evidence_outside_initial_summary(world):
    store, add, access = world
    ids = [add(Image.new('RGB', (32, 32), (i, 0, 0))) for i in range(20)]
    class Client:
        calls = 0
        def complete(self, prompt, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return json.dumps({'visual_requests': [{'event': ids[0], 'output': 'statistics'}]})
            return json.dumps({'findings': [{'text': '古い領域と最近の領域の関連は未確認です。', 'evidence': [ids[0]]}], 'wat': ''})
    scientist = Scientist(store, Revisions(store.root/'revisions'), Client(), ['A'], 48, lambda _: None)
    scientist.visual = access
    job = store.job('research', {})
    scientist.study(store.rows(), job)
    assert store.get_job(job)['state'] == 'complete'
    hypothesis = next(r for r in store.knowledge_rows() if r['kind'] == 'hypothesis')
    assert json.loads(hypothesis['evidence']) == [ids[0]]


def test_managed_vision_context_pressure_preserves_references_and_does_not_retry_forever(world, monkeypatch):
    from selfstruct.research import LocalLLM
    store, add, access = world
    event = add(Image.new('RGB', (32, 32), 'red'))
    client = LocalLLM('http://127.0.0.1:8766/v1', 'local')
    client.vision = True; client.context = 8192
    monkeypatch.setattr(client, 'health', lambda: None)
    monkeypatch.setattr(client, 'count_tokens', lambda prompt: 6144)
    calls = []
    def complete(prompt, **kwargs):
        calls.append((prompt, kwargs))
        if len(calls) == 1:
            return json.dumps({'visual_requests': [{'event': event, 'output': 'image', 'size': [64, 64]} for _ in range(4)]})
        assert len(kwargs['images']) == 1
        assert 'attachment_omitted' in prompt and '"attachment_index":0' in prompt
        assert kwargs['max_tokens'] >= 512
        return '{"findings":[],"wat":""}'
    monkeypatch.setattr(client, 'complete', complete)
    scientist = Scientist(store, Revisions(store.root/'revisions'), client, ['A'], 48, lambda _: None)
    scientist.visual = access
    job = store.job('research', {})
    scientist.study(store.rows(), job)
    assert len(calls) == 2 and store.get_job(job)['state'] == 'complete'
    assert len(json.loads(store.get_job(job)['payload'])['visual_history']) == 4


def test_actual_trace_identifies_evidence_region_and_resolution_within_display_budget(world):
    _, add, access = world
    event = add(Image.new('RGB', (128, 96)))
    access.handle({'event': event, 'box': [16, 0, 96, 64], 'size': [128, 64], 'mode': 'RGB', 'output': 'statistics'})
    trace = access.eyes.activity.snapshot().trace[-1]
    assert f'experience={event}:before' in trace.text
    assert 'crop=[16, 0, 96, 64]' in trace.text and '128x64 RGB' in trace.text
