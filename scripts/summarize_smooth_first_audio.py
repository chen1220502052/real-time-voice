"""Final paired check after protecting later TTS phrase tails."""
import array
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
    data=json.loads((DEST/'smooth-results.json').read_text())
    rows=data['rows'];assert len(rows)==112
    assert len({(r['profile'],r['id'],r['repeat']) for r in rows})==112
    assert not [r for r in rows if r.get('error')]
    frozen=json.loads((DEST/'frozen-smooth.json').read_text())
    workspace=Path(frozen['workspace'])
    assert all(digest(workspace/p)==sha for p,sha in frozen['sourceHashes'].items())
    result={'date':data['date'],'sourceSha256':digest(DEST/'smooth-results.json'),
            'sourceFilesVerified':len(frozen['sourceHashes']),'profiles':{},
            'order':'Smooth repeat 0, baseline repeat 0, baseline repeat 1, smooth repeat 1. First smooth block retained unchanged from its preceding pass; source, cases and RNG seeds unchanged.',
            'scope':'PCM-ready latency, same known input boundary and frozen Bots; later phrase-tail repair plus first-TTS priority. Not a microphone/browser playback benchmark.'}
    manifest={'sourceSha256':result['sourceSha256'],'rows':[]}
    for profile in ['native_actual','native_priority_smooth']:
        all_rows=[r for r in rows if r['profile']==profile]
        selected=[r for r in all_rows if r['id']!='q35-01']
        result['profiles'][profile]={
            'firstAudio':stats([r['firstAudioMs'] for r in selected]),
            'firstExposure':stats([r['firstAudioMs'] for r in selected if r['repeat']==0]),
            'repeat':stats([r['firstAudioMs'] for r in selected if r['repeat']==1]),
            'firstTts':stats([r['chunks'][0]['details']['inferenceMs'] for r in selected]),
            'fullSynthesis':stats([r['totalMs'] for r in selected]),
            'audioSecondsMedian':statistics.median(r['audioSeconds'] for r in selected),
            'chunksMedian':statistics.median(len(r['chunks']) for r in selected),
            'textPreserved':all(r['textPreserved'] for r in all_rows),
            'playbackUnderruns':sum(c['playbackUnderrunMs']>1 for r in selected for c in r['chunks'][1:]),
            'maxPlaybackUnderrunMs':max(c['playbackUnderrunMs'] for r in selected for c in r['chunks'])}
        for row in all_rows:
            path=Path(row['output']);assert digest(path)==row['audioSha256']
            with wave.open(str(path)) as wav:
                assert wav.getparams()[:3]==(1,2,16000)
                samples=array.array('h',wav.readframes(wav.getnframes()))
            rms=(sum(x*x for x in samples)/len(samples))**.5;assert rms>0
            manifest['rows'].append({k:row[k] for k in ['profile','id','repeat','text','recognized','output','audioSha256']} | {'rms':rms})
    a={(r['id'],r['repeat']):r for r in rows if r['profile']=='native_actual'}
    b={(r['id'],r['repeat']):r for r in rows if r['profile']=='native_priority_smooth'}
    pairs=[]
    for key in sorted(a):
        x,y=a[key],b[key]
        pairs.append({'id':key[0],'repeat':key[1],'sameText':x['textSha256']==y['textSha256'],
                      'sameInput':x['recognized']==y['recognized'],
                      'sameFirstChunkText':x['chunks'][0]['text']==y['chunks'][0]['text'],
                      'savedMs':x['firstAudioMs']-y['firstAudioMs'],
                      'baselineChunks':[c['text'] for c in x['chunks']],
                      'candidateChunks':[c['text'] for c in y['chunks']]})
    assert all(p['sameText'] and p['sameInput'] for p in pairs)
    selected=[p for p in pairs if p['id']!='q35-01']
    result['paired']={'n':len(pairs),'sameText':sum(p['sameText'] for p in pairs),
                      'sameInput':sum(p['sameInput'] for p in pairs),
                      'sameFirstChunkText':sum(p['sameFirstChunkText'] for p in pairs),
                      'nExcludingWarmFixture':len(selected),
                      'medianSavedMs':statistics.median(p['savedMs'] for p in selected),
                      'meanSavedMs':statistics.mean(p['savedMs'] for p in selected),
                      'fasterCount':sum(p['savedMs']>0 for p in selected),'rows':pairs}
    result['waveformsVerified']=len(manifest['rows'])
    (DEST/'smooth-summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    (ROOT/'.local/qwen35-first-audio/smooth-audio-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v if k!='paired' else {a:b for a,b in v.items() if a!='rows'} for k,v in result.items()},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
