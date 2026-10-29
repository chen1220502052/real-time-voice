import json
from pathlib import Path

import pytest

from backend.chat_materials import material_context, _pack
from backend.text_bot import BotMessage
from scripts.build_chat_materials import build


@pytest.mark.parametrize('grade,topics,words', [(3,12,136),(4,12,188),(5,12,287),(6,6,238),(7,18,884),(8,16,885),(9,8,551)])
def test_grade_packs_match_local_sources_and_provenance(grade,topics,words):
    pack = _pack(grade)
    assert pack == build(grade)
    assert len(pack['topics']) == topics and len(pack['legacy_vocabulary']) == words
    assert all(len(source['sha256']) == 64 and Path(source['path']).is_file() for source in pack['sources'])
    assert all('tasks' not in topic and 'official_audio' not in topic for topic in pack['topics'])


def test_relevant_topics_and_word_definitions_are_injected_without_whole_library():
    context = json.loads(material_context(7, [BotMessage('user','I want to join an art club. What does guitar mean?')]))
    assert context['grade'] == 7 and len(context['topics']) == 2
    assert any('Fun Clubs' in topic['unit'] for topic in context['topics'])
    assert any(word['word'] == 'guitar' and word['meaning'] == '吉他' for word in context['legacySupplementalWords'])
    assert len(json.dumps(context,ensure_ascii=False)) < 6000


def test_latest_topic_switch_takes_priority_over_older_history():
    context = json.loads(material_context(7, [BotMessage('user','I like basketball.'),
                        BotMessage('assistant','Do you play with friends?'),
                        BotMessage('user','Let us talk about rain and the weather now.')]))
    assert any('Rain or Shine' in topic['unit'] for topic in context['topics'])


def test_materials_never_cross_grade_boundaries():
    a = json.loads(material_context(6, [BotMessage('user','Tell me about space.')]))
    b = json.loads(material_context(9, [BotMessage('user','Tell me about space.')]))
    assert a['grade'] == 6 and b['grade'] == 9
    assert a['topics'] != b['topics']


def test_compact_projection_preserves_definitions_examples_and_full_source_packs():
    messages = (BotMessage('user', 'I want to join an art club. What does guitar mean?'),)
    full = json.loads(material_context(7, messages))
    compact = json.loads(material_context(7, messages, compact=True))
    assert len(json.dumps(compact)) < len(json.dumps(full))
    assert compact['legacySupplementalWords'] == full['legacySupplementalWords']
    for source, projected in zip(full['topics'], compact['topics']):
        assert projected['unit'] == source['unit']
        assert projected['example'] == source['example']
        assert projected['words'] == [[w['word'], w['meaning']] for w in source['vocabulary']]
        assert 'kind' not in projected and 'edition' not in projected


def test_unrelated_input_does_not_inject_arbitrary_grade_topics():
    context = json.loads(material_context(7, (BotMessage('user', 'quasar pulsar'),), compact=True))
    assert context['topics'] == []
