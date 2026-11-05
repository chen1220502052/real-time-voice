"""Explicit experiment entry point around the unchanged local speech worker."""
import contextlib
import os
import sys


def main():
    from scripts import local_speech_worker as worker
    with contextlib.redirect_stdout(sys.stderr):
        if os.getenv('EXPERIMENTAL_TTS_ALIGNMENT') == 'vectorized':
            from scripts.experimental_tts_alignment import install_vectorized_alignment
            install_vectorized_alignment()
    original = worker.load_tts

    def load():
        infer = original()

        def seeded(request):
            if 'experimentSeed' in request:
                import mlx.core as mx
                mx.random.seed(int(request['experimentSeed']))
            return infer(request)
        return seeded

    worker.load_tts = load
    worker.main()


if __name__ == '__main__':
    main()
