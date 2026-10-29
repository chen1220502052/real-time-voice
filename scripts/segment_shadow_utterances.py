"""Draft phonics clips from reviewed exclusions and measured quiet gaps.

Never applies to ordinary sentences. English recognizers hallucinate isolated
phonemes; a source/page classification and explicit task-header review are
required. Original audio stays intact and no recognizer text reaches the UI.
"""
from __future__ import annotations
import math


def phonetic_intervals(duration, quiet_gaps, exclusions, *, target=8.0, maximum=18.0, listening_sections=()):
    if not math.isfinite(duration) or duration<=0:
        raise ValueError('Invalid decoded duration')
    gaps=sorted((max(0,a),min(duration,b)) for a,b in quiet_gaps if b-a>=.20)
    # Midpoints are inside measured silence, never at an inferred word time.
    cuts=sorted(set([0.0,duration]+[(a+b)/2 for a,b in gaps]))
    removed=[]
    for item in list(exclusions)+list(listening_sections):
        a,b=item['start'],item['end']
        if not 0<=a<b<=duration or not item.get('reason'):
            raise ValueError('Every excluded header needs finite source bounds and review evidence')
        before=max((c for c in cuts if c<=a+.08),default=0.0)
        after=min((c for c in cuts if c>=b-.08),default=duration)
        # Excluding across another utterance is unsafe; missing a nearby quiet
        # edge must send the source back for review rather than delete its body.
        def voiced_between(x,y):
            return sum(max(0,min(y,stop)-max(x,start)) for start,stop in nonquiet(duration,gaps))
        if ((a-before>1.8 and voiced_between(before,a)>.12)
                or (after-b>1.8 and voiced_between(b,after)>.12)):
            raise ValueError('Header has no nearby measured quiet boundary')
        removed.append((before,after))
    atoms=[]
    for a,b in zip(cuts,cuts[1:]):
        if any(min(b,y)>max(a,x)+.001 for x,y in removed):continue
        if not any(min(b,y)>max(a,x)+.08 for x,y in nonquiet(duration,gaps)):continue
        if b-a>maximum:
            raise ValueError('Continuous phonetic utterance is too long for a safe short target')
        atoms.append((a,b))
    clips=[]
    for a,b in atoms:
        if clips and abs(clips[-1]['end']-a)<.001 and b-clips[-1]['start']<=target:
            clips[-1]['end']=b
        else:clips.append({'start':a,'end':b})
    return [{'start':round(c['start'],3),'end':round(c['end'],3)} for c in clips]


def nonquiet(duration,gaps):
    spans=[];cursor=0.0
    for a,b in gaps:
        if a>cursor:spans.append((cursor,a))
        cursor=max(cursor,b)
    if cursor<duration:spans.append((cursor,duration))
    return spans
