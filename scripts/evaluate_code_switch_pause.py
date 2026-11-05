"""Fresh pause-only acceptance: original ASR, updated nonblocking input path."""
import argparse,asyncio,os
from pathlib import Path
import scripts.evaluate_bilingual_voice as runner
if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--variant',choices=['baseline','candidate'],required=True);parser.add_argument('--repeat',type=int,default=2);args=parser.parse_args();args.mode='live';args.all_live=True
 configure=runner.configure
 def setup():
  configure();os.environ['LOCAL_ASR_MULTILINGUAL_ENGINE']='whisper'
 runner.configure=setup
 runner.ROOT=Path('.local/code-switch-pause');runner.PACK=Path('evals/code_switch_pause')
 asyncio.run(runner.main(args))
