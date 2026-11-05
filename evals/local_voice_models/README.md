# Local Qwen voice model comparison

Compares `qwen2.5:3b`, `qwen3.5:2b`, and `qwen3.5:4b` on the same Mac and a frozen copy of current voice application code. No service configuration is changed, no ECS is accessed, no cloud judge is used, and no student data is used. The downloaded default 2B tag is Q8_0; 3B and 4B are Q4_K_M. This is a comparison of the actual downloaded variants, not a controlled quantization experiment.

Run from the repository root after downloading both candidate tags:

```bash
.venv/bin/python -m scripts.compare_local_voice_models
```

The final run executes from `.local/qwen35-comparison/frozen-workspace/` with the original repository's virtualenv Python and an absolute `--output` path pointing to this directory's `results.json`. The snapshot copies application code/data and uses only whitelisted speech settings; it contains no cloud keys or account credentials. `frozen.json` verifies source stability during capture and hashes all copied sources. The earlier live-workspace run is retained only as `.local/qwen35-comparison/preliminary-workspace-results.json`: workspace source files changed concurrently during that run, so its results are excluded from the final ranking.

The corpus combines 24 task-specific questions with the 40 existing shipping regression questions. Two task-specific questions were used for harness smoke checks; the first audio rendering of question 02 was clarified before measurement because two recognizers heard `each` as `age`. Input preparation history is retained. These are synthetic evaluation scenarios, not a representative sample of children's conversations. Repeated requests are not independent samples. Existing regression questions are reported separately from task-specific questions. No prompts or product logic are tuned during this comparison.

The text phase exercises actual `EnglishChatBot` and `CurriculumChatBot` voice paths, including current identity, preference, grounded meanings, language checks, and reply validation. Retrieval is disabled with the existing read-only evaluation adapter, so answer-library hits do not conceal model calls. Deterministic replies are retained and identified separately from generated replies. All three candidates use temperature 0.3, 4096 context, at most 256 output tokens, and `think=false` through the existing Ollama adapter.

The audio phase uses 12 synthesized English questions and three Mandarin capability probes. English input uses macOS Samantha; Mandarin uses Eddy. History is supplied as text. The complete 16 kHz PCM input is sent through the existing Parakeet worker, confidence gate, actual English voice Bot, normal speech chunker, and Kokoro MLX worker. Kokoro speed is 0.95 and volume is 0.2; its exact audio cache is disabled. The timer starts with the full input waveform and known end boundary available and ends at the first PCM response. It includes ASR, response generation, and TTS, and excludes live VAD, microphone transport, browser scheduling, playback buffers, model loading, and warmup. It must not be presented as microphone-to-speaker latency. Mandarin probes are reported separately from English ranking.

Each phase has two measured repeats, using model blocks ABC then CBA and seeded shuffled question order. A candidate is warmed before its block; other comparison models are unloaded. All requests are serial, with normal overlap between response generation and queued TTS. The app's original 3B is loaded again on exit. Cold/warm preparation records, model digests, quantization details, source hashes, exact responses, provider calls, and timing are retained in `results.json`. WAV inputs, responses, and diagnostics live under `.local/qwen35-comparison/`.

Rule checks are inherited from the existing evaluator. They detect selected required facts, language, formatting and length constraints; they cannot establish complete semantic correctness. Manual assistant review of delivered text and independent Whisper transcription of generated WAVs are recorded separately. No blind human listening panel, MOS score, pronunciation assessment, statistical significance or production SLA is claimed.

`frozen.json` pins the input corpus and relevant code before the measured run. The result records before/after hashes of `.env.local`, the startup script, and product sources to verify that defaults and application logic remain unchanged.
