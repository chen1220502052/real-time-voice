"""Cooperative first-audio priority for the isolated native MLX child only."""
import argparse
import signal
import sys
import time
from pathlib import Path


def main():
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    import mlx_lm
    from scripts import local_llm_mlx_worker
    paused=False

    def pause(signum, frame):
        nonlocal paused
        paused=True

    def resume(signum, frame):
        nonlocal paused
        paused=False

    signal.signal(signal.SIGUSR1,pause)
    signal.signal(signal.SIGUSR2,resume)
    generate=mlx_lm.stream_generate

    def gated(*args,**kwargs):
        for item in generate(*args,**kwargs):
            # Finish in-flight native work, then wait between decoded tokens.
            # Python signal handlers continue running during this short wait.
            while paused:time.sleep(.001)
            yield item

    mlx_lm.stream_generate=gated
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',required=True)
    local_llm_mlx_worker.main(parser.parse_args())


if __name__=='__main__':main()
