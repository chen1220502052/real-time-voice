# 第二轮：20 题诊断

本批在发现问题后转为开发诊断，不再用作最终独立验收。原始结果和失败保留。最终离线与实时成绩见 docs/29-3b-bilingual-voice-evaluation.md。

# 3B 双语语音独立复验

2026-11-04。前一批 24 条样本已经转为诊断集。本批是另写的 20 条问题，附静音和低噪声两条控制，首次推理前冻结文本、输入 WAV 的 SHA-256、产品源码及基线源码。测试期间不修改产品，也不根据本批输出调整提示词。历史为明文上下文，最新问题为真正的合成语音。

中文音色为 macOS Tingting，英语为 Samantha/Daniel，中英混说通过分别合成后拼接。所有输入由固定脚本一次生成，无依照候选识别结果筛选样本。相同音频给修改前、修改后链路各测两遍。语音模型始终是 Ollama qwen2.5:3b Q4_K_M，4096 上下文；没有微调、没有使用 9B 改写。两遍重复不算两份独立问题。样本不代表真实儿童、方言、扬声器回声或课堂噪声分布。

离线主集从已知语音结束边界、完整 PCM 可用开始，覆盖 ASR、回答和实际 Kokoro 音频；它不含 VAD。实时子集以每 100ms 一帧按真实时钟送入现有 WebRTC VAD/ParakeetAsr，接着走同一个 Bot 和 TTS，从输入话音结束算首段 PCM 返回，包含端点等待与识别。两种口径都不含浏览器播放缓冲、麦克风传输或扬声器输出，不称为“听见声音”的延迟。保留空回答和异常，计算延迟时必须同时给成功数量。TTS 缓存关闭，单请求顺序测量，模型预热排除。

录音内容核验采用实际输出 WAV 再经 Whisper 转写，并结合作者预先写入的逐题标准审阅回答。此转写器与中文 ASR 回退使用同一 Whisper 权重，属于组件交叉检查，不是独立人工听评，也不提供音质 MOS。Whisper 对中英混合播报本身也有漏词风险；不只根据回答文字声称语音正确。

运行：

```sh
.local/asr-venv/bin/python -m scripts.build_bilingual_voice_release
.venv/bin/python -m scripts.evaluate_bilingual_voice_release --variant candidate --mode offline
.venv/bin/python -m scripts.evaluate_bilingual_voice_release --variant candidate --mode live
```

build 会重建录音并改变文件哈希，不要对已冻结的结果重新生成输入。原始音频及源码快照位于 .local/bilingual-voice-release；本目录保存逐次结果、转写、审阅和统计。baseline-to-candidate.patch 对应实际参与对照的主要源文件差异。最初诊断集的基线脚本错误及原始失败另保留在 evals/bilingual_voice。
