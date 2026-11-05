"""Ablation: current input/grounding fixes with the original Whisper fallback.

The ASR algorithm/weights remain original. This is a component check on the
same frozen corpus, not a second independent corpus.
"""
import argparse,asyncio,os
from pathlib import Path
import scripts.evaluate_bilingual_voice as runner
if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--mode',choices=['offline','live'],default='live');parser.add_argument('--repeat',type=int,default=1);args=parser.parse_args();args.variant='candidate'
 configure=runner.configure
 def setup():
  configure();os.environ['LOCAL_ASR_MULTILINGUAL_ENGINE']='whisper'
 runner.configure=setup
 runner.ROOT=Path('.local/code-switch-release/frontend-whisper');runner.PACK=Path('evals/code_switch_voice_release/frontend-whisper')
 args.live_ids={'r01','r03','r06','r07','r08','r11','r13','r15','r17','r18','r19','silence','quiet-noise','loud-noise'}
 asyncio.run(runner.main(args))
