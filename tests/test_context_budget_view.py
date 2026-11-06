import json

from selfstruct.single_link import prompt_view


def _facts():
    frame = [[[x % 256, 0, 0] for x in range(32)] for _ in range(32)]
    regions = [0.0] * 64
    regions[3] = 0.5
    regions[40] = 0.02
    return {
        'assignment': {'kind': 'claim', 'hypothesis_id': 9},
        'hypothesis': {'id': 9, 'text': 'h' * 900, 'evidence': list(range(50))},
        'states': [{'id': 14, 'text': 's' * 900, 'evidence': list(range(50))}],
        'baseline': [{'id': 8, 'state': 14,
                      'frames': json.dumps([frame] * 5),
                      'regions': json.dumps(regions)}],
        'samples': [{'id': i} for i in range(16)],
        'evidence_ids': list(range(100)),
    }


def test_baseline_frames_never_rendered_as_arrays():
    view = prompt_view(_facts())
    record = view['baseline'][0]
    assert 'frames' not in record
    assert record['frames_count'] == 5
    assert record['active_regions'] == [3, 40]
    assert len(json.dumps(view)) < 6000


def test_compact_view_drops_evidence_and_truncates_text():
    facts = _facts()
    full = prompt_view(facts)
    compact = prompt_view(facts, compact=True)
    assert len(json.dumps(compact)) < len(json.dumps(full))
    assert 'evidence' not in compact['hypothesis']
    assert len(compact['hypothesis']['text']) == 300
    assert len(compact['samples']) == 1 and compact['samples_count'] == 16
    assert 'evidence_ids' not in compact


def test_prompt_view_does_not_mutate_facts():
    facts = _facts()
    prompt_view(facts)
    prompt_view(facts, compact=True)
    assert 'frames' in facts['baseline'][0]
    assert len(facts['hypothesis']['text']) == 900
