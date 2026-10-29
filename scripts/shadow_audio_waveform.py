"""Source-bound local waveform/speech cache shared by recognition and cut checks."""
from __future__ import annotations
import hashlib
import json
from math import gcd
from pathlib import Path


def analyze_audio(path,sha,cache_dir):
    from scripts.refine_shadow_boundaries import quiet_gaps
    import soundfile as sf
    import inspect
    from scipy.signal import resample_poly
    from faster_whisper.vad import get_speech_timestamps,VadOptions
    cache_dir.mkdir(parents=True,exist_ok=True);target=cache_dir/(sha+'.json')
    algorithm=hashlib.sha256(Path(__file__).read_bytes()+inspect.getsource(quiet_gaps).encode()).hexdigest()
    if target.exists():
        saved=json.loads(target.read_text())
        if saved.get('algorithmSha256')==algorithm and saved.get('sha256')==sha:return saved
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==sha,'Audio changed before waveform review'
    samples,rate=sf.read(path,dtype='float32',always_2d=True);mono=samples.mean(axis=1)
    quiet=quiet_gaps(mono[::4],rate/4);divisor=gcd(rate,16000)
    audio=resample_poly(mono,16000//divisor,rate//divisor) if rate!=16000 else mono
    spans=get_speech_timestamps(audio,VadOptions(threshold=.35,min_speech_duration_ms=180,min_silence_duration_ms=200,speech_pad_ms=40))
    speech=[{'start':s['start']/16000,'end':s['end']/16000} for s in spans]
    result={'sha256':sha,'algorithmSha256':algorithm,'decodedDuration':len(audio)/16000,
            'quietGaps':quiet,'speech':speech}
    temporary=target.with_suffix('.tmp');temporary.write_text(json.dumps(result,ensure_ascii=False));temporary.replace(target)
    return result


def missing_speech(transcript,speech,padding=.3,extra_coverage=()):
    coverage=sorted((max(0,w['start']-padding),w['end']+padding)
                    for s in transcript['segments'] for w in (s.get('words') or [])
                    if 0<=w['end']-w['start']<=3)
    coverage=sorted(coverage+list(extra_coverage))
    merged=[]
    for start,end in coverage:
        if merged and start<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],end)
        else:merged.append([start,end])
    missing=[]
    for span in speech:
        cursor=span['start']
        for start,end in merged:
            if end<=cursor:continue
            if start>=span['end']:break
            if start-cursor>=.5:missing.append({'start':round(cursor,3),'end':round(min(start,span['end']),3)})
            cursor=max(cursor,end)
            if cursor>=span['end']:break
        if span['end']-cursor>=.5:missing.append({'start':round(cursor,3),'end':round(span['end'],3)})
    return missing
