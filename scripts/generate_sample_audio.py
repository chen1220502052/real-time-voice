"""Rebuild original sample audio on macOS using the installed Samantha voice.

This uses local system speech synthesis, not a cloud API. Output is labelled as
synthetic throughout the sample course. Requires node, say and afconvert.
"""
import json
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile
import wave

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clip', help='Rebuild one named clip instead of all clips')
    options = parser.parse_args()
    for binary in ('node', 'say', 'afconvert'):
        if not shutil.which(binary):
            raise SystemExit(f'{binary} is required to rebuild the bundled sample audio')
    content = subprocess.check_output(['node', '-e', """
const u=require('./web/learning-data.js');
const clips=Object.fromEntries(u.words.map(w=>[w.audio,w.word+'. '+w.example]));
for(const s of u.sentences)clips[s.audio]=s.text;
clips.dialogue=u.listening.transcript;
clips.story=u.reading.paragraphs.join(' ');
clips.speaking='I usually play badminton with my friends on Saturdays. I like it because it is fun. What do you do on weekends?';
process.stdout.write(JSON.stringify(clips));
"""], cwd=ROOT, text=True)
    clips = json.loads(content)
    if options.clip and options.clip not in clips:
        raise SystemExit(f'Unknown clip: {options.clip}')
    target = ROOT / 'web' / 'audio' / 'weekend'
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='kitty-sample-audio-') as temporary:
        for name, text in clips.items():
            if options.clip and name != options.clip:
                continue
            source = Path(temporary) / f'{name}.aiff'
            if name == 'dialogue':
                # Distinct voices let students identify the two speakers.
                source = Path(temporary) / 'dialogue.wav'
                with wave.open(str(source), 'wb') as output:
                    output.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                    for index, line in enumerate(text.splitlines()):
                        speaker, speech = line.split(': ', 1)
                        voice = 'Samantha' if speaker == 'Mia' else 'Daniel'
                        fragment = Path(temporary) / f'line-{index}.aiff'
                        pcm = Path(temporary) / f'line-{index}.wav'
                        subprocess.run(['say', '-v', voice, '-r', '145', '-o', str(fragment), speech], check=True)
                        subprocess.run(['afconvert', '-f', 'WAVE', '-d', 'LEI16@24000', '-c', '1', str(fragment), str(pcm)], check=True)
                        with wave.open(str(pcm), 'rb') as audio:
                            output.writeframes(audio.readframes(audio.getnframes()))
                        output.writeframes(b'\0' * 2 * 7200)
            else:
                subprocess.run(['say', '-v', 'Samantha', '-r', '145', '-o', str(source), text], check=True)
            destination = target / f'{name}.m4a'
            # afconvert does not overwrite existing outputs.
            if destination.exists():
                destination.unlink()
            subprocess.run(['afconvert', '-f', 'm4af', '-d', 'aac', str(source), str(destination)], check=True)
    (target / 'manifest.json').write_text(json.dumps({
        'source': 'Original Kitty English sample; local synthetic speech',
        'voice': 'macOS Samantha; dialogue: Mia=Samantha, Leo=Daniel', 'rate': 145, 'clips': clips,
    }, ensure_ascii=False, indent=2) + '\n')
    print(f'Generated {1 if options.clip else len(clips)} original synthetic recordings in {target}')


if __name__ == '__main__':
    main()
