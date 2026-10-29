import array
from pathlib import Path

import pytest

from scripts.signal_metrics import first_onset, last_onset, bursts, reconciliation
from scripts.check_endpoint_ab import arm_values, guardrails, summarize
from scripts.build_endpoint_fixtures import controlled_pause
from scripts.analyze_endpoint_ab import summarize_verified
from scripts.run_endpoint_browser_ab import review


def row(ms=2000, failures=None):
    return {'fixture':'en_hello_16k.wav','sourceEndToFirstPacketMs':ms,
            'endpointToFinalMs':900,'finalToFirstPacketMs':800,
            'guardrailFailures':failures or []}


def test_signal_threshold_is_absolute_inclusive_and_peak_independent():
    assert first_onset([0,299,-300,20000])==2
    assert last_onset([20000,300,299,0])==1
    assert first_onset([0,299,-299]) is None
    assert bursts([300]+[0]*4801+[-300])==[0,4802]
    assert last_onset([20000,350,301])==2  # Quiet tail must not disappear at 8% peak.


def test_script_uses_the_same_policy_as_the_frontend_and_no_embedded_auth():
    assert 'Math.abs(value)>=300' in Path('web/recorder.js').read_text()
    assert 'Math.abs(sample)>=300' in Path('web/latency.js').read_text()
    assert '300 / 32768' in Path('web/output-worklet.js').read_text()
    script=Path('scripts/browser_recorder_test.py').read_text()
    assert 'ratio=0.08' not in script and 'AUTH = None' in script
    assert 'pkill' not in script and '"rm", "-rf"' not in script


def test_reconciliation_uses_disjoint_intervals_and_retains_missingness():
    server={k:10 for k in ['sourceEndFrameToFinalMs','finalToBotRequestMs','botToFirstTextMs',
        'firstTextToTtsMs','ttsToFirstPcmMs','pcmToFrameMs','frameQueueMs','sendLockWaitMs']}
    server['sendAwaitMs']=99
    client={k:1 for k in ['signalToPacketEndMs','captureDeliveryLagMs','captureCallbackToSendMs',
        'receiveToPlannedSignalMs','plannedToGraphSignalMs']}
    client.update(receiveToScheduleCallMs=99,captureSignalToGraphMs=105)
    r={'serverParts':server,'clientParts':client,'combinedTransportMs':20}
    assert reconciliation(r)['residualMs']==0
    del server['ttsToFirstPcmMs']
    assert reconciliation(r)['residualMs'] is None


@pytest.mark.parametrize('bad',[True,399,551,'400',None])
def test_arms_only_change_the_two_endpoint_parameters(bad):
    assert arm_values(400)=={'VAD_MAX_END_SILENCE_MS':'400','NLS_MAX_SENTENCE_SILENCE_MS':'400'}
    with pytest.raises(ValueError):arm_values(bad)


def test_failed_and_slower_turns_are_not_removed_from_denominator():
    pairs=[{'550':row(2000),'400':row(1800)}, {'550':row(2000),'400':row(2200,['premature_reply'])}]
    result=summarize(pairs)
    assert result['arms']['400']['attempts']==2
    assert result['pairedSavingsMs']['sourceEndToFirstPacketMs']['mean']==0
    assert not result['candidateSafe']
    for pair in pairs:
        pair['fixture']='en_hello_16k.wav'
    reviewed=summarize_verified({'status':'rejected','pairs':pairs})
    assert reviewed['validPairs']==1
    assert reviewed['allAttempts']['400']=={'n':2,'failed':1}
    assert reviewed['validPairedLatency']['sourceEndToFirstPacketMs']['pairedSavingMs']==200


def test_pause_guard_checks_both_input_clauses_and_early_output():
    r={'errors':[],'asrFinals':['What can you help me with?'],'earlyTextReplies':['r'],
       'earlyAudioReplies':[],'userReplies':[{'text':'I can help you.','complete':True,'audioFrames':1}]}
    failures=guardrails(r,'en_pause400_16k.wav')
    assert {'missing_input_clause','premature_reply'}.issubset(failures)
    r['earlyTextReplies']=[];r['asrFinals']=['What can you help me with, and can you tell me a joke?']
    assert not guardrails(r,'en_pause400_16k.wav')


@pytest.mark.parametrize('milliseconds', [300,400,500])
def test_controlled_pause_changes_only_zero_samples(milliseconds):
    original=array.array('h', [400,120]+[0]*6500+[150,-400])
    output,meta=controlled_pause(original,milliseconds)
    assert [x for x in output if x] == [x for x in original if x]
    assert last_onset(output)-first_onset(output)==milliseconds*16
    assert meta['nonzeroSamplesPreserved']


def test_recorder_analysis_does_not_hide_premature_output(tmp_path):
    import json
    import wave
    from scripts.browser_recorder_test import analyze_recording
    mic=array.array('h',[0])*32000; mic[16000]=300
    robot=array.array('h',[0])*32000;robot[8000]=300
    for suffix,pcm in [('mic',mic),('robot-scheduled',robot)]:
        with wave.open(str(tmp_path/f'test-{suffix}.wav'),'wb') as w:
            w.setparams((1,2,16000,0,'NONE','not compressed'));w.writeframes(pcm.tobytes())
    log={'clock':{'originCtxTime':0},'latency':{'replies':[
        {'replyId':'r','sourceSignalEndSequence':10,'sourceSignalEndCtx':.9,'graphOnsetCtx':.5,
         'clientParts':{'captureSignalToGraphMs':-400}}]},
        'replyScheduled':{'r':{'firstSignalCtx':.5}},
        'robotFrames':[{'replyId':'r','startAtCtx':.5,'samples':1000}]}
    path=tmp_path/'test-log.json';path.write_text(json.dumps(log))
    result=analyze_recording(path)
    assert result['wavSignalLatencyMs']==-500
    assert result['wavMinusCorrectedMs']==0
    assert result['earlyGraphReplies']==['r']
    assert result['physicalOutput'] is None
    assert result['perReplyGraphLatencyMs']==-400
    assert result['sourceBoundaryDifferenceMs']==pytest.approx(100)


def test_browser_split_reply_is_failure_even_if_low_latency_and_clocks_agree():
    result=review({'wavSignalLatencyMs':435,'correctedGraphLatencyMs':435,
        'wavMinusCorrectedMs':0,'earlyGraphReplies':[], 'asrFinalCount':2,
        'userReplyCount':2,'errors':[]})
    assert not result['passed']
