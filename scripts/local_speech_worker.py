"""Persistent offline inference worker. JSON lines on stdin/stdout, diagnostics on stderr."""
import argparse
import base64
import contextlib
import json
import os
from pathlib import Path
import sys
import time
import struct
import math
import re
from collections import OrderedDict

CODE_SWITCH_ASR_CONTEXT = '请逐字转写音频。中英混说时保留英语词原有的英文拼写，不要翻译、遗漏或替换。'

def accepted_asr_text(result):
    """Discard uncertain decoder guesses on room noise, without word blacklists."""
    minimum=float(os.getenv('LOCAL_ASR_MIN_LOGPROB','-1.0'))
    segments=[s for s in result.get('segments',[])
              if s.get('avg_logprob',-10)>minimum and s.get('no_speech_prob',1)<.6]
    return ''.join(s['text'] for s in segments).strip()


def trim_tts_leading_silence(pcm):
    """Keep quiet consonants, with 30 ms of padding before their first samples."""
    # A lone low-amplitude sample in Kokoro's prefix is not a consonant.
    # Use 10 ms energy windows instead of preserving that entire prefix.
    for offset in range(0,len(pcm),320):
        frame=pcm[offset:offset+320]
        energy=sum(value*value for (value,) in struct.iter_unpack('<h',frame))
        if energy>=64*(len(frame)//2):
            return pcm[max(0,offset-960):]
    return pcm


def accepted_parakeet_text(result):
    """Use model token confidence, including for short answers; no word blacklist."""
    tokens=result.tokens
    confidence=math.exp(sum(math.log(max(1e-10,t.confidence)) for t in tokens)/len(tokens)) if tokens else 0
    minimum=float(os.getenv('LOCAL_PARAKEET_MIN_CONFIDENCE','.9'))
    return {'text':result.text.strip() if confidence>=minimum else '', 'confidence':confidence}


def configure_parakeet_precision(model, bits):
    """Keep convolutions in BF16; quantize eligible dense layers only.

    Four-bit weights regressed isolated-word recognition in the local corpus.
    The serving path deliberately supports only the validated 8/16-bit modes.
    """
    if bits not in {8,16}:
        raise ValueError('LOCAL_PARAKEET_QUANT_BITS must be 8 or 16')
    if bits == 8:
        from mlx import nn
        nn.quantize(model,bits=8,group_size=64,class_predicate=lambda path,layer:
            isinstance(layer,(nn.Linear,nn.Embedding)) and layer.weight.shape[-1]%64==0)


def load_parakeet():
    import mlx.core as mx
    import numpy as np
    from huggingface_hub import snapshot_download
    from parakeet_mlx import from_pretrained
    from parakeet_mlx.audio import get_logmel
    model_name=os.getenv('LOCAL_PARAKEET_MODEL','mlx-community/parakeet-tdt-0.6b-v3')
    revision=os.getenv('LOCAL_PARAKEET_REVISION') or (
        'ed2b7e8c15f9aaa0b5772e2efb986255eaef7e15' if model_name=='mlx-community/parakeet-tdt-0.6b-v3' else None)
    path=Path(model_name).expanduser()
    if not path.is_dir():path=Path(snapshot_download(model_name,revision=revision,local_files_only=True,
        allow_patterns=['config.json','model.safetensors']))
    mx.set_default_device(mx.gpu)
    cache_limit=int(os.getenv('LOCAL_ASR_MLX_CACHE_MB','128'))*1024*1024
    mx.set_cache_limit(cache_limit)
    model=from_pretrained(str(path))
    quantization_bits=int(os.getenv('LOCAL_PARAKEET_QUANT_BITS','16'))
    configure_parakeet_precision(model,quantization_bits)
    mx.eval(model.parameters())
    model.generate(get_logmel(mx.zeros(16000),model.preprocessor_config))
    mx.clear_cache()
    multilingual = os.getenv('LOCAL_ASR_MULTILINGUAL', '1').lower() not in {'0', 'false', 'off'}
    fallback = detect_language = None
    if multilingual:
        from mlx_whisper.load_models import load_model
        from mlx_whisper.audio import log_mel_spectrogram, pad_or_trim, N_FRAMES
        detector_path = snapshot_download(
            'mlx-community/whisper-tiny', revision='78c52ab98ca87f570bc57ad852e15ef7060f9f76',
            local_files_only=True, allow_patterns=['config.json', '*.npz', '*.safetensors'])
        detector = load_model(detector_path, dtype=mx.float16)
        multilingual_engine = os.getenv('LOCAL_ASR_MULTILINGUAL_ENGINE', 'whisper')
        if multilingual_engine not in {'whisper', 'qwen3'}:
            raise ValueError('LOCAL_ASR_MULTILINGUAL_ENGINE must be whisper or qwen3')
        fallback = load_qwen_asr() if multilingual_engine == 'qwen3' else load_asr(language='auto')

        def detect_language(samples):
            mel = log_mel_spectrogram(samples, n_mels=detector.dims.n_mels)
            mel = pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16)
            _, probabilities = detector.detect_language(mel)
            return max(probabilities, key=probabilities.get)

        detect_language(np.zeros(16000, dtype=np.float32))

    def transcribe(request):
        raw=base64.b64decode(request['pcm'],validate=True)
        if not raw or len(raw)%2 or len(raw)>16000*2*35:raise ValueError('Invalid ASR audio length')
        samples=np.frombuffer(raw,dtype='<i2').astype(np.float32)/32768
        result=model.generate(get_logmel(mx.array(samples),model.preprocessor_config))[0]
        accepted=accepted_parakeet_text(result)
        accepted['engine'] = 'parakeet'
        # Live English partials stay inexpensive. Quiet/final snapshots also
        # identify Mandarin, including confident but wrong English hypotheses.
        if multilingual and request.get('multilingual', True):
            language = detect_language(samples)
            if language == 'zh' or not accepted['text']:
                verify = (multilingual_engine == 'whisper' and
                          os.getenv('LOCAL_ASR_TARGET_VERIFY', '0').lower() not in {'0', 'false', 'off'})
                from backend.asr_target import parakeet_words
                accepted = fallback({**request, 'language': 'zh' if language == 'zh' else 'auto',
                                     'word_timestamps': verify,
                                     'target_hypotheses': parakeet_words(result.tokens)})
                accepted.setdefault('engine', 'whisper')
                words = accepted.pop('_words', [])
                if verify and accepted['text'] and accepted.get('language') == 'zh':
                    from backend.asr_target import verify_target

                    def decode_word(start, end):
                        # Context padding is silence, not a synthesized word or
                        # a hotword prompt. Keep the original waveform intact.
                        crop = samples[int(start * 16000):int(end * 16000)]
                        crop = np.pad(crop, (3200, 3200))
                        from mlx_whisper.decoding import decode, DecodingOptions
                        mel = log_mel_spectrogram(crop, n_mels=detector.dims.n_mels)
                        mel = pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16)
                        decoded = decode(detector, mel, DecodingOptions(
                            language='en', without_timestamps=True, sample_len=16))
                        score = float(decoded.avg_logprob)
                        reliable = (math.isfinite(score) and score <= 0 and
                                    score > float(os.getenv('LOCAL_ASR_MIN_LOGPROB', '-1.0')) and
                                    decoded.no_speech_prob < .6 and len(decoded.tokens) < 16)
                        return {'text': decoded.text.strip() if reliable else '',
                                'confidence': math.exp(score) if reliable else 0,
                                'engine': 'whisper-tiny'}

                    accepted = verify_target(accepted, words, result.tokens, decode_word)
            else:
                accepted['language'] = language
        if mx.get_cache_memory()>cache_limit:mx.clear_cache()
        accepted['memory']={'activeBytes':mx.get_active_memory(),'cacheBytes':mx.get_cache_memory(),
                            'quantizationBits':quantization_bits}
        return accepted
    return transcribe


def scored_qwen_transcription(model, samples, *, max_tokens=256):
    """Keep actual decoder probabilities; never invent a confidence for ASR.

    mlx-audio 0.6.4's STTOutput omits scores, while stream_generate exposes
    them. This adapter runs serially in the existing isolated ASR worker.
    Ignore the auto-language header when measuring transcription confidence.
    """
    original = model.stream_generate
    tokens, logprobs = [], []

    def capture(*args, **kwargs):
        for token, probabilities in original(*args, **kwargs):
            token_id = int(token)
            tokens.append(token_id)
            logprobs.append(float(probabilities[token_id]))
            yield token, probabilities

    model.stream_generate = capture
    try:
        result = model.generate(samples, language=None, max_tokens=max_tokens,
                                temperature=0, verbose=False, system_prompt=CODE_SWITCH_ASR_CONTEXT)
    finally:
        model.stream_generate = original
    if len(tokens) >= max_tokens:
        raise ValueError('Multilingual ASR reached its token limit before completion')
    raw = model._tokenizer.decode(tokens, skip_special_tokens=True)
    marker = '<asr_text>'
    if not raw.startswith('language ') or marker not in raw:
        return {'text': '', 'confidence': 0, 'language': None, 'engine': 'qwen3-asr'}
    prefix = raw[:raw.index(marker) + len(marker)]
    count = len(model._tokenizer.encode(prefix, add_special_tokens=False))
    scores = logprobs[count:]
    valid = bool(scores) and all(math.isfinite(score) and score <= 0 for score in scores)
    confidence = math.exp(sum(scores) / len(scores)) if valid else 0
    text = result.text.strip()
    minimum = math.exp(float(os.getenv('LOCAL_ASR_MIN_LOGPROB', '-1.0')))
    languages = result.language if isinstance(result.language, list) else [result.language]
    detected = next((language for language in languages if language), None)
    language = {'Chinese': 'zh', 'English': 'en', 'Cantonese': 'yue'}.get(detected, detected)
    return {'text': text if confidence >= minimum else '', 'confidence': confidence if text else 0,
            'language': language, 'engine': 'qwen3-asr', 'generationTokens': len(tokens)}


def load_qwen_asr():
    """Small bilingual recognizer, separate from the unchanged 3B dialogue LLM."""
    import mlx.core as mx
    import numpy as np
    from huggingface_hub import snapshot_download
    from mlx_audio.stt import load
    name = os.getenv('LOCAL_CODE_SWITCH_MODEL', 'mlx-community/Qwen3-ASR-0.6B-8bit')
    revision = os.getenv('LOCAL_CODE_SWITCH_REVISION') or (
        '89e96d92ba34aca20b3e29fb10cc284097d1219f' if name == 'mlx-community/Qwen3-ASR-0.6B-8bit' else None)
    path = Path(name).expanduser()
    if not path.is_dir():
        path = Path(snapshot_download(name, revision=revision, local_files_only=True,
            allow_patterns=['*.json', '*.txt', '*.safetensors']))
    loaded = load(str(path))
    # The pinned MLX adapter wraps the decoder. Capture the actual decoder's
    # stream, rather than assigning a method to a non-forwarding outer wrapper.
    model = loaded._model
    if not callable(getattr(model, 'stream_generate', None)):
        raise RuntimeError('Installed mlx-audio is incompatible with the Qwen ASR score adapter')
    scored_qwen_transcription(model, np.zeros(16000, dtype=np.float32), max_tokens=32)
    mx.clear_cache()

    def transcribe(request):
        raw = base64.b64decode(request['pcm'], validate=True)
        if not raw or len(raw) % 2 or len(raw) > 16000 * 2 * 35:
            raise ValueError('Invalid ASR audio length')
        samples = np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768
        # The live VAD uses this same energy floor. Do not ask an autoregressive
        # decoder to turn silence/quiet room noise into plausible words.
        if float(np.sqrt(np.mean(samples * samples))) < float(os.getenv('LOCAL_VAD_MIN_RMS', '.006')):
            return {'text': '', 'confidence': 0, 'language': None, 'engine': 'qwen3-asr'}
        # Leave decoder language automatic even when the utterance routes from
        # Mandarin detection: a mixed sentence must retain its English words.
        return scored_qwen_transcription(model, samples)
    return transcribe


def load_asr(*, language=None):
    import mlx.core as mx
    import mlx_whisper
    import numpy as np
    from huggingface_hub import snapshot_download
    model=os.getenv('LOCAL_ASR_MODEL','mlx-community/whisper-large-v3-turbo')
    path=Path(model).expanduser()
    if not path.is_dir():path=Path(snapshot_download(model,local_files_only=True,
        allow_patterns=['config.json','*.safetensors','*.npz']))
    mx.set_default_device(mx.gpu)
    options=dict(path_or_hf_repo=str(path),word_timestamps=False,condition_on_previous_text=False,
                 temperature=0,verbose=None)
    mlx_whisper.transcribe(np.zeros(16000,dtype=np.float32),language='en',**options)

    def transcribe(request):
        raw=base64.b64decode(request['pcm'],validate=True)
        if not raw or len(raw)%2 or len(raw)>16000*2*35:raise ValueError('Invalid ASR audio length')
        samples=np.frombuffer(raw,dtype='<i2').astype(np.float32)/32768
        selected = request.get('language') or language or os.getenv('LOCAL_ASR_LANGUAGE','en')
        result=mlx_whisper.transcribe(samples,language=None if selected=='auto' else selected,
                                     **options)
        text = accepted_asr_text(result)
        # Alignment is expensive. Ordinary Mandarin chat must not pay for a
        # target-word repair, nor should it alter the decoder's text/segments.
        from backend.asr_target import needs_verification
        aligned = (bool(request.get('word_timestamps', False)) and text and
                   result.get('language') == 'zh' and
                   needs_verification(text, request.get('target_hypotheses', [])) and
                   len(samples) <= 28 * 16000 and
                   all(segment.get('seek', 0) == 0 for segment in result.get('segments', [])))
        if aligned:
            from mlx_whisper.audio import log_mel_spectrogram, pad_or_trim, N_FRAMES
            from mlx_whisper.transcribe import ModelHolder
            from mlx_whisper.tokenizer import get_tokenizer
            from mlx_whisper.timing import add_word_timestamps
            decoder = ModelHolder.get_model(str(path), mx.float16)
            mel = log_mel_spectrogram(samples, n_mels=decoder.dims.n_mels)
            frames = min(int(mel.shape[-2]), N_FRAMES)
            mel = pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16)
            tokenizer = get_tokenizer(decoder.is_multilingual,
                                      num_languages=decoder.num_languages, language='zh', task='transcribe')
            add_word_timestamps(segments=result['segments'], model=decoder, tokenizer=tokenizer,
                                mel=mel, num_frames=frames, last_speech_timestamp=0)
        segments = [segment for segment in result.get('segments', []) if segment.get('text', '').strip()]
        confidence = math.exp(sum(segment.get('avg_logprob', -10) for segment in segments) / len(segments)) if segments else 0
        accepted = {'text':text, 'confidence':confidence if text else 0, 'language':result.get('language')}
        if aligned:
            accepted['_words'] = [word for segment in result.get('segments', [])
                                  for word in segment.get('words', [])]
        return accepted
    return transcribe


def tts_parameters(request):
    text=request['text']
    if not isinstance(text,str) or not 1<=len(text)<=2000:raise ValueError('Invalid TTS text')
    speed=float(request.get('speed',1));volume=float(request.get('volume',.2))
    if not .5<=speed<=2 or not 0<=volume<=1:raise ValueError('Invalid TTS parameters')
    return text,speed,volume


def tts_language_segments(text):
    """Keep English phrases intact and send Mandarin and its numbers to ZHG2P."""
    if not re.search(r'[\u4e00-\u9fff]', text):
        return [('a', text)]
    result = []
    position = 0
    english = r"[A-Za-z]+(?:['’\-][A-Za-z]+)*(?:[ \t]+(?:[A-Za-z]+(?:['’\-][A-Za-z]+)*|\d+(?:\.\d+)?))*"
    for match in re.finditer(english, text):
        if match.start() > position:
            result.append(('z', text[position:match.start()]))
        result.append(('a', match[0]))
        position = match.end()
    if position < len(text):
        result.append(('z', text[position:]))
    merged = []
    for language, segment in result:
        if not re.search(r'[A-Za-z0-9\u4e00-\u9fff]', segment):
            if merged:
                prior_language, prior = merged[-1]
                merged[-1] = (prior_language, prior + segment)
            continue
        if merged and merged[-1][0] == language:
            merged[-1] = (language, merged[-1][1] + segment)
        else:
            merged.append((language, segment))
    return merged


def cache_tts(infer,*,max_bytes=8*1024*1024,max_entries=64):
    """Exact-input audio reuse; bounded process memory, never written to disk."""
    cache=OrderedDict();used=0
    def synthesize(request):
        nonlocal used
        key=tts_parameters(request)
        if key in cache:
            value=cache.pop(key);cache[key]=value
            return {**value,'cacheHit':True,'stageMs':{name:0 for name in value.get('stageMs',{})}}
        result={**infer(request),'cacheHit':False}
        cost=len(result['pcm'])
        if len(key[0])<=160 and cost<=max_bytes:
            while cache and (used+cost>max_bytes or len(cache)>=max_entries):
                _,old=cache.popitem(last=False);used-=len(old['pcm'])
            cache[key]=result;used+=cost
        return result
    return synthesize


def load_mlx_tts():
    """Run the existing Kokoro checkpoint and voice on MLX, without downloads.

    Conversion uses mlx-audio's own weight sanitizer. Keep voice packs cached:
    its high-level generate() clears that cache on every call.
    """
    import gc
    import numpy as np
    import torch
    import mlx.core as mx
    from scipy.signal import resample_poly
    from huggingface_hub import snapshot_download
    from mlx_audio.tts.models.kokoro.kokoro import Model,ModelConfig
    torch.set_num_threads(int(os.getenv('TTS_CPU_THREADS','4')))
    mx.set_default_device(mx.gpu)
    cache_limit=int(os.getenv('LOCAL_TTS_MLX_CACHE_MB','128'))*1024*1024
    mx.set_cache_limit(cache_limit)
    folder=Path(snapshot_download('hexgrad/Kokoro-82M',local_files_only=True,
        allow_patterns=['config.json','kokoro-v1_0.pth','voices/*.pt']))
    voice=folder/'voices'/os.getenv('KOKORO_VOICE','af_heart.pt')
    chinese_voice=folder/'voices'/os.getenv('KOKORO_VOICE_ZH','zf_xiaobei.pt')
    if not voice.is_file() or not chinese_voice.is_file():
        raise FileNotFoundError('Download the configured English and Chinese Kokoro voices first')
    config=json.loads((folder/'config.json').read_text());config['model_path']=str(folder)
    model=Model(ModelConfig.from_dict(config),repo_id='hexgrad/Kokoro-82M')
    checkpoint=torch.load(folder/'kokoro-v1_0.pth',map_location='cpu',weights_only=True)
    weights={module+'.'+(key[7:] if key.startswith('module.') else key):mx.array(tensor.numpy())
        for module,state in checkpoint.items() for key,tensor in state.items()}
    sanitized=model.sanitize(weights);model.load_weights(list(sanitized.items()),strict=True)
    del checkpoint,weights,sanitized;gc.collect()
    model.set_dtype(mx.bfloat16);mx.eval(model.parameters());model.eval()
    pipelines={language:model._get_pipeline(language) for language in ('a','z')}
    voices={'a':voice, 'z':chinese_voice}
    for language,path in voices.items():
        pipelines[language].voices[path.stem]=mx.array(torch.load(path,weights_only=True).numpy())

    def synthesize(request):
        text,speed,volume=tts_parameters(request);begun=time.perf_counter()
        chunks=[]
        for language,part in tts_language_segments(text):
            for segment in pipelines[language](part,voice=voices[language].stem,speed=speed):
                mx.eval(segment.audio)
                chunks.append(np.asarray(segment.audio,dtype=np.float32).reshape(-1))
        generated=time.perf_counter()
        if not chunks:raise ValueError('TTS returned no speech')
        audio=resample_poly(np.concatenate(chunks),2,3)
        audio*=min(3,volume/.2)*float(os.getenv('LOCAL_TTS_GAIN','1'))
        if not np.isfinite(audio).all():raise ValueError('Invalid synthesized samples')
        pcm=trim_tts_leading_silence((np.clip(audio,-.95,.95)*32767).astype('<i2').tobytes())
        if mx.get_cache_memory()>cache_limit:mx.clear_cache()
        return {'pcm':base64.b64encode(pcm).decode(),'sampleRate':16000,'engine':'mlx-bf16',
            'stageMs':{'generation':round((generated-begun)*1000,3),
                'postprocess':round((time.perf_counter()-generated)*1000,3)},
            'memory':{'activeBytes':mx.get_active_memory(),'cacheBytes':mx.get_cache_memory()}}
    synthesize({'text':'Hello!','speed':1,'volume':.2})
    synthesize({'text':'你好。','speed':1,'volume':.2});mx.clear_cache()
    return cache_tts(synthesize) if os.getenv('LOCAL_TTS_CACHE','1')!='0' else synthesize


def load_tts():
    engine=os.getenv('LOCAL_TTS_ENGINE','torch')
    if engine=='mlx':return load_mlx_tts()
    if engine!='torch':raise ValueError('LOCAL_TTS_ENGINE must be torch or mlx')
    return load_torch_tts()


def load_torch_tts():
    import numpy as np
    import torch
    from scipy.signal import resample_poly
    from kokoro import KModel,KPipeline
    from huggingface_hub import snapshot_download
    threads=int(os.getenv('TTS_CPU_THREADS','4'))
    torch.set_num_threads(threads)
    folder=Path(snapshot_download('hexgrad/Kokoro-82M',local_files_only=True,
        allow_patterns=['config.json','kokoro-v1_0.pth','voices/*.pt']))
    pipelines={language:KPipeline(lang_code=language,repo_id='hexgrad/Kokoro-82M',model=False)
               for language in ('a','z')}
    voice=folder/'voices'/os.getenv('KOKORO_VOICE','af_heart.pt')
    chinese_voice=folder/'voices'/os.getenv('KOKORO_VOICE_ZH','zf_xiaobei.pt')
    if not voice.is_file() or not chinese_voice.is_file():
        raise FileNotFoundError('Download the configured English and Chinese Kokoro voices first')
    packs={'a':torch.load(voice,weights_only=True), 'z':torch.load(chinese_voice,weights_only=True)}
    model=KModel(repo_id='hexgrad/Kokoro-82M',config=str(folder/'config.json'),model=str(folder/'kokoro-v1_0.pth')).eval()
    def neural(ps,speed,language):
        with torch.inference_mode():return KPipeline.infer(model,ps,packs[language],speed).audio.cpu().numpy()

    def synthesize(request):
        text,speed,volume=tts_parameters(request);begun=time.perf_counter()
        phonemes=[(language,r.phonemes) for language,part in tts_language_segments(text)
                  for r in pipelines[language](part) if r.phonemes]
        phonemized=time.perf_counter()
        chunks=[neural(ps,speed,language) for language,ps in phonemes]
        generated=time.perf_counter()
        if not chunks:raise ValueError('TTS returned no speech')
        audio=np.concatenate(chunks)
        audio=resample_poly(audio,2,3)  # Kokoro 24 kHz -> websocket 16 kHz.
        # The existing dialogue interface uses .2 as its conversational default.
        audio*=min(3,volume/.2)*float(os.getenv('LOCAL_TTS_GAIN','1'))
        if not np.isfinite(audio).all():raise ValueError('Invalid synthesized samples')
        audio=np.clip(audio,-.95,.95)
        pcm=trim_tts_leading_silence((audio*32767).astype('<i2').tobytes())
        result={'pcm':base64.b64encode(pcm).decode(),'sampleRate':16000,'engine':'torch',
            'stageMs':{'phonemize':round((phonemized-begun)*1000,3),'synthesis':round((generated-phonemized)*1000,3),
                       'postprocess':round((time.perf_counter()-generated)*1000,3)}}
        return result
    synthesize({'text':'Hello!','speed':1,'volume':.2})
    synthesize({'text':'你好。','speed':1,'volume':.2})
    return cache_tts(synthesize) if os.getenv('LOCAL_TTS_CACHE','1')!='0' else synthesize


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind',choices=['asr','tts']);kind=parser.parse_args().kind
    output=sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        infer=(load_parakeet() if os.getenv('ASR_BACKEND')=='parakeet' else load_asr()) if kind=='asr' else load_tts()
    print(json.dumps({'ready':True,'kind':kind}),file=output,flush=True)
    for line in sys.stdin:
        request=json.loads(line)
        begun=time.monotonic()
        try:
            with contextlib.redirect_stdout(sys.stderr):result=infer(request)
            response={'id':request['id'],**result,'inferenceMs':round((time.monotonic()-begun)*1000,2)}
        except Exception as error:
            print(type(error).__name__,file=sys.stderr,flush=True)
            response={'id':request.get('id'),'error':'Local speech inference failed','errorType':type(error).__name__}
        print(json.dumps(response,ensure_ascii=False),file=output,flush=True)


if __name__=='__main__':main()
