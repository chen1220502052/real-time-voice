import numpy as np
from scripts.refine_shadow_boundaries import quiet_gaps,refine


def draft(units,clips):return {'duration':10,'units':units,'clips':clips}
def unit(i,start,end,role='content'):return {'id':i,'start':start,'end':end,'role':role,'words':[{'start':start,'end':end,'word':' sample'}]}
def clip(ids,start,end,text):return {'unitIds':ids,'start':start-.1,'end':end+.1,'spokenStart':start,'spokenEnd':end,'text':text}


def test_local_waveform_recovers_a_zero_length_asr_word_without_cutting_its_ending():
    d=draft([unit(0,3.7,3.7)],[clip([0],3.7,3.7,'Help.')])
    result=refine(d,[(3.2,3.6),(4.5,5)])
    assert not result['waveformIssues']
    assert result['clips'][0]['start']<3.6
    assert result['clips'][0]['end']>4.5


def test_no_pause_between_two_sentences_keeps_the_complete_short_exchange():
    d=draft([unit(0,1,2),unit(1,2.1,4)],[clip([0],1,2,'Hello.'),clip([1],2.1,4,'Hi there.')])
    result=refine(d,[(.5,.9),(4.1,4.5)])
    assert not result['waveformIssues']
    assert len(result['clips'])==1
    assert result['clips'][0]['text']=='Hello. Hi there.'


def test_waveform_never_merges_across_an_excluded_task():
    d=draft([unit(0,1,2),unit(1,2.1,3,'instruction'),unit(2,3.1,4)],
            [clip([0],1,2,'Hello.'),clip([2],3.1,4,'Goodbye.')])
    result=refine(d,[(.5,.9),(4.1,4.5)])
    assert result['waveformIssues']
    assert len(result['clips'])==2


def test_long_silence_does_not_add_seconds_of_dead_air_before_a_word():
    d=draft([unit(0,6,7)],[clip([0],6,7,'Hello.')])
    result=refine(d,[(0,5.9),(7.1,9.8)])
    assert result['clips'][0]['start']>=5.8
    assert result['clips'][0]['end']<=7.2


def test_quiet_gap_detection_uses_time_from_local_samples():
    signal=np.r_[np.zeros(300),np.full(400,.2),np.zeros(300)].astype('float32')
    gaps=quiet_gaps(signal,1000)
    assert gaps==[(0,.3),(.7000000000000001,1.0)] or np.allclose(gaps,[(0,.3),(.7,1.0)])


def test_shared_quiet_gap_can_be_used_by_adjacent_complete_sentences():
    d=draft([unit(0,1,3.06),unit(1,3.33,5)],
            [clip([0],1,3.06,'First sentence.'),clip([1],3.33,5,'Second sentence.')])
    result=refine(d,[(.4,.95),(3.08,3.32),(5.1,5.4)])
    assert not result['waveformIssues']
    assert result['clips'][0]['end']==result['clips'][1]['start']


def test_a_hallucinated_word_in_silence_is_never_marked_playable():
    d=draft([unit(0,3.7,3.8)],[clip([0],3.7,3.8,'Ghost.')])
    result=refine(d,[(0,10)])
    assert result['waveformIssues']


def test_music_is_not_reported_as_a_missing_spoken_sentence():
    from scripts.refine_shadow_boundaries import filter_voice_gaps
    audio=np.zeros(16000*10,dtype='float32');intervals=[{'start':2,'end':4}]
    assert filter_voice_gaps(audio,intervals,detector=lambda x:[])==([],intervals)
    spoken,other=filter_voice_gaps(audio,intervals,detector=lambda x:[{'start':4800,'end':32000}])
    assert spoken==intervals and other==[]


def test_overlapping_recovered_edges_merge_without_replaying_speech_twice():
    d=draft([unit(0,1,3.6),unit(1,3.7,6.5)],
            [clip([0],1,3.6,'First complete sentence.'),clip([1],3.7,6.5,'Second complete sentence.')])
    result=refine(d,[(.5,.9),(3.2,3.5),(6.6,7)])
    assert not result['waveformIssues']
    assert len(result['clips'])==1
    assert result['clips'][0]['unitIds']==[0,1]
    assert result['clips'][0]['end']>6.5
