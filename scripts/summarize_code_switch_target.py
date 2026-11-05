"""Reviewed labels for the target-word experiment; retains every failed task.

Scores describe the frozen experiment, which was rejected for serving.
No reference string is supplied to any recognizer.
"""
import hashlib,json,math,re
from pathlib import Path
PACK=Path('evals/code_switch_target_final')
def quantile(values,p):
 if not values:return None
 a=sorted(values);x=(len(a)-1)*p;i=math.floor(x);return round(a[i]+(a[math.ceil(x)]-a[i])*(x-i),2)
def latency(rows):
 a=[r['firstAudioMs'] for r in rows if r.get('firstAudioMs') is not None and r['firstAudioMs']>=0]
 return dict(n=len(a),p50Ms=quantile(a,.5),p95Ms=quantile(a,.95))
def retained(r,c):return bool(c.get('target') and re.search(r'(?<![a-z])'+re.escape(c['target'])+r'(?![a-z])',r.get('recognized','').lower()))
def substitution(r,c):
 text=r.get('recognized','')
 return bool(c.get('target') and not retained(r,c) and (re.search('[A-Za-z]',text) or re.search(r'\d',text)))
reviews=[];summary={};cases={c['id']:c for c in json.loads((PACK/'cases.json').read_text())}
for mode in ['offline','live']:
 summary[mode]={}
 for variant in ['baseline','candidate']:
  data=json.loads((PACK/f'{variant}-{mode}.json').read_text());assert data.get('completedUtc')
  main=[r for r in data['rows'] if cases[r['id']]['kind']=='task'];mixed=[r for r in main if cases[r['id']]['language']=='mixed'];passed=[]
  for r in data['rows']:
   c=cases[r['id']];kind=c['kind'];good=False;reason=''
   if kind=='task':
    good=r['id'] in {'n01','n07','n09','n13','n14','n15','n16'}
    if mode=='live' and variant=='candidate' and r['id']=='n11':good=True
    if r.get('error') or (r.get('firstAudioMs') is not None and r['firstAudioMs']<0):good=False
    if good:passed.append(r)
    elif r['id'] in {'n11','n12'}:reason='window omitted; clarification or an invented replacement is a task failure.'
    else:reason='Target omitted or substituted; answering a different word is not success.'
   elif kind=='missing-target':good='没有听清' in r.get('text','');reason='Must clarify the genuinely missing target.'
   elif kind=='noise':good=not r.get('recognized') and not r.get('text');reason='VAD must suppress noise.'
   elif kind=='mandarin-word':
    good=False;reason='ASR preserves Chinese language, but 书包 is answered with English; 台灯 was misheard as 抬灯. Neither is a fully correct meaning explanation.'
   reviews.append(dict(mode=mode,variant=variant,id=r['id'],repeat=r['repeat'],kind=kind,rubric=c['rubric'],recognized=r.get('recognized'),answer=r.get('text'),taskPass=good,reason=reason,targetRetained=retained(r,c) if c.get('target') else None,wrongTarget=substitution(r,c) if c.get('target') else None,output=r.get('output'),error=r.get('error')))
  cold=[r for r in main if r.get('timing',{}).get('modelLoadMs',0)>1000]
  summary[mode][variant]=dict(mainTrials=len(main),taskPass=len(passed),mixedTrials=len(mixed),targetsRetained=sum(retained(r,cases[r['id']]) for r in mixed),wrongTargetTrials=sum(substitution(r,cases[r['id']]) for r in mixed),allResponsesLatency=latency(main),successfulOnlyLatency=latency(passed),withoutModelColdLoadLatency=latency([r for r in main if r not in cold]),coldLoads=[dict(id=r['id'],repeat=r['repeat'],modelLoadMs=r['timing']['modelLoadMs'],firstAudioMs=r['firstAudioMs']) for r in cold],repairs=sum(bool(r.get('timing',{}).get('exampleRepairRequests') or r.get('timing',{}).get('languageRepairRequests')) for r in main),errors=sum(bool(r.get('error')) for r in data['rows']),noiseClean=sum(cases[r['id']]['kind']=='noise' and not r.get('recognized') and not r.get('text') for r in data['rows']),windowCorrect=sum(retained(r,cases[r['id']]) for r in main if r['id'] in {'n11','n12'}))
summary['decision']='Rejected as default: no offline retention gain, false insertion dance from Mandarin 单词, and unstable window recovery. Dialogue weights remain qwen2.5:3b.'
(PACK/'review.json').write_text(json.dumps(reviews,ensure_ascii=False,indent=2)+'\n');(PACK/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n');print(json.dumps(summary,ensure_ascii=False,indent=2))
