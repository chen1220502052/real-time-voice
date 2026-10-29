"""Compare old/new packs on fixed synthetic cases, never call a real model.

Rates describe this scenario suite, not measured production traffic. Latency
covers hot local bot routing (no HTTP/ASR/TTS/session-journal or model latency).
"""
import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import re
import statistics
import tempfile
import time
from types import SimpleNamespace
from backend.chat_library import ChatLibrary,DATA,_load_library
from backend.curriculum_chat import CurriculumChatBot
from backend.llm_runtime import Timing,use_timing
from backend.text_bot import BotMessage,BotRequest

ROOT=Path(__file__).resolve().parents[1]
def summary(values):
    ordered=sorted(values)
    return {'samples':len(values),'p50Ms':round(statistics.median(values),4),
            'p95Ms':round(ordered[max(0,int(len(ordered)*.95+.999)-1)],4),'maxMs':round(max(values),4)} if values else {}

async def evaluate(repo,cases):
    db=__import__('sqlite3').connect(repo.path)
    ids=[r[0] for r in db.execute('SELECT id FROM chapters ORDER BY id')]
    coverage={r[0]:{'eligibleAnswers':r[1]} for r in db.execute('SELECT chapter_id,count(*) FROM eligible_answers GROUP BY chapter_id')}
    origins=dict(db.execute('SELECT origin,count(*) FROM answers GROUP BY origin'));db.close()
    counts=Counter();kinds={};latencies=[];units=[];details=[]
    class NoNetwork:
        async def ainvoke(self,state,config):
            return {'messages':[SimpleNamespace(content='I can help with that.')]}
    for chapter in ids:
        scope=repo.scope(chapter)
        bot=CurriculumChatBot(lambda _:NoNetwork(),lambda _:scope,repo)
        unitcounts=Counter()
        for index,case in enumerate(cases):
            history=tuple(BotMessage('user' if i%2==0 else 'assistant',t) for i,t in enumerate(case['history']))+(BotMessage('user',case['user']),)
            q=BotRequest('bench_'+str(index),'bench',str(index),messages=history,
                options={'grade':int(re.search(r'\d+',scope.chapter['grade'])[0]),'chapterId':chapter})
            probe=Timing(q.request_id);first=None;start=time.perf_counter()
            with use_timing(probe):
                async for e in bot.stream(q):
                    if e.type=='text_delta' and first is None:first=(time.perf_counter()-start)*1000
            source=probe.metrics['chatReplySource'];cache=source in {'textbook','prepared','generated_cache'}
            counts['turns']+=1;counts['cacheHits']+=cache;counts['policy']+=source=='policy';counts['modelRequired']+=source=='generated'
            counts['unavailableTextbook']+=source=='textbook_unavailable'
            unitcounts['turns']+=1;unitcounts['cacheHits']+=cache;unitcounts['modelRequired']+=source=='generated'
            kind=kinds.setdefault(case['kind'],Counter());kind['turns']+=1;kind['cacheHits']+=cache;kind['modelRequired']+=source=='generated'
            if case['kind']=='must_fallback':counts['unsafeMatches']+=cache
            if cache:latencies.append(first)
            if chapter=='d2435143bdba0792312a2ecc':
                details.append({'id':case['id'],'user':case['user'],'source':source,'firstTextMs':round(first,4)})
        units.append({'chapterId':chapter,**{k:scope.chapter[k] for k in ('grade','edition','unit','publisher')},
                      **coverage.get(chapter,{'eligibleAnswers':0}),**unitcounts})
    return {'counts':dict(counts),'cacheHitRate':round(counts['cacheHits']/counts['turns'],5),
            'modelRequiredRate':round(counts['modelRequired']/counts['turns'],5),
            'cacheLatency':summary(latencies),'origins':origins,'byKind':{k:dict(v) for k,v in kinds.items()},
            'units':units,'selectedUnitCases':details,'stats':repo.stats()}

async def main(args):
    cases=json.loads(args.cases.read_text())['cases'];library=_load_library(os.getenv('ENGLISH_RESOURCE_ROOT',ROOT/'英语'))
    with tempfile.TemporaryDirectory(prefix='chat-benchmark-') as folder:
        reports={}
        for name,extra in [('baseline',{}),('expanded',{'bulk_path':DATA/'bulk-replies.json','sentences_path':DATA/'textbook-sentences.json'})]:
            started=time.perf_counter()
            repo=ChatLibrary.build(library,Path(folder)/(name+'.sqlite3'),**extra)
            buildms=round((time.perf_counter()-started)*1000,2)
            reports[name]=await evaluate(repo,cases);reports[name]['buildMs']=buildms
        assert reports['expanded']['counts']['unsafeMatches']==0
        assert reports['expanded']['counts']['cacheHits']>reports['baseline']['counts']['cacheHits']
        report={'passed':True,'method':'Synthetic fixed scenarios on every selectable unit; same routing code, baseline=44 old replies, expanded=old+bulk. No network/model calls. Not production hit rate.',
                'caseCount':len(cases),'baseline':reports['baseline'],'expanded':reports['expanded']}
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({k:{field:v[field] for field in ['counts','cacheHitRate','modelRequiredRate','cacheLatency','stats','buildMs']} for k,v in reports.items()},ensure_ascii=False))
if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases',type=Path,default=ROOT/'tests/fixtures/chat/retrieval-evaluation.json')
    parser.add_argument('--output',type=Path,default=ROOT/'.local/bulk-chat-benchmark.json')
    asyncio.run(main(parser.parse_args()))
