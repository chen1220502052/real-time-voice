"""Locally recognize already bounded utterances to verify phonics task headers.

English ASR is unreliable on isolated phonemes. These recognitions are only for
identifying the spoken task labels; the phoneme body is preserved from waveform
intervals, and recognizer text is never displayed as textbook text.
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
from math import gcd
import hashlib,json,time
from pathlib import Path


def run(inventory,output,model_path,threads=6):
    import soundfile as sf
    from scipy.signal import resample_poly
    from faster_whisper import WhisperModel,BatchedInferencePipeline
    model=WhisperModel(str(model_path),device='cpu',compute_type='int8',cpu_threads=threads,num_workers=1,local_files_only=True)
    model_sha=hashlib.sha256((model_path/'model.bin').read_bytes()).hexdigest();output.mkdir(parents=True,exist_ok=True)
    rows=json.loads(inventory.read_text())['recordings'];done=0;cached=0;started=time.monotonic()
    for row in rows:
        target=output/(row['sha256']+'.json');request_sha=hashlib.sha256(json.dumps(row['windows'],sort_keys=True).encode()).hexdigest()
        if target.exists():
            prior=json.loads(target.read_text())
            if prior['requestSha256']==request_sha and prior['modelSha256']==model_sha:
                cached+=1;continue
        assert hashlib.sha256(Path(row['path']).read_bytes()).hexdigest()==row['sha256']
        samples,rate=sf.read(row['path'],dtype='float32',always_2d=True);audio=samples.mean(axis=1)
        if rate!=16000:
            divisor=gcd(rate,16000);audio=resample_poly(audio,16000//divisor,rate//divisor)
        pipeline=BatchedInferencePipeline(model)
        segments,info=pipeline.transcribe(audio,language='en',beam_size=1,word_timestamps=True,
            condition_on_previous_text=False,vad_filter=False,clip_timestamps=row['windows'],
            max_new_tokens=64,no_repeat_ngram_size=4,batch_size=8)
        recognized=[asdict(s) for s in segments]
        result={'sha256':row['sha256'],'requestSha256':request_sha,'modelSha256':model_sha,
                'duration':len(audio)/16000,'windows':row['windows'],'segments':recognized,
                'purpose':'task-header verification only; not phoneme transcription','decoding':{'maxNewTokens':64,'noRepeatNgram':4}}
        temporary=target.with_suffix('.tmp');temporary.write_text(json.dumps(result,ensure_ascii=False,indent=2));temporary.replace(target)
        done+=1
        progress={'done':done,'cached':cached,'total':len(rows),'elapsed':round(time.monotonic()-started,1)}
        p=output.parent/'progress.json';temporary=p.with_suffix('.tmp');temporary.write_text(json.dumps(progress));temporary.replace(p)
        print(json.dumps(progress),flush=True)
    print('Header utterance recognition completed',flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['inventory','output','model']:p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--threads',type=int,default=6)
    a=p.parse_args();run(a.inventory,a.output,a.model,a.threads)
