"""Run the final fresh corpus with the frozen baseline or current candidate.

Example: .venv/bin/python -m scripts.evaluate_bilingual_voice_release --variant candidate --mode live
Baseline snapshots live in .local/bilingual-voice-release/baseline; the exact
baseline-to-candidate diff and source hashes are retained with the results.
"""
import argparse,asyncio
from pathlib import Path
import scripts.evaluate_bilingual_voice as evaluate
if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--variant',choices=['baseline','candidate'],required=True)
 parser.add_argument('--mode',choices=['offline','live'],default='offline')
 parser.add_argument('--repeat',type=int,default=2)
 args=parser.parse_args()
 evaluate.ROOT=Path('.local/bilingual-voice-release');evaluate.PACK=Path('evals/bilingual_voice_release')
 asyncio.run(evaluate.main(args))
