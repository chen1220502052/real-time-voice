"""Assemble reviewed local audio metadata; refuse incomplete or stale reviews.

This command never connects to a server. Keep transcripts and draft text local;
only source-bound intervals and review provenance are included in the release.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_intervals(clips,duration):
    previous=0
    for clip in clips:
        start,end=clip['start'],clip['end']
        if (not all(isinstance(v,(int,float)) and math.isfinite(v) for v in [start,end])
                or not previous<=start<end<=duration+.01 or end-start>18.05):
            raise ValueError('Invalid or overlapping reviewed interval')
        previous=end


def validate_practice(draft,decision):
    if decision['decision']!='practice':
        if decision['decision']!='listen_only' or not decision.get('reason'):
            raise ValueError('Unresolved or unexplained recording decision')
        return []
    if draft['waveformIssues']:
        raise ValueError('Unresolved waveform boundary')
    if draft.get('unrecognizedVoice'):
        raise ValueError('Audible content remains outside the recognized source ranges')
    waived=set(decision.get('waivedWarnings',[]))
    if set(draft['issues'])-waived:
        raise ValueError('Unreviewed recognition or semantic issue')
    allowed={'low_recognition_confidence','invalid_word_timestamp','duration_mismatch'}
    if waived-allowed:
        raise ValueError('A structural failure cannot be waived')
    if waived and not decision.get('reason'):
        raise ValueError('A reviewed warning needs its evidence')
    if not draft['clips']:
        raise ValueError('A practice recording has no content')
    retained={i for c in draft['clips'] for i in c['unitIds']}
    expected={u['id'] for u in draft['units'] if u['role']=='content'}
    if retained!=expected:
        raise ValueError('Content was lost or an excluded instruction was retained')
    clips=[]
    for clip in draft['clips']:
        if clip.get('waveform') not in {'quiet-boundaries','voice-boundaries'}:
            raise ValueError('Unverified waveform edge')
        for unit in draft['units']:
            if unit['role']!='content' and min(clip['end'],unit['end'])>max(clip['start'],unit['start'])+.03:
                raise ValueError('Clip contains an excluded title, instruction, or announcement')
        clips.append({k:clip[k] for k in ['start','end']})
    validate_intervals(clips,draft['duration'])
    return clips


def validate_phonetic(proof, decision, row):
    from scripts.segment_shadow_utterances import phonetic_intervals
    if decision.get('method')!='source-verified-phonics-quiet-boundaries' or proof.get('status')!='locally_reviewed':
        raise ValueError('Missing completed phonetic review')
    if '语音' not in proof.get('sourceClassification',[]):
        raise ValueError('Physical phonetic splitting cannot replace ordinary sentence review')
    if not proof.get('headerTranscriptSha256') or not proof.get('waveformAnalysisSha256'):
        raise ValueError('Phonetic source verification is missing')
    sections=proof.get('listeningSections',[])
    if any(s.get('kind')!='chant' or not s.get('reason') for s in sections):
        raise ValueError('Unreviewed listening-only subsection')
    expected=phonetic_intervals(proof['duration'],proof['quietGaps'],proof['excludedHeaders'],listening_sections=sections)
    if expected!=proof['clips']:
        raise ValueError('A phoneme was lost or a clip no longer matches the measured pauses')
    validate_intervals(expected,proof['duration'])
    return expected


def assemble(inventory,drafts,reviews,baseline,output,phonetics=None):
    rows=json.loads(inventory.read_text())['recordings']
    manifest=json.loads(reviews.read_text())
    decisions=manifest['recordings']
    phonetic_pack=json.loads(phonetics.read_text())['recordings'] if phonetics else {}
    if set(decisions)!={r['sha256'] for r in rows}:
        raise ValueError('Every inventoried source needs a completed review')
    pack=json.loads(baseline.read_text());overrides={'version':1,'recordings':{}}
    report={'recordings':len(rows),'practice':0,'listenOnly':0,'clips':0,'retainedReviewedOverrides':0,
            'baselineSha256':digest(baseline),'inventorySha256':digest(inventory),'reviewsSha256':digest(reviews)}
    if set(pack['recordings'])!={r['sha256'] for r in rows}:
        raise ValueError('Production source inventory differs; reconcile before publishing')
    for row in rows:
        sha=row['sha256'];path=drafts/(sha+'.json');decision=decisions[sha]
        if decision.get('draftSha256')!=digest(path):
            raise ValueError('Review no longer matches its local draft: '+sha)
        draft=json.loads(path.read_text())
        if draft['sha256']!=sha:
            raise ValueError('Wrong source digest in draft')
        if decision.get('method')=='source-verified-phonics-quiet-boundaries':
            proof=phonetic_pack.get(sha)
            if proof is None or decision.get('phoneticReviewDigest')!=hashlib.sha256(json.dumps(proof,ensure_ascii=False,sort_keys=True).encode()).hexdigest():
                raise ValueError('Missing or stale phonetic review')
            clips=validate_phonetic(proof,decision,row)
        elif decision.get('reuseReviewedOverride'):
            clips=pack['recordings'][sha]['clips'];validate_intervals(clips,draft['duration'])
            report['retainedReviewedOverrides']+=1
        else:
            clips=validate_practice(draft,decision)
        entry=pack['recordings'][sha]
        entry['duration']=round(draft['duration'],3)
        entry['clips']=clips
        proof={'clips':clips,'duration':entry['duration'],'note':decision['reason'],
               'audit':{'method':decision['method'],'decision':decision['decision'],
                        'transcriptDigest':draft['transcriptDigest'],'reviewDigest':digest(path)}}
        overrides['recordings'][sha]=proof
        report['practice' if clips else 'listenOnly']+=1;report['clips']+=len(clips)
    output.mkdir(parents=True,exist_ok=True)
    for name,value in [('audio-excerpts.json',pack),('excerpt-overrides.json',overrides),('validation.json',report)]:
        (output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['inventory','drafts','reviews','baseline','output']:p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--phonetics',type=Path)
    a=p.parse_args();assemble(a.inventory,a.drafts,a.reviews,a.baseline,a.output,a.phonetics)
