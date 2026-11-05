"""Generate reusable grade-three word samples with local Kokoro, never textbook recordings.

Run with the isolated TTS environment: .local/tts-venv/bin/python -m scripts.build_grade3_word_audio
Model files are downloaded once from the official repository, then inference is local.
The final index is replaced only after all selected words pass audio validation.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = 'hexgrad/Kokoro-82M'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def build(limit=None):
    import numpy as np
    import soundfile as sf
    import torch
    from huggingface_hub import snapshot_download
    from kokoro import KModel, KPipeline

    torch.set_num_threads(4)
    resources = ROOT / '英语'
    data = ROOT / 'backend/data/practice/word-audio.json'
    # Use the audited curriculum vocabulary, not theme-example supplements.
    curriculum = json.loads((resources / '科普版/结构化教材词表.json').read_text())
    words = {}
    for chapter in curriculum['chapters']:
        if chapter.get('grade') != '3年级':
            continue
        for item in chapter['vocabulary']:
            text = item if isinstance(item, str) else item['word']
            words.setdefault(' '.join(text.casefold().split()), text)
    selected = sorted(words)[:limit] if limit else sorted(words)
    folder = resources / '本地示范音/kokoro-af-heart'
    folder.mkdir(parents=True, exist_ok=True)
    checkpoint = folder / 'generation.json'
    saved = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
    model_dir = Path(snapshot_download(REPO, allow_patterns=['config.json', 'kokoro-v1_0.pth', 'voices/af_heart.pt']))
    model_sha = digest(model_dir / 'kokoro-v1_0.pth')
    model = KModel(repo_id=REPO, config=str(model_dir / 'config.json'), model=str(model_dir / 'kokoro-v1_0.pth')).eval()
    pipeline = KPipeline(lang_code='a', repo_id=REPO, model=model)
    voice = str(model_dir / 'voices/af_heart.pt')
    for index, key in enumerate(selected):
        entry = saved.get(key, {})
        previous = resources / entry.get('path', '')
        if (entry.get('modelSha256') == model_sha and entry.get('speed') == .9
                and previous.is_file() and digest(previous) == entry.get('sha256')):
            continue
        text = words[key]
        chunks = list(pipeline(text, voice=voice, speed=.9))
        if not chunks or any(chunk.audio is None or not chunk.phonemes.strip() for chunk in chunks):
            raise ValueError('Missing pronunciation: ' + key)
        samples = np.concatenate([chunk.audio.cpu().numpy() for chunk in chunks])
        duration = len(samples) / 24000
        if not np.isfinite(samples).all() or not .2 <= duration <= 15 or np.max(np.abs(samples)) < .01:
            raise ValueError('Invalid audio: ' + key)
        filename = hashlib.sha256((key + model_sha + 'af_heart:0.9').encode()).hexdigest()[:24] + '.wav'
        path = folder / filename
        # Short margins preserve consonants and avoid abrupt playback starts.
        samples = np.pad(samples, (2400, 3600))
        peak = float(np.max(np.abs(samples)))
        if peak > .95:
            samples *= .95 / peak
        sf.write(path, samples, 24000, subtype='PCM_16')
        saved[key] = {'path': str(path.relative_to(resources)), 'sha256': digest(path),
                      'kind': 'synthetic', 'provider': 'kokoro-local', 'model': REPO,
                      'modelSha256': model_sha, 'voice': 'af_heart', 'speed': .9,
                      'generationMethod': 'local-offline', 'generatedAt': datetime.now(timezone.utc).isoformat(),
                      'duration': round(sf.info(path).duration, 3), 'sampleRate': 24000,
                      'text': text, 'phonemes': ' '.join(c.phonemes for c in chunks)}
        atomic_json(checkpoint, saved)
        if index % 20 == 0:
            print(f'Generated {index + 1}/{len(selected)}', flush=True)
    if not limit:
        pack = json.loads(data.read_text()) if data.exists() else {'version': 1, 'words': {}}
        for key in selected:
            entry = saved[key]
            path = resources / entry['path']
            if digest(path) != entry['sha256'] or sf.info(path).duration < .2:
                raise ValueError('Changed audio: ' + key)
            pack['words'][key] = entry
        atomic_json(data, pack)
    print(json.dumps({'words': len(selected), 'indexed': not bool(limit), 'model': REPO, 'voice': 'af_heart'}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, help='Generate a sample batch without changing the runtime index')
    build(parser.parse_args().limit)
