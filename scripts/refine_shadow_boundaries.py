"""Check ASR cut candidates against local waveform pauses, without publishing."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path


def quiet_gaps(samples, rate):
    import numpy as np
    hop = max(1,round(rate*.01))
    step = hop / rate
    count = len(samples)//hop
    if not count:
        return []
    energy = np.sqrt(np.mean(samples[:count*hop].reshape(count,hop)**2,axis=1))
    threshold = min(.008,max(.0015,float(np.percentile(energy,80))*.06))
    flags = energy < threshold
    gaps=[];start=None
    for i,quiet in enumerate(flags):
        if quiet and start is None:start=i
        if start is not None and (not quiet or i==len(flags)-1):
            end=i if not quiet else i+1
            if (end-start)*step>=.119:gaps.append((start*step,end*step))
            start=None
    return gaps


def refine(draft, gaps, gap_basis='quiet'):
    units=draft['units']
    def bounds(clip):
        content=[u for u in units if u['id'] in clip['unitIds']]
        return (clip.get('spokenStart',min(u['start'] for u in content)),
                clip.get('spokenEnd',max(u['end'] for u in content)))
    def edges(clip):
        begin,end=bounds(clip)
        lower=max((u['end'] for u in units if u['role']!='content' and u['end']<=begin),default=0)
        upper=min((u['start'] for u in units if u['role']!='content' and u['start']>=end),default=draft['duration'])
        starts=[min(b-.04,max(a+.04,begin-.18,lower)) for a,b in gaps if b>=begin-.9]
        ends=[max(a+.04,min(b-.04,end+.18,upper)) for a,b in gaps if a<=end+.9
              and (end-begin>=.12 or a>=begin-.08)]
        start=max((x for x in starts if lower<=x<=begin+.06),default=None)
        stop=min((x for x in ends if end-.06<=x<=upper),default=None)
        if begin<.2:start=0
        if draft['duration']-end<.25:stop=draft['duration']
        return start,stop
    def can_merge(left,right):
        left_ids,right_ids=left['unitIds'],right['unitIds']
        barrier=any(u['role']!='content' and max(left_ids)<u['id']<min(right_ids) for u in units)
        return not barrier and bounds(right)[1]-bounds(left)[0]<=17.5
    clips=[dict(c) for c in draft['clips']]
    changed=True
    while changed:
        changed=False
        for i,clip in enumerate(clips):
            start,stop=edges(clip)
            pair=(i-1,i) if start is None and i>0 else (i,i+1) if stop is None and i+1<len(clips) else None
            if pair is None and stop is not None and i+1<len(clips):
                next_start,_=edges(clips[i+1])
                # A late word alignment can place the left edge after the
                # right clip has begun. Preserve both complete utterances in
                # one short target instead of playing shared speech twice.
                if next_start is not None and stop>next_start+.01:
                    if not any(a<=next_start and stop<=b for a,b in gaps):
                        pair=(i,i+1)
            if pair is None:continue
            a,b=pair
            if not can_merge(clips[a],clips[b]):continue
            left,right=clips[a],clips[b]
            clips[a:b+1]=[dict(left,end=right['end'],spokenStart=bounds(left)[0],spokenEnd=bounds(right)[1],
                              unitIds=left['unitIds']+right['unitIds'],text=left['text']+' '+right['text'])]
            changed=True;break
    result=[];unresolved=[]
    for i,clip in enumerate(clips):
        begin,end=bounds(clip);start,stop=edges(clip)
        silent=start is not None and stop is not None and any(a<=start+.03 and b>=stop-.03 for a,b in gaps)
        if start is None or stop is None or stop<=start or stop-start>18.05 or silent:
            unresolved.append({'clip':i,'text':clip['text'],'start':begin,'end':end,'missingStart':start is None,'missingEnd':stop is None})
            result.append(dict(clip,waveform='pending'))
        else:
            result.append(dict(clip,start=round(start,2),end=round(stop,2),
                               waveform='quiet-boundaries' if gap_basis=='quiet' else 'voice-boundaries'))
    for left,right in zip(result,result[1:]):
        if left['end']>right['start']+.01:
            if any(a<=right['start'] and left['end']<=b for a,b in gaps):
                midpoint=round((left['end']+right['start'])/2,2)
                left['end']=right['start']=midpoint
            else:
                unresolved.append({'error':'overlapping_waveform_clips','left':left['text'],'right':right['text']})
    coverage=[(max(0,w['start']-.3),min(draft['duration'],w['end']+.3))
              for u in units for w in u['words'] if 0<w['end']-w['start']<=3]
    coverage+=gaps
    song_start=None
    for u in units:
        if u['role']=='listening' and song_start is None:song_start=u['start']
        elif song_start is not None and u['role']!='listening':
            coverage.append((song_start,u['start']));song_start=None
    if song_start is not None:coverage.append((song_start,draft['duration']))
    cursor=0;unrecognized=[]
    for start,end in sorted(coverage):
        if start-cursor>=.65:unrecognized.append({'start':round(cursor,2),'end':round(start,2)})
        cursor=max(cursor,end)
    if draft['duration']-cursor>=.65:unrecognized.append({'start':round(cursor,2),'end':round(draft['duration'],2)})
    return dict(draft,clips=result,waveformIssues=unresolved,unrecognizedVoice=unrecognized)


def filter_voice_gaps(audio, intervals, detector=None):
    """Distinguish missing spoken content from instrumental/background sound."""
    if detector is None:
        from faster_whisper.vad import get_speech_timestamps,VadOptions
        def detector(samples):
            return get_speech_timestamps(samples,VadOptions(threshold=.35,min_speech_duration_ms=180,
                                                          min_silence_duration_ms=200,speech_pad_ms=40))
    spoken=[];other=[]
    for interval in intervals:
        begin=max(0,int((interval['start']-.3)*16000));end=min(len(audio),int((interval['end']+.3)*16000))
        speech=detector(audio[begin:end])
        has_voice=any(min(interval['end'],(begin+s['end'])/16000)
                      -max(interval['start'],(begin+s['start'])/16000)>=.25 for s in speech)
        (spoken if has_voice else other).append(interval)
    return spoken,other


def build(inventory,drafts,output):
    from scripts.shadow_audio_waveform import analyze_audio,missing_speech
    rows=json.loads(inventory.read_text())['recordings'];output.mkdir(parents=True,exist_ok=True)
    refiner_sha=hashlib.sha256(Path(__file__).read_bytes()+Path(__file__).with_name('shadow_audio_waveform.py').read_bytes()).hexdigest()
    counts={'checked':0,'pending':0,'cached':0,'missing':0,'voice':0}
    def process(row):
        source=drafts/(row['sha256']+'.json');target=output/source.name
        if not source.exists():return 'missing',False,False
        draft=json.loads(source.read_text())
        if target.exists():
            try:cached=json.loads(target.read_text())
            except (OSError,ValueError):cached={}
            if cached.get('candidateDigest')==draft['candidateDigest'] and cached.get('refinerSha256')==refiner_sha:
                return 'cached',bool(cached['waveformIssues']),bool(cached.get('unrecognizedVoice'))
        if draft['listenOnly']:
            result=dict(draft,waveformIssues=[],unrecognizedVoice=[])
        else:
            wave=analyze_audio(row['path'],row['sha256'],output.parent/'waveform-cache')
            voice_gaps=[];cursor=0
            for span in wave['speech']:
                if span['start']-cursor>=.12:voice_gaps.append((cursor,span['start']))
                cursor=max(cursor,span['end'])
            if draft['duration']-cursor>=.12:voice_gaps.append((cursor,draft['duration']))
            result=refine(draft,sorted([tuple(g) for g in wave['quietGaps']]+voice_gaps),gap_basis='local-non-speech')
            extras=[(c['start'],c['end']) for c in result['clips'] if c.get('waveform') in {'quiet-boundaries','voice-boundaries'}]+[tuple(g) for g in wave['quietGaps']]
            song=None
            import re
            for u in draft['units']:
                if u['role']!='content' and re.match(r'^listen\s+and\s+sing\b',u['text'],re.I):song=u['end']
                elif song is not None and u['role']!='content' and re.match(r'^listen\b',u['text'],re.I):
                    extras.append((song,u['start']));song=None
            if song is not None:extras.append((song,draft['duration']))
            transcript={'segments':[{'words':[w for u in draft['units'] for w in u['words']]}]}
            result['unrecognizedVoice']=missing_speech(transcript,wave['speech'],extra_coverage=extras)
            result['waveformAnalysisSha256']=wave['algorithmSha256']
        result['refinerSha256']=refiner_sha
        temporary=target.with_suffix('.tmp');temporary.write_text(json.dumps(result,ensure_ascii=False,indent=2));temporary.replace(target)
        return 'checked',bool(result['waveformIssues']),bool(result.get('unrecognizedVoice'))
    from concurrent.futures import ThreadPoolExecutor,as_completed
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures=[executor.submit(process,row) for row in rows]
        for n,future in enumerate(as_completed(futures),1):
            status,pending,voice=future.result();counts[status]+=1;counts['pending']+=pending;counts['voice']+=voice
            if n%25==0 or n==len(rows):
                progress=output.parent/'refine-progress.json';temporary=progress.with_suffix('.tmp')
                temporary.write_text(json.dumps({'processed':n,'total':len(rows),**counts}));temporary.replace(progress)
    print(json.dumps(counts),flush=True)


if __name__=='__main__':
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inventory',type=Path,required=True);p.add_argument('--drafts',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();build(a.inventory,a.drafts,a.output)
