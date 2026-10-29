"""Summarize a retained paired experiment without rewarding premature replies."""
import argparse
import json
from pathlib import Path
import statistics


def summarize_verified(report):
    pairs = report['pairs']
    groups = {}
    for pair in pairs:
        group = groups.setdefault(pair['fixture'], {'550': [], '400': []})
        for arm in ('550', '400'):
            if arm in pair:
                row = pair[arm]
                group[arm].append({'failures': row['guardrailFailures'],
                                  'asrFinals': row.get('asrFinals', []),
                                  'firstPacketMs': row.get('sourceEndToFirstPacketMs')})
    valid = [p for p in pairs if all(a in p and not p[a]['guardrailFailures'] for a in ('550', '400'))]
    metrics = {}
    for metric in ('sourceEndToFirstPacketMs', 'endpointToFinalMs', 'finalToFirstPacketMs'):
        selected = [p for p in valid if all(p[a].get(metric) is not None for a in ('550', '400'))]
        metrics[metric] = {'n': len(selected)}
        for arm in ('550', '400'):
            values = [p[arm][metric] for p in selected]
            metrics[metric][arm] = statistics.mean(values) if values else None
        differences = [p['550'][metric]-p['400'][metric] for p in selected]
        metrics[metric]['pairedSavingMs'] = statistics.mean(differences) if differences else None
    return {'status': report['status'], 'pairs': len(pairs), 'validPairs': len(valid),
            'allAttempts': {a: {'n': sum(a in p for p in pairs),
                'failed': sum(bool(p[a]['guardrailFailures']) for p in pairs if a in p)} for a in ('550','400')},
            'validPairedLatency': metrics, 'byFixture': groups,
            'guardrails': {a: {'passed': g.get('passed'), 'failureType': g.get('failureType')}
                           for a, g in report.get('guardrails', {}).items()},
            'caveat': 'Latency is paced-fixture end to first packet on ECS loopback, not physical output. '
                      'Latency subset requires both arms to pass; all failures remain in allAttempts.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args=parser.parse_args()
    summary=summarize_verified(json.loads(args.report.read_text()))
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(summary, ensure_ascii=False))


if __name__=='__main__':main()
