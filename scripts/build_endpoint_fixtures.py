"""Change only a zero run to obtain measured 300/400/500ms clause pauses."""
import argparse
import array
import hashlib
import json
from pathlib import Path
import wave


def controlled_pause(samples, milliseconds):
    active=[i for i,value in enumerate(samples) if abs(value)>=300]
    left,right=max(zip(active,active[1:]),key=lambda pair:pair[1]-pair[0])
    runs=[];start=None
    for i in range(left+1,right):
        if samples[i]==0 and start is None:start=i
        elif samples[i]!=0 and start is not None:runs.append((start,i));start=None
    if start is not None:runs.append((start,right))
    begin,end=max(runs,key=lambda pair:pair[1]-pair[0])
    delta=milliseconds*16-(right-left)
    if end-begin+delta<0:raise ValueError('Cannot shorten pause without changing speech samples')
    output=samples[:begin]+array.array('h',[0])*(end-begin+delta)+samples[end:]
    assert [v for v in output if v]!=[ ]
    assert [v for v in output if v]==[v for v in samples if v]
    return output,{'targetAcousticGapMs':milliseconds,'originalAcousticGapMs':(right-left)/16,
                   'zeroSamplesAdded':delta,'nonzeroSamplesPreserved':True}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=Path('tests/fixtures/en/en_pause300_16k.wav'))
    p.add_argument('--output-dir',type=Path,required=True);args=p.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=True)
    with wave.open(str(args.source),'rb') as w:
        assert (w.getframerate(),w.getsampwidth(),w.getnchannels())==(16000,2,1)
        samples=array.array('h',w.readframes(w.getnframes()))
    report=[]
    for ms in (300,400,500):
        pcm,meta=controlled_pause(samples,ms)
        target=args.output_dir/f'controlled_pause{ms}_16k.wav'
        with wave.open(str(target),'wb') as w:
            w.setframerate(16000);w.setsampwidth(2);w.setnchannels(1);w.writeframes(pcm.tobytes())
        report.append(dict(meta,file=target.name,sha256=hashlib.sha256(target.read_bytes()).hexdigest()))
    (args.output_dir/'controlled-fixtures.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))


if __name__=='__main__':main()
