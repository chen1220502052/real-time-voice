"""Recheck the isolated native MLX experiment and retain inspectable summaries."""
import collections
import hashlib
import json
import math
from pathlib import Path
import statistics
import wave


ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'evals/voice_optimization'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stats(values):
    values = sorted(values)
    if not values:
        return {'n': 0}
    # Linear interpolation, identical for every condition.
    position = .95 * (len(values) - 1)
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return {'n': len(values), 'p50Ms': round(statistics.median(values), 2),
            'p95Ms': round(values[low] + (values[high] - values[low]) * (position-low), 2)}


def main():
    data = json.loads((DEST / 'native-results.json').read_text())
    assert len(data['rows']) == 648, 'Incomplete run'
    keys = [(r['phase'], r['model'], r['repeat'], r['id']) for r in data['rows']]
    assert len(set(keys)) == len(keys)
    frozen = json.loads((DEST / 'native-frozen.json').read_text())
    workspace = Path(data['executionRoot'])
    mismatches = [name for name, sha in frozen['sourceSha256'].items()
                  if digest(workspace / name) != sha]
    assert not mismatches, mismatches
    defaults = {name: digest(ROOT / name) for name in ['.env.local', 'scripts/run_learning_local.sh']}
    original = frozen['originalDefaultsBefore']
    assert all(defaults[name] == original[name] for name in defaults)
    summary = {'scope': data['scope'], 'models': {},
               'sourceFilesVerified': len(frozen['sourceSha256']), 'defaultHashes': defaults,
               'rawSha256': digest(DEST / 'native-results.json'),
               'firstExposureDefinition': 'repeat 0 after model and static-policy preparation; excludes q35-01 because that fixture is used in audio warmup. Text runs use a separately restarted model block.',
               'latencyDefinition': 'Complete input PCM and known end boundary to first output PCM; excludes live VAD, browser, network and speaker buffering.',
               'referenceText': data.get('referenceText')}
    manifest = {'sourceSha256': summary['rawSha256'], 'rows': []}
    for model in data['models']:
        rows = [r for r in data['rows'] if r['model'] == model]
        text = [r for r in rows if r['phase'] == 'text']
        audio = [r for r in rows if r['phase'] == 'audio']
        assert len(text) == 176 and len(audio) == 40
        item = {'allAudio': stats([r['firstAudioMs'] for r in audio if not r.get('error')]),
                'firstExposureAudio': stats([r['firstAudioMs'] for r in audio if r['repeat'] == 0 and r['id'] != 'q35-01' and not r.get('error')]),
                'repeatAudio': stats([r['firstAudioMs'] for r in audio if r['repeat'] == 1 and r['id'] != 'q35-01' and not r.get('error')]),
                'textModelFirstToken': stats([c['firstTokenMs'] for r in text for c in r['calls'] if c.get('firstTokenMs') is not None]),
                'textApplicationRejections': sum(bool(r.get('error')) for r in text),
                'audioErrors': sum(bool(r.get('error')) for r in audio),
                'regressionRules': collections.Counter('pass' if r['score']['passed'] else 'fail' for r in text if r['set'] == 'regression'),
                'preparation': []}
        for w in data['warmups']:
            if w['model'] != model:
                continue
            p = w.get('prefixPreparation', {})
            item['preparation'].append({'phase': w['phase'], 'repeat': w['repeat'],
                'loadWarmupAndPreparationMs': w['milliseconds'],
                'prefixCount': len(p.get('prefixes', [])),
                'prefixComputeMs': sum(x['milliseconds'] for x in p.get('prefixes', [])),
                'activeMemoryBytes': p.get('activeMemoryBytes')})
        summary['models'][model] = item
        for r in audio:
            path = workspace / r['output']
            assert digest(path) == r['audioSha256']
            with wave.open(str(path)) as wav:
                assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
                frames = wav.readframes(wav.getnframes())
                import array
                samples = array.array('h', frames)
                rms = math.sqrt(sum(x*x for x in samples) / len(samples))
                assert rms > 0
                duration = len(samples) / 16000
            manifest['rows'].append({k: r[k] for k in ['id', 'model', 'repeat', 'text', 'recognized', 'audioSha256']} |
                                    {'output': str(path), 'rms': round(rms, 3), 'seconds': duration})
    (DEST / 'native-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')
    (ROOT / '.local/qwen35-optimization/native-audio-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
