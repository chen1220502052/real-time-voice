"""Package existing pronunciation assets and reviewed textbook excerpts, without model calls.

Local ASR and TTS are prohibited. Missing samples must be generated through GPT
cloud speech and imported with provider/model provenance before packaging.
Published metadata is source-SHA bound; runtime never synthesizes audio.
"""
import hashlib
import json
import re
from pathlib import Path
from backend.chapter_practice import ChapterLibrary

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'backend/data/practice'
RESOURCES = ROOT / '英语'
def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def key(word): return ' '.join(word.casefold().split())

def apply_excerpt_overrides(metadata, overrides):
    """Keep reviewed dialogue boundaries when rebuilding silence candidates.

    Silence detection cannot distinguish a section announcement or task instruction
    from dialogue. Overrides are bound to the original audio digest, never an order
    number or a universal number of seconds to skip.
    """
    import math
    replacements = {}
    for sha, entry in overrides['recordings'].items():
        if sha not in metadata:
            raise ValueError('Reviewed excerpt source is missing: ' + sha)
        duration = entry.get('duration', metadata[sha]['duration'])
        if (not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0
                or abs(duration - metadata[sha]['duration']) > max(2, metadata[sha]['duration'] * .01)):
            raise ValueError('Reviewed source duration differs: ' + sha)
        clips = entry['clips']
        previous_end = 0
        for clip in clips:
            start, end = clip['start'], clip['end']
            if (not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (start, end))
                    or start < previous_end or end <= start or end > duration + .05
                    or end - start > 18.05):
                raise ValueError('Invalid reviewed excerpt: ' + sha)
            previous_end = end
        replacements[sha] = (clips, duration)
    for sha, (clips, duration) in replacements.items():
        metadata[sha]['clips'] = clips
        metadata[sha]['duration'] = duration

def pause_clips(path, maximum=18):
    """Split only at quiet gaps >= 240ms; never cut a continuous utterance to fit."""
    import numpy as np
    import soundfile as sf
    samples, rate = sf.read(path, dtype='float32', always_2d=True)
    mono = samples.mean(axis=1)[::4]; rate = rate / 4
    hop = max(1, round(rate * .02)); n = len(mono) // hop
    if not n: return []
    rms = np.sqrt(np.mean(mono[:n * hop].reshape(n, hop) ** 2, axis=1))
    threshold = min(.012, max(.002, float(np.percentile(rms, 85)) * .09))
    quiet = rms < threshold
    cuts = []
    begin = None
    for i, value in enumerate(quiet):
        if value and begin is None: begin = i
        if begin is not None and (not value or i == n - 1):
            end = i if not value else i + 1
            if (end - begin) * .02 >= .24: cuts.append((begin * .02, end * .02))
            begin = None
    voiced = []
    cursor = 0.
    for start, end in cuts:
        if start - cursor >= .25: voiced.append((max(0, cursor - .10), min(len(samples)/rate/4, start + .12)))
        cursor = end
    duration = len(samples) / (rate * 4)
    if duration - cursor >= .25: voiced.append((max(0, cursor-.10), duration))
    result = []
    for start, end in voiced:
        if end - start > maximum: continue  # Music/continuous long speech is listen-only.
        if result and end - result[-1][0] <= 10 and start - result[-1][1] < .8:
            result[-1][1] = end
        else: result.append([start, end])
    return [{'start':round(a,2),'end':round(b,2)} for a,b in result if b-a >= .4]

def build():
    import soundfile as sf
    lib = ChapterLibrary(RESOURCES)
    dictionary = {}
    source = json.loads((RESOURCES/'科普版/初中本地课文词表索引.json').read_text())
    for book in source:
        for group in book['word_groups']:
            for word in group['words']:
                path = RESOURCES/'科普版'/word['local_audio']
                if path.is_file(): dictionary.setdefault(key(word['word']), path)
    words = sorted({key(w['word']) for c in lib.chapters.values() for w in c['vocabulary']})
    previous_path = DATA/'word-audio.json'
    previous = json.loads(previous_path.read_text()).get('words', {}) if previous_path.exists() else {}
    entries = {}
    for i, word in enumerate(words):
        path = dictionary.get(word); kind = 'recorded'
        if path is None:
            kind = 'synthetic'
            imported = previous.get(word, {})
            if (imported.get('provider') != 'openai' or not imported.get('model')
                    or imported.get('kind') != 'synthetic'):
                raise RuntimeError('GPT cloud pronunciation required for '+word+'; local TTS is disabled')
            path = (RESOURCES/imported['path']).resolve()
            if (not path.is_relative_to(RESOURCES.resolve()) or not path.is_file()
                    or digest(path) != imported.get('sha256')):
                raise ValueError('Imported GPT pronunciation is missing or changed: '+word)
        info = sf.info(path)
        entries[word] = {'path':str(path.relative_to(RESOURCES)), 'sha256':digest(path), 'kind':kind,
                         'duration':round(info.duration,2)}
        if kind == 'synthetic':
            for name in ('provider','model','voice','generationMethod','generatedAt'):
                if name in imported: entries[word][name] = imported[name]
        if i % 250 == 0: print('words',i,'/',len(words),flush=True)
    (DATA/'word-audio.json').write_text(json.dumps({'version':1,'words':entries},ensure_ascii=False,indent=2))
    metadata = {}; total = sum(len(c['recordings']) for c in lib.chapters.values())
    for c in lib.chapters.values():
        for r in c['recordings']:
            path = lib.audio.get(r['id'])
            if path is None: continue
            sha = digest(path)
            if sha in metadata: continue
            info = sf.info(path)
            full = '整课' in r['title'] or path.name.startswith('00_')
            metadata[sha] = {'duration':round(info.duration,2), 'fullLesson':full,
                             'clips':[] if full or r['kind']!='passage' else pause_clips(path)}
            if len(metadata)%100 == 0: print('tracks',len(metadata),'/',total,flush=True)
    overrides = DATA/'excerpt-overrides.json'
    if overrides.is_file():
        apply_excerpt_overrides(metadata, json.loads(overrides.read_text()))
    (DATA/'audio-excerpts.json').write_text(json.dumps({'version':1,'recordings':metadata},ensure_ascii=False,indent=2))
    print('done',len(entries),'words;',len(metadata),'tracks;',sum(w['kind']=='synthetic' for w in entries.values()),'synthetic',flush=True)

if __name__ == '__main__': build()
