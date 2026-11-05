"""Frozen comparison against the previous release, unchanged 3B and ASR weights."""
import argparse,asyncio,os
from pathlib import Path
import scripts.evaluate_bilingual_voice as runner
if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--variant',choices=['baseline','candidate'],required=True);parser.add_argument('--mode',choices=['offline','live'],default='offline');parser.add_argument('--repeat',type=int,default=2);args=parser.parse_args()
 configure=runner.configure
 def setup():
  configure();os.environ['LOCAL_ASR_MULTILINGUAL_ENGINE']='whisper';os.environ['LOCAL_ASR_TARGET_VERIFY']='1'
 runner.configure=setup;runner.ROOT=Path('.local/code-switch-target-final');runner.PACK=Path('evals/code_switch_target_final')
 args.live_ids={'n01','n02','n03','n04','n06','n07','n11','n12','n13','n15','n17','n18','n19','n20','silence','quiet-noise','loud-noise'}
 asyncio.run(runner.main(args))
