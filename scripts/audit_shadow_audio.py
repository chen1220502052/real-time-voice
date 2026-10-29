"""Resumable, local-only textbook ASR; never publishes candidates automatically.

Install optional offline dependencies in a dedicated environment. Download a
CTranslate2 Whisper model first, then pass its local directory. No credentials,
network service, application database, or production files are used by this job.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import time


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    os.replace(temporary, path)


def transcribe(inventory: Path, output: Path, model_path: Path, workers: int, threads: int) -> None:
    from faster_whisper import WhisperModel, BatchedInferencePipeline
    assert model_path.is_dir(), 'Pass an already downloaded local model directory'
    model_sha = digest(model_path / 'model.bin')
    model = WhisperModel(str(model_path), device='cpu', compute_type='int8',
                         cpu_threads=threads, num_workers=workers, local_files_only=True)
    rows = json.loads(inventory.read_text())['recordings']
    rows.sort(key=lambda r: (not bool(r['existingClips']), r['fullLesson'], r['duration'] or 0))
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    statistics = {'total': len(rows), 'done': 0, 'failed': 0, 'cached': 0, 'audioSeconds': 0.0}

    def process(row):
        source = Path(row['path'])
        sha = row['sha256']
        target = output / (sha + '.json')
        if target.is_file():
            cached = json.loads(target.read_text())
            if cached.get('sha256') == sha and cached.get('modelSha256') == model_sha and cached.get('status') == 'complete':
                return 'cached', cached['duration'], sha
        begun = time.monotonic()
        try:
            assert digest(source) == sha, 'Audio changed after inventory'
            print(json.dumps({'event':'start','sha256':sha}),flush=True)
            import soundfile as sf
            from scipy.signal import resample_poly
            from math import gcd
            samples,rate=sf.read(source,dtype='float32',always_2d=True)
            mono=samples.mean(axis=1)
            if rate!=16000:
                divisor=gcd(rate,16000)
                mono=resample_poly(mono,16000//divisor,rate//divisor)
            pipeline = BatchedInferencePipeline(model=model)
            needs_words=not row['fullLesson'] and row['kinds']!=['words']
            segments, info = pipeline.transcribe(mono, language='en', beam_size=1, batch_size=8,
                word_timestamps=needs_words, vad_filter=True, vad_parameters={'min_silence_duration_ms': 400},
                condition_on_previous_text=False, temperature=0, hallucination_silence_threshold=2)
            result = []
            for segment in segments:
                result.append(asdict(segment))
            data = {'version': 1, 'status': 'complete', 'sha256': sha, 'modelSha256': model_sha,
                    'engine': 'faster-whisper/int8/cpu', 'language': 'en', 'duration': info.duration,
                    'wordTimestamps':needs_words,
                    'decoder':'libsndfile/scipy-resample-poly',
                    'elapsed': round(time.monotonic() - begun, 2), 'segments': result}
            atomic_json(target, data)
            return 'done', info.duration, sha
        except Exception as error:
            atomic_json(output / (sha + '.error.json'), {'sha256': sha, 'error': str(error),
                        'type': type(error).__name__})
            return 'failed', 0, sha

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process, row): row for row in rows}
        for future in as_completed(futures):
            status, duration, sha = future.result()
            statistics[status] += 1
            statistics['audioSeconds'] += duration
            statistics['elapsed'] = round(time.monotonic() - started, 1)
            atomic_json(output.parent / 'progress.json', statistics)
            print(json.dumps({'status': status, 'sha256': sha, **statistics}), flush=True)
    atomic_json(output.parent / 'transcription-result.json', statistics)
    if statistics['failed']:
        raise SystemExit('Recognition failures remain; production data was not modified')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--threads', type=int, default=3)
    args = parser.parse_args()
    transcribe(args.inventory, args.output, args.model, args.workers, args.threads)


if __name__ == '__main__':
    main()
