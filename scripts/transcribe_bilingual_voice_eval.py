"""Cross-check actual WAV outputs, separately from timed evaluation.

Run with the MLX Whisper environment; never concurrently with latency tests.
This shares ASR weights with the Mandarin fallback and is not human listening.
"""
import argparse,hashlib,json
from pathlib import Path
import numpy as np
import soundfile as sf
import mlx_whisper
from huggingface_hub import snapshot_download
if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--pack',required=True);args=parser.parse_args()
 model=snapshot_download('mlx-community/whisper-large-v3-turbo',local_files_only=True,allow_patterns=['config.json','*.safetensors','*.npz'])
 for path in sorted(Path(args.pack).glob('*.json')):
  if path.stem.endswith('-transcribed'):continue
  data=json.loads(path.read_text())
  if not isinstance(data,dict) or 'rows' not in data:continue
  for row in data['rows']:
   if not row.get('output'):continue
   audio,rate=sf.read(row['output'],dtype='float32');assert rate==16000
   result=mlx_whisper.transcribe(audio,path_or_hf_repo=model,temperature=0,condition_on_previous_text=False,verbose=None)
   row.update(audioTranscript=result['text'].strip(),audioLanguage=result['language'],
     waveformSha256=hashlib.sha256(Path(row['output']).read_bytes()).hexdigest(),
     waveformRms=float(np.sqrt(np.mean(audio*audio))),waveformPeak=float(np.max(np.abs(audio))))
   print(path.stem,row['id'],row['repeat'],row['audioTranscript'],flush=True)
   path.with_name(path.stem+'-transcribed.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
 print('DONE',flush=True)
