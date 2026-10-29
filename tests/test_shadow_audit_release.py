import copy
import pytest
from scripts.assemble_shadow_audit_release import validate_intervals,validate_practice


def draft():
    return {'duration':20,'issues':[],'waveformIssues':[],
            'units':[{'id':0,'start':1,'end':3,'role':'instruction'}, {'id':1,'start':5,'end':8,'role':'content'}],
            'clips':[{'start':4.8,'end':8.2,'unitIds':[1],'waveform':'quiet-boundaries'}]}


def test_release_contains_only_intervals_and_never_unverified_transcript_text():
    assert validate_practice(draft(),{'decision':'practice'})==[{'start':4.8,'end':8.2}]


@pytest.mark.parametrize('change',[
    {'waveformIssues':['missing_pause']}, {'issues':['long_sentence_without_safe_clause_boundary']},
    {'clips':[]}, {'clips':[{'start':2,'end':8,'unitIds':[0,1],'waveform':'quiet-boundaries'}]},
    {'clips':[{'start':4,'end':8,'unitIds':[1],'waveform':'pending'}]},
])
def test_unresolved_cut_or_content_deletion_blocks_publication(change):
    value=draft()|change
    with pytest.raises(ValueError):validate_practice(value,{'decision':'practice'})


def test_a_whole_sentence_cannot_be_silently_omitted():
    value=draft();value['units'].append({'id':2,'start':10,'end':12,'role':'content'})
    with pytest.raises(ValueError,match='Content was lost'):validate_practice(value,{'decision':'practice'})


def test_a_long_sentence_failure_cannot_be_waived_as_a_minor_recognition_warning():
    value=draft();value['issues']=['long_sentence_without_safe_clause_boundary']
    with pytest.raises(ValueError):validate_practice(value,{'decision':'practice','reason':'checked',
        'waivedWarnings':value['issues']})


def test_listening_only_needs_an_explicit_reason():
    with pytest.raises(ValueError):validate_practice(draft(),{'decision':'listen_only'})
    assert validate_practice(draft(),{'decision':'listen_only','reason':'song stays a full listening resource'})==[]


def test_overlap_non_finite_and_overlong_intervals_are_rejected():
    for clips in [[{'start':1,'end':float('inf')}],[{'start':0,'end':19}],
                  [{'start':1,'end':5},{'start':4,'end':6}]]:
        with pytest.raises(ValueError):validate_intervals(clips,20)


def test_unrecognized_audible_content_blocks_a_complete_review_claim():
    value=draft();value['unrecognizedVoice']=[{'start':10,'end':14}]
    with pytest.raises(ValueError,match='Audible content remains'):validate_practice(value,{'decision':'practice'})
