"""Opt-in Kokoro alignment experiment; never edits the installed package."""
import hashlib
import inspect
import textwrap


def install_vectorized_alignment():
    import numpy as np
    from mlx_audio.tts.models.kokoro.kokoro import Model

    original = Model.__call__
    source = textwrap.dedent(inspect.getsource(original))
    start = source.index('    indices_list = []\n')
    end = source.index('    pred_aln_trg = ', start)
    replacement = '''    # One duration transfer and one integer repeat, retaining the exact
    # original integer alignment and all subsequent neural operations.
    counts = _alignment_np.clip(_alignment_np.asarray(pred_dur), 0, max_frames_per_phoneme)
    index_values = _alignment_np.repeat(_alignment_np.arange(len(counts), dtype=_alignment_np.int32), counts)
    if not index_values.size:
        silence = mx.zeros((1, 1))
        return self.Output(audio=silence, pred_dur=pred_dur) if return_output else silence
    indices = mx.array(index_values)
'''
    # Fail closed if the installed implementation has changed.
    expected = '''    indices_list = []
    for i, n in enumerate(pred_dur):
        count = min(max(int(n), 0), max_frames_per_phoneme)
        if count > 0:
            indices_list.append(mx.repeat(mx.array(i), count))
    if not indices_list:
        silence = mx.zeros((1, 1))
        return (
            self.Output(audio=silence, pred_dur=pred_dur)
            if return_output
            else silence
        )
    indices = mx.concatenate(indices_list)
'''
    if source[start:end] != expected:
        raise RuntimeError('Installed Kokoro alignment differs from the inspected implementation')
    namespace = dict(original.__globals__, _alignment_np=np)
    exec(compile(source[:start] + replacement + source[end:], '<experimental-kokoro-alignment>', 'exec'), namespace)
    Model.__call__ = namespace['__call__']
    return original, {'originalCallSha256': hashlib.sha256(source.encode()).hexdigest(),
                      'replacementSha256': hashlib.sha256(replacement.encode()).hexdigest()}
