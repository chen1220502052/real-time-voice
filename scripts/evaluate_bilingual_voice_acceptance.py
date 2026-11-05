"""Evaluate the final independent corpus; keep product sources frozen."""
import argparse,asyncio
from pathlib import Path
import scripts.evaluate_bilingual_voice as evaluate
if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--variant',choices=['baseline','candidate'],required=True)
 parser.add_argument('--mode',choices=['offline','live'],default='offline')
 parser.add_argument('--repeat',type=int,default=2)
 evaluate.ROOT=Path('.local/bilingual-voice-acceptance');evaluate.PACK=Path('evals/bilingual_voice_acceptance')
 asyncio.run(evaluate.main(parser.parse_args()))
