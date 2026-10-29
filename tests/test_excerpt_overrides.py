import copy
import json
from pathlib import Path

import pytest
from scripts.build_shadow_assets import apply_excerpt_overrides

DATA = Path(__file__).resolve().parents[1] / 'backend/data/practice'


def test_reviewed_excerpts_survive_silence_candidate_rebuild():
    published = json.loads((DATA / 'audio-excerpts.json').read_text())['recordings']
    overrides = json.loads((DATA / 'excerpt-overrides.json').read_text())
    candidates = copy.deepcopy(published)
    for sha in overrides['recordings']:
        candidates[sha]['clips'] = [{'start': 0, 'end': 5}]
    apply_excerpt_overrides(candidates, overrides)
    for sha, entry in overrides['recordings'].items():
        assert candidates[sha]['clips'] == published[sha]['clips'] == entry['clips']


@pytest.mark.parametrize('clips', [
    [{'start': 0, 'end': 19}], [{'start': 4, 'end': 3}],
    [{'start': 0, 'end': float('nan')}], [{'start': 9, 'end': 11}],
    [{'start': 0, 'end': 5}, {'start': 4, 'end': 6}],
])
def test_invalid_reviewed_boundaries_do_not_replace_candidates(clips):
    metadata = {'source': {'duration': 10, 'clips': [{'start': 1, 'end': 2}]}}
    before = copy.deepcopy(metadata)
    with pytest.raises(ValueError):
        apply_excerpt_overrides(metadata, {'recordings': {'source': {'clips': clips}}})
    assert metadata == before


def test_changed_source_cannot_reuse_reviewed_boundaries():
    with pytest.raises(ValueError, match='source is missing'):
        apply_excerpt_overrides({'new-digest': {'duration': 10, 'clips': []}},
                                {'recordings': {'old-digest': {'clips': [{'start': 1, 'end': 3}]}}})


def test_holiday_dialogues_exclude_headings_tasks_and_conversation_announcements():
    recordings = json.loads((DATA / 'audio-excerpts.json').read_text())['recordings']
    for sha, first, announcements in [
        ('19500d1391514a39b276f3ea345c19c8b6b7c508ca5821e24b69b44f03e999c8', 19.60, [(0, 19.08), (69.54, 71.52)]),
        ('30a592dd823102e18cda2a2bd77211bc5365a0f79aed75e076ee4f24a8ad12b8', 11.65, [(0, 11.12), (61.58, 63.54)]),
    ]:
        clips = recordings[sha]['clips']
        assert len(clips) == 11
        assert clips[0]['start'] == first
        assert clips[0]['end'] - first > 10  # Opening exchange, rather than a heading.
        for clip in clips:
            assert all(clip['end'] <= start or clip['start'] >= end for start, end in announcements)
        # Preserve the final question and answer, including the final sentence ending.
        assert clips[-1]['end'] >= recordings[sha]['duration'] - 1.1


def test_reviewed_decoded_duration_survives_rebuild_of_mp3_container_estimate():
    metadata={'source':{'duration':10.52,'clips':[{'start':0,'end':5}]}}
    apply_excerpt_overrides(metadata,{'recordings':{'source':{'duration':10,'clips':[{'start':2,'end':9.9}]}}})
    assert metadata['source']=={'duration':10,'clips':[{'start':2,'end':9.9}]}
