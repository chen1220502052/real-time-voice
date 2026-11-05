# Local speech-to-speech comparison

Twelve original English prompts cover identity, explicit preferences and corrections, word meanings and a compound example request, contextual senses, history recall, an invitation, a fictional character, a short follow-up and a topic switch. Three additional Mandarin prompts probe Chinese speech input for meanings, identity and preference strength. Inputs were synthesized offline with macOS Samantha for English and Eddy for Mandarin, then converted to mono PCM16 at 16 kHz. The case manifest includes input hashes. Historical turns are text in each system. This is a controlled synthetic test and contains no student recordings.

The current pipeline uses the project Parakeet int8 worker, EnglishChatBot voice path with Qwen2.5-3B, the normal speech chunker and Kokoro MLX with its exact audio cache disabled. The baseline includes its existing deterministic fact and meaning routes. Candidates receive equivalent tutoring instructions and supplied history but have their own speech understanding and generation.

The common turn-based timer starts when the complete input waveform and its known utterance boundary are available. It includes preprocessing, inference and waveform generation. It excludes microphone transport, endpoint detection, speaker playback buffers, download, load and warmup. Native streaming audio is measured at the first received PCM event. The stock Qwen Transformers path returns the full waveform, so its first waveform time also includes completion of generation. Keep that runtime limitation visible when comparing numbers.

Two measured repeats follow one warmup. Independent Whisper-large-v3-turbo transcription of output waveforms checks that the audible response carries the requested content. Transcription is fallible, so retain waveforms and distinguish generated-text checks from audio checks. No human listening-panel score is claimed. Full-duplex interaction uses a separate timed sequence and is reported separately from the turn-based timing.

Pinned model revisions, source revisions, local runtime logs and generated WAV files are kept in .local/omni-eval/. The focused candidates are Qwen2.5-Omni-3B and MiniCPM-o-4.5 Q4_K_M.

Final scope: baseline and MiniCPM each completed 30 requests. The Qwen Transformers 5.22 diagnostic matrix also completed 30 waveform generations with EOS, but poor waveform content prevents using these as validated speech latency or ranking quality. Official-system controls on Transformers 4.53.3 reproduce the MPS issue. Moving only Token2Wav to CPU restores audible content on three diagnostic probes, with 15.6–28.3 s full-waveform return. These controls are recorded separately.

Exclude o13 from semantic rankings because independent input transcription hears perch rather than the intended peach. Its runtime record is retained.

The MiniCPM test copy changes the server binding to honor --host and passes the provided system message through the TTS template. The exact patch is embedded in results.json. The adapter supplies history as explicitly marked context because the upstream audio-only turn serializer omits text history. Results before these adapter corrections remain under .local/omni-eval/ as diagnostic history.

Full local adapters are .local/omni-eval/baseline.py, mini_eval.py, qwen_eval.py, qwen_official_system_smoke.py, qwen_cpu_vocoder_smoke.py and transcribe.py. The native duplex controls use duplex_recheck.py with an absolute reference-audio path. Results are summarized in docs/28-omni-voice-evaluation.md.
