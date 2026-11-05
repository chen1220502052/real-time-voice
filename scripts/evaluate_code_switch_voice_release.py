"""Paired evaluation of the frozen mixed-language target-word holdout.

Dialogue is always qwen2.5:3b; only the multilingual ASR route and the quoted
word guard differ. Both variants use identical dependency versions and audio.
"""
import argparse,asyncio,os
from pathlib import Path
import scripts.evaluate_bilingual_voice as runner

if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--variant',choices=['baseline','candidate'],required=True)
 parser.add_argument('--mode',choices=['offline','live'],default='offline')
 parser.add_argument('--repeat',type=int,default=2)
 args=parser.parse_args()
 configure=runner.configure
 def setup():
  configure()
  os.environ['LOCAL_ASR_PYTHON']=str(Path('.local/code-switch-asr-venv/bin/python').absolute())
  os.environ['LOCAL_ASR_MULTILINGUAL_ENGINE']='qwen3' if args.variant=='candidate' else 'whisper'
 runner.configure=setup
 runner.ROOT=Path('.local/code-switch-release');runner.PACK=Path('evals/code_switch_voice_release')
 args.live_ids={'r01','r03','r06','r07','r08','r11','r13','r15','r17','r18','r19','silence','quiet-noise','loud-noise'}
 asyncio.run(runner.main(args))
