"""Browser client only: fixed-threshold recording checks against remote lab."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


def review(analysis):
    row={k:analysis[k] for k in ('wavSignalLatencyMs','correctedGraphLatencyMs',
        'wavMinusCorrectedMs','earlyGraphReplies','asrFinalCount','userReplyCount','errors')}
    row['passed']=(not row['earlyGraphReplies'] and not row['errors']
        and row['asrFinalCount']==1 and row['userReplyCount']==1
        and row['wavMinusCorrectedMs'] is not None and abs(row['wavMinusCorrectedMs'])<=2)
    return row


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:18876')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--fixture-dir', type=Path, required=True)
    parser.add_argument('--reanalyze', action='store_true', help='Offline only; preserve original recordings')
    args=parser.parse_args()
    if args.reanalyze:
        from browser_recorder_test import analyze_recording
        rows=json.loads((args.output_dir/'summary.json').read_text())
        for row in rows:
            case=args.output_dir/f'{Path(row["fixture"]).stem}-{row["arm"]}'
            logs=list(case.glob('*-log.json'))
            if not logs:continue
            assert len(logs)==1
            analysis=analyze_recording(logs[0]);analysis['fixture']=row['fixture']
            (case/'analysis.json').write_text(json.dumps(analysis,indent=2)+'\n')
            row.update(review(analysis));row['passed']=row['passed'] and row.get('exitCode')==0
        (args.output_dir/'summary.json').write_text(json.dumps(rows,indent=2)+'\n')
        print(json.dumps(rows));return
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if (args.output_dir/'summary.json').exists():
        parser.error('Use a fresh evidence directory')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    # Wait for the cloud-only harness to finish all paired and recovery cases.
    # The control route rejects changes while any lab call is active.
    rows=[]
    for fixture,arms in [('en_hello_16k.wav',(550,400)),('controlled_pause300_16k.wav',(400,550))]:
        for arm in arms:
            request=urllib.request.Request(args.url+'/__endpoint_test/arm',
                data=json.dumps({'endpointMs':arm}).encode(),headers={'Content-Type':'application/json'})
            with opener.open(request,timeout=10) as response:
                control=json.load(response)
            assert control['endpointMs']==arm
            case=args.output_dir/f'{Path(fixture).stem}-{arm}'
            case.mkdir()
            result_path=case/'analysis.json'
            try:
                process=subprocess.run([sys.executable,str(Path(__file__).with_name('browser_recorder_test.py')),
                    '--url',args.url+'/', '--fixture',str(args.fixture_dir/fixture),
                    '--download-dir',str(case), '--output',str(result_path)],
                    text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=150)
            except subprocess.TimeoutExpired:
                row={'fixture':fixture,'arm':arm,'passed':False,'error':'browser_timeout'}
                rows.append(row)
                (args.output_dir/'summary.json').write_text(json.dumps(rows,indent=2)+'\n')
                print(json.dumps(row),flush=True)
                continue
            # Child output contains only test UI/network diagnostics, no auth.
            print(process.stdout,flush=True)
            row={'fixture':fixture,'arm':arm,'exitCode':process.returncode}
            if result_path.exists():
                analysis=json.loads(result_path.read_text())
                row.update(review(analysis));row['passed']=row['passed'] and process.returncode==0
            else:row['passed']=False
            rows.append(row)
            (args.output_dir/'summary.json').write_text(json.dumps(rows,indent=2)+'\n')
            print(json.dumps(row),flush=True)
            time.sleep(1)


if __name__=='__main__':main()
