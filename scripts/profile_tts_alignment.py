"""Same-text, same-seed baseline/vectorized Kokoro waveform and latency check."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import time


def main(args):
    os.environ.update(LOCAL_TTS_ENGINE='mlx', LOCAL_TTS_CACHE='0')
    import mlx.core as mx
    from mlx_audio.tts.models.kokoro.kokoro import Model
    from scripts.local_speech_worker import load_mlx_tts
    from scripts.experimental_tts_alignment import install_vectorized_alignment
    infer = load_mlx_tts()
    baseline, metadata = install_vectorized_alignment()
    optimized = Model.__call__
    texts = ['Would you like to draw with me?', 'You can take a train to the library now.',
             "I do not know the colour of Ben's shoes.", 'Nora starts with three pencils.',
             'A raincoat keeps your clothes dry from water.',
             'The price is 3.50 dollars, not 35 dollars.',
             'Dr. Green has twenty five books.', 'I cannot assess your pronunciation.',
             'Cherry 在中文里是樱桃。', '这里的 light 意思是重量轻。']
    rows = []
    for repeat in range(3):
        for index, text in enumerate(texts):
            for mode in (['baseline', 'vectorized'] if repeat % 2 == 0 else ['vectorized', 'baseline']):
                Model.__call__ = baseline if mode == 'baseline' else optimized
                mx.random.seed(17000+index)
                begin = time.perf_counter()
                result = infer({'text': text, 'speed': .95, 'volume': .2})
                row = {'repeat': repeat, 'text': text, 'mode': mode,
                       'wallMs': (time.perf_counter()-begin)*1000,
                       'pcmSha256': hashlib.sha256(result['pcm'].encode()).hexdigest(),
                       'stages': result['stageMs'], 'memory': result['memory']}
                rows.append(row)
                print(mode, repeat, index, round(row['wallMs'], 1), flush=True)
    pairs = []
    for repeat in range(3):
        for text in texts:
            a, b = [next(r for r in rows if r['repeat']==repeat and r['text']==text and r['mode']==mode)
                    for mode in ['baseline', 'vectorized']]
            pairs.append({'repeat': repeat, 'text': text, 'samePcm': a['pcmSha256']==b['pcmSha256'],
                          'savedMs': a['wallMs']-b['wallMs']})
    output = {'metadata': metadata, 'rows': rows, 'pairs': pairs,
              'allPcmIdentical': all(p['samePcm'] for p in pairs),
              'medianSavedMs': statistics.median(p['savedMs'] for p in pairs),
              'medianMs': {mode: statistics.median(r['wallMs'] for r in rows if r['mode']==mode)
                           for mode in ['baseline', 'vectorized']}}
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k:v for k,v in output.items() if k not in ['rows','pairs']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    main(parser.parse_args())
