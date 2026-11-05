"""Fresh independent paced live-speech acceptance after the endpoint fix."""
import argparse,asyncio
from pathlib import Path
import scripts.evaluate_bilingual_voice as evaluate
if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--variant',choices=['baseline','candidate'],required=True)
 parser.add_argument('--repeat',type=int,default=2)
 args=parser.parse_args();args.mode='live';args.all_live=True
 evaluate.ROOT=Path('.local/bilingual-voice-live-release');evaluate.PACK=Path('evals/bilingual_voice_live_release')
 asyncio.run(evaluate.main(args))
