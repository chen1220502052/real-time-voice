"""Validate first-audio experiment and compare paired identical replies."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics
import wave

from scripts.summarize_native_voice import stats

ROOT=Path(__file__).resolve().parents[1]
DEST=ROOT/'evals/first_audio'


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    data=json.loads((DEST/'results.json').read_text());rows=data['rows']
    assert len(rows)==280, f'Incomplete: {len(rows)}'
    keys=[(r['profile'],r['id'],r['repeat']) for r in rows]
    assert len(set(keys))==280
    frozen=json.loads((DEST/'frozen.json').read_text());workspace=Path(data['executionRoot'])
    changed=[p for p,sha in frozen['sourceHashes'].items() if digest(workspace/p)!=sha]
    assert not changed, changed
    defaults={p:digest(ROOT/p) for p in frozen['currentDefaultHashes']}
    defaults_unchanged=defaults==frozen['currentDefaultHashes']
    summary={'date':data['date'],'latencyDefinition':'Full input PCM with known end boundary to first output PCM, after warmup. No live VAD/browser/speaker buffers.',
             'measurementCorrection':'native_legacy uses take_speech_segments as in the preceding evaluation; native_actual uses the actual dialogue SpeechBuffer, which already existed in that frozen source. Legacy-to-actual reduction is measurement correction, not a newly implemented product improvement.',
             'profiles':{},'paired':{},'filesVerified':len(frozen['sourceHashes']),
             'defaultHashesBefore':frozen['currentDefaultHashes'],'defaultHashesAfter':defaults,
             'defaultHashesUnchanged':defaults_unchanged,
             'defaultScope':'This experiment never writes service configuration. A concurrent task changed the outer .env.local; all benchmark settings came from the unchanged frozen copy. Do not revert that external change.',
             'sourceSha256':digest(DEST/'results.json'),
             'reusedControls':data.get('reference')}
    manifest={'sourceSha256':summary['sourceSha256'],'rows':[]}
    for profile in ['3b_actual','native_legacy','native_actual','native_priority','native_tuned']:
        selected=[r for r in rows if r['profile']==profile]
        successful=[r for r in selected if not r.get('error') and r['id']!='q35-01']
        item={'count':len(selected),'errors':[r for r in selected if r.get('error')],
              'allExceptWarmFixture':stats([r['firstAudioMs'] for r in successful]),
              'firstExposure':stats([r['firstAudioMs'] for r in successful if r['repeat']==0]),
              'repeat':stats([r['firstAudioMs'] for r in successful if r['repeat']==1]),
              'priorCases':stats([r['firstAudioMs'] for r in successful if r['set']=='prior_regression']),
              'newCases':stats([r['firstAudioMs'] for r in successful if r['set']=='new_cases']),
              'firstChunkTts':stats([r['chunks'][0]['details']['inferenceMs'] for r in successful]),
              'firstChunkTextCharsMean':statistics.mean(len(r['chunks'][0]['text']) for r in successful),
              'firstChunkTextCharsMedian':statistics.median(len(r['chunks'][0]['text']) for r in successful),
              'firstChunkQueueMs':stats([r['chunks'][0]['details']['queueWaitMs'] for r in successful]),
              'playbackUnderruns':sum(c['playbackUnderrunMs']>1 for r in successful for c in r['chunks'][1:]),
              'maxPlaybackUnderrunMs':max(c['playbackUnderrunMs'] for r in successful for c in r['chunks']),
              'allTextPreserved':all(r['textPreserved'] for r in successful),
              'rulePasses':sum(r['score']['passed'] for r in selected if not r.get('error'))}
        summary['profiles'][profile]=item
        for row in selected:
            if row.get('error'):continue
            path=Path(row['output']);assert digest(path)==row['audioSha256']
            with wave.open(str(path)) as wav:
                assert wav.getparams()[:3]==(1,2,16000)
                import array
                samples=array.array('h',wav.readframes(wav.getnframes()))
                rms=(sum(x*x for x in samples)/len(samples))**.5
                assert rms>0
            manifest['rows'].append({k:row[k] for k in ['id','profile','repeat','text','recognized','output','audioSha256']} |
                                    {'rms':rms,'audioSeconds':len(samples)/16000})
    for a,b in [('native_legacy','native_actual'),('native_actual','native_priority'),('native_actual','native_tuned'),('native_priority','native_tuned')]:
        ra={(r['id'],r['repeat']):r for r in rows if r['profile']==a and not r.get('error')}
        rb={(r['id'],r['repeat']):r for r in rows if r['profile']==b and not r.get('error')}
        comparisons=[]
        for key in sorted(ra.keys()&rb.keys()):
            x,y=ra[key],rb[key]
            comparisons.append({'id':key[0],'repeat':key[1],
                'sameText':x['textSha256']==y['textSha256'],
                'sameRecognizedInput':x['recognized']==y['recognized'],
                'sameFirstChunkText':x['chunks'][0]['text']==y['chunks'][0]['text'],
                'sameFirstChunkPcm':x['chunks'][0]['pcmSha256']==y['chunks'][0]['pcmSha256'],
                'sameAllChunkBoundaries':[c['text'] for c in x['chunks']]==[c['text'] for c in y['chunks']],
                'savedMs':x['firstAudioMs']-y['firstAudioMs'],
                'baselineFirstChunk':x['chunks'][0]['text'], 'candidateFirstChunk':y['chunks'][0]['text']})
        matched=[r for r in comparisons if r['sameText'] and r['sameRecognizedInput'] and r['id']!='q35-01']
        summary['paired'][a+' -> '+b]={'n':len(comparisons),'sameText':sum(r['sameText'] for r in comparisons),
            'sameRecognizedInput':sum(r['sameRecognizedInput'] for r in comparisons),
            'sameFirstChunkText':sum(r['sameFirstChunkText'] for r in comparisons),
            'sameFirstChunkPcm':sum(r['sameFirstChunkPcm'] for r in comparisons),
            'sameAllChunkBoundaries':sum(r['sameAllChunkBoundaries'] for r in comparisons),
            'matchedComparisonExcludingWarmFixture':len(matched),
            'matchedMedianSavedMs':statistics.median(r['savedMs'] for r in matched),
            'matchedMeanSavedMs':statistics.mean(r['savedMs'] for r in matched),
            'matchedFasterCount':sum(r['savedMs']>0 for r in matched),'rows':comparisons}
    summary['waveformsVerified']=len(manifest['rows'])
    (DEST/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    (ROOT/'.local/qwen35-first-audio/audio-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in summary.items() if k not in ['paired']},ensure_ascii=False,indent=2))
    print(json.dumps({k:{a:b for a,b in v.items() if a!='rows'} for k,v in summary['paired'].items()},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
