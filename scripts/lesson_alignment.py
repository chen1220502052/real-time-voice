"""Match reference text to independently recognized words, without inventing timings."""
import math
import re

CONTRACTIONS = {"i'm":"i am", "it's":"it is", "he's":"he is", "she's":"she is",
                "we're":"we are", "they're":"they are", "you're":"you are",
                "don't":"do not", "doesn't":"does not", "isn't":"is not", "can't":"cannot"}
NUMBERS = dict(zip(map(str,range(21)),
    'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split()))

def tokens(text):
    value=str(text).casefold().replace('’', "'")
    for before,after in CONTRACTIONS.items():
        value=re.sub(r'\b'+re.escape(before)+r'\b',after,value)
    return [NUMBERS.get(t,t) for t in re.findall(r"[a-z]+(?:'[a-z]+)*|\d+",value)]

def recognized_words(transcript):
    result=[]
    for segment in transcript.get('segments',[]):
        for word in segment.get('words',[]):
            start,end=word.get('start'),word.get('end')
            probability=word.get('probability',0)
            if (not all(isinstance(v,(int,float)) and math.isfinite(v) for v in [start,end,probability])
                    or start<0 or end<=start):
                continue
            for token in tokens(word['word']):
                result.append({'token':token,'start':start,'end':end,'probability':probability})
    return result

def align_references(references, transcript, duration, *, minimum_probability=.7):
    """Publish only exact normalized word matches, finite short intervals and no overlap.

    Source timecodes are deliberately ignored: several collected pages contain a
    complete book's text next to a single recording. Independent ASR supplies the
    audible-word positions; its wording is never substituted for source text.
    """
    from scripts.shadow_audio_review import HEADING, ACTIVITY, MARKER_PHRASES
    words=recognized_words(transcript)
    normalized=[w['token'] for w in words]
    candidates=[]
    seen=set()
    for reference in references:
        text=reference.get('text')
        if not isinstance(text,str):continue
        target=tokens(text)
        if (reference.get('textSource')!='image-verified' and
                (HEADING.fullmatch(text.strip()) or ACTIVITY.match(text.strip())
                 or ' '.join(target) in MARKER_PHRASES)):
            continue
        if len(target)<2 or tuple(target) in seen:continue
        seen.add(tuple(target))
        for index in range(len(words)-len(target)+1):
            if normalized[index:index+len(target)]!=target:continue
            matched=words[index:index+len(target)]
            if any(b['start']<a['start'] or b['start']-a['end']>2 for a,b in zip(matched,matched[1:])):continue
            confidence=sum(w['probability'] for w in matched)/len(matched)
            start,end=max(0,matched[0]['start']-.025),min(duration,matched[-1]['end']+.06)
            if confidence<minimum_probability or not 0<end-start<=18:continue
            candidates.append({**reference,'start':round(start,3),'end':round(end,3),
                               'confidence':round(confidence,3),'alignment':'local-asr-exact-reference-match',
                               '_wordCount':len(target)})
            break
    # Prefer the longer complete reference when the source repeats sub-sentences.
    selected=[]
    for candidate in sorted(candidates,key=lambda r:(-r['_wordCount'],r['start'])):
        if any(min(candidate['end'],p['end'])-max(candidate['start'],p['start'])>.08 for p in selected):continue
        selected.append(candidate)
    selected.sort(key=lambda r:r['start'])
    for index,row in enumerate(selected):
        row.pop('_wordCount')
        if index and row['start']<selected[index-1]['end']:
            boundary=round((row['start']+selected[index-1]['end'])/2,3)
            selected[index-1]['end']=boundary;row['start']=boundary
    return selected
