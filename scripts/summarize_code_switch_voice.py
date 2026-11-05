"""Summarize frozen voice runs with the reviewed, case-specific task judgments.

The explicit pass lists are manual rubric labels, not model-based grading.
review.json retains every transcript, answer, failure, and rubric for inspection.
"""
import hashlib,json,math,re
from pathlib import Path
MAIN=Path('evals/code_switch_voice_release');PAUSE=Path('evals/code_switch_pause')
def q(values,p):
 if not values:return None
 a=sorted(values);pos=(len(a)-1)*p;i=math.floor(pos);j=math.ceil(pos)
 return round(a[i]+(a[j]-a[i])*(pos-i),2)
def latency(rows):
 a=[r['firstAudioMs'] for r in rows if r.get('firstAudioMs') is not None and r['firstAudioMs']>=0 and r.get('asrMs',0)>=-100]
 return {'n':len(a),'p50Ms':q(a,.5),'p95Ms':q(a,.95)}
def retained(row,case):
 return bool(case.get('target') and re.search(r'(?<![a-z])'+re.escape(case['target'])+r'(?![a-z])',row.get('recognized','').lower()))
reviews=[];summary={}
for pack,mode in [(MAIN,'offline'),(MAIN,'live'),(PAUSE,'live')]:
 cases={c['id']:c for c in json.loads((pack/'cases.json').read_text())};by={}
 for variant in ['baseline','candidate']:
  data=json.loads((pack/f'{variant}-{mode}-transcribed.json').read_text());rows=data['rows'];normal=[r for r in rows if cases[r['id']]['kind']=='task'];mixed=[r for r in normal if cases[r['id']]['language']=='mixed'];passed=[]
  for r in rows:
   case=cases[r['id']];kind=case['kind'];isnoise=kind=='noise';control=kind!='task'
   if pack==PAUSE:
    cover=bool(r.get('endpoint') and r['endpoint']['endMediaMs']>=case.get('targetEndMediaMs',float('inf')))
    good=(variant=='candidate' and r['id'] in {'p01','p03','p04'}) if not control else not r.get('recognized') and not r.get('text')
   else:
    cover=None
    ids=({'r01','r05','r10','r11','r13','r14','r15','r16'} if variant=='baseline' else {'r01','r02','r03','r04','r09','r10','r11','r12','r13','r14','r15','r16'})
    if mode=='live':
     # Exact target and requested parts are required; never count clarification.
     ids=({'r01','r13','r15'} if variant=='baseline' else {'r01','r03','r11','r13','r15'})
    good=r['id'] in ids
    if variant=='candidate' and mode=='offline' and r['id']=='r09' and r['repeat']==1:good=False
    if kind=='noise':good=not r.get('recognized') and not r.get('text')
    elif kind=='missing-target':good='没有听清' in r.get('text','')
    elif kind=='mandarin-word':good=not re.search('[A-Za-z]',r.get('recognized',''))
   if good and not control:passed.append(r)
   review={'pack':str(pack),'variant':variant,'mode':mode,'id':r['id'],'repeat':r['repeat'],'kind':kind,'rubric':case['rubric'],'recognized':r.get('recognized'),'answer':r.get('text'),'targetRetained':retained(r,case) if case.get('target') else None,'taskPass':good,'completeTargetAudio':cover,'audio':r.get('output'),'audioTranscript':r.get('audioTranscript'),'error':r.get('error')}
   if pack==MAIN and variant=='candidate' and r['id'] in {'r05','r07','r08'}:review['issue']='Substitution: bed→bad, hat→had, or bag→bad. Definition of the wrong word does not count as success.'
   if pack==MAIN and variant=='candidate' and r['id']=='r09' and r['repeat']==1:review['issue']='Having fun ... is very happy is not a suitable natural English example; strict quality failure.'
   if pack==MAIN and variant=='candidate' and r['id']=='r12':review['note']='ASR translated football into 足球: English spelling not retained, but the spoken answer conveys the requested meaning.'
   if pack==PAUSE and r['id']=='p02' and variant=='candidate':review['issue']='Full target audio received, but window was still omitted by Whisper; clarification is a failed task.'
   reviews.append(review)
  exact=sum(retained(r,cases[r['id']]) for r in mixed)
  by[variant]={'tasks':len(normal),'taskPass':len(passed),'taskPercent':round(100*len(passed)/len(normal),2),'mixedTrials':len(mixed),'mixedTargetsRetained':exact,'mixedTargetRetentionPercent':round(100*exact/len(mixed),2),'allResponsesLatency':latency(normal),'completedOnlyLatency':latency(passed),'errors':sum(bool(r.get('error')) for r in rows),'repairs':sum(bool(r.get('timing',{}).get('exampleRepairRequests') or r.get('timing',{}).get('languageRepairRequests')) for r in normal),'noiseControls':{'n':sum(cases[r['id']]['kind']=='noise' for r in rows),'clean':sum(cases[r['id']]['kind']=='noise' and not r.get('recognized') and not r.get('text') for r in rows)}}
  if pack==PAUSE:by[variant]['completeTargetAudioTrials']=sum(bool(r.get('endpoint') and r['endpoint']['endMediaMs']>=cases[r['id']].get('targetEndMediaMs',float('inf'))) for r in normal)
 summary[('pause' if pack==PAUSE else 'general')+'-'+mode]=by
# Final serving code unchanged after the pause-set freeze.
frozen=json.loads((PAUSE/'frozen.json').read_text())
summary['servingSourceAudit']={p:hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in frozen['sources'].items()};assert all(summary['servingSourceAudit'].values())
# The ordinary-corpus ASR-only metric is independent of the later endpoint fix.
old=json.loads((MAIN/'frozen.json').read_text());summary['ordinaryAsrSourceAudit']={p:hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in old['sources'].items() if p!='backend/providers/speech_local.py'};assert all(summary['ordinaryAsrSourceAudit'].values())
summary['servingMultilingualEngine']='whisper';summary['experimentalMultilingualEngine']='qwen3-asr-0.6B, opt-in only; not promoted due substitutions';summary['dialogue']='qwen2.5:3b, unchanged weights; no fine-tuning'
(MAIN/'review.json').write_text(json.dumps(reviews,ensure_ascii=False,indent=2)+'\n');(MAIN/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(summary,ensure_ascii=False,indent=2))
