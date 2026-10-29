import pytest
from scripts.segment_shadow_utterances import phonetic_intervals


def test_preserves_all_phonemes_but_removes_reviewed_task():
    gaps=[(0,.2),(2,3),(4,5),(6,7),(8,9),(10,12)]
    clips=phonetic_intervals(12,gaps,[{'start':.3,'end':1.9,'reason':'Listen and repeat task header verified against source'}])
    assert clips==[{'start':2.5,'end':8.5},{'start':8.5,'end':11.0}]
    for a,b in [(3.1,3.9),(5.1,5.9),(7.1,7.9),(9.1,9.9)]:
        assert any(c['start']<=a<b<=c['end'] for c in clips)


def test_does_not_cut_continuous_long_speech_or_music():
    with pytest.raises(ValueError,match='too long'):
        phonetic_intervals(24,[],[])


def test_rejects_exclusion_that_would_delete_following_utterance():
    with pytest.raises(ValueError,match='nearby'):
        phonetic_intervals(12,[(0,.2),(8,9),(11,12)],[{'start':.3,'end':2,'reason':'task'}])


def test_does_not_merge_across_excluded_middle_task():
    result=phonetic_intervals(10,[(0,.2),(2,3),(4,5),(6,7),(8,10)],
        [{'start':3.1,'end':3.9,'reason':'second activity marker'}])
    assert len(result)==2
    assert result[0]['end']<=2.5 and result[1]['start']>=4.5


def test_release_gate_rejects_missing_phoneme_or_changed_header_cut():
    from scripts.assemble_shadow_audit_release import validate_phonetic
    proof={'status':'locally_reviewed','sourceClassification':['语音'],
           'headerTranscriptSha256':'verified-local-transcript',
           'waveformAnalysisSha256':'verified-local-waveform','duration':12,
           'quietGaps':[(0,.2),(2,3),(4,5),(6,7),(8,9),(10,12)],
           'excludedHeaders':[{'start':.3,'end':1.9,'reason':'Listen and repeat'}]}
    proof['clips']=phonetic_intervals(proof['duration'],proof['quietGaps'],proof['excludedHeaders'])
    decision={'method':'source-verified-phonics-quiet-boundaries'}
    assert validate_phonetic(proof,decision,{})==proof['clips']
    proof['clips']=proof['clips'][:1]
    with pytest.raises(ValueError,match='phoneme was lost'):
        validate_phonetic(proof,decision,{})


def test_phonetic_release_gate_cannot_bypass_ordinary_sentence_review():
    from scripts.assemble_shadow_audit_release import validate_phonetic
    proof={'status':'locally_reviewed','sourceClassification':['课文与听力']}
    with pytest.raises(ValueError,match='ordinary sentence'):
        validate_phonetic(proof,{'method':'source-verified-phonics-quiet-boundaries'},{})


def test_phoneme_practice_keeps_song_available_as_full_listening():
    gaps=[(0,.2),(2,3),(4,5),(6,7),(30,32)]
    sections=[{'start':7,'end':32,'kind':'chant','reason':'Source activity includes a continuous musical chant after isolated sounds; full original listening retained.'}]
    clips=phonetic_intervals(32,gaps,[{'start':.3,'end':1.9,'reason':'opening task'}],listening_sections=sections)
    assert clips==[{'start':2.5,'end':6.5}]
    assert clips[0]['start']<3.1<3.9<clips[0]['end']
