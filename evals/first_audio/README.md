# Qwen3.5 4B 首段语音实验

这组实验沿用上一轮的原生 MLX affine 4 bit 模型、固定系统前缀缓存和冻结产品源码，只测试首段分段、合成任务调度与 TTS 分配器缓存预算。回答提示词、模型精度、输出上限、ASR、声音、速度、音量和中文交付校验均保持对照条件一致。默认服务配置不写入实验参数。

## 对照条件

| 标识 | 分段与调度 | TTS 分配器缓存上限 |
| --- | --- | ---: |
| `3b_actual` | 现有 3B，使用实际对话的 SpeechBuffer | 128 MB |
| `native_legacy` | 原生 4B，使用上一轮评测直接调用的 take_speech_segments | 128 MB |
| `native_actual` | 原生 4B，使用实际对话的 SpeechBuffer | 128 MB |
| `native_priority` | 同 native_actual，首段合成期间让 4B 在 token 间暂停，完成后立即恢复 | 512 MB |
| `native_tuned` | 同 native_priority，额外尝试三个稳定词的首段，最长等待 80 ms；数字陈述等保持完整 | 512 MB |
| `native_priority_smooth` | 后续质量修正版，原首段不变，保护后续短尾词，有播放余量时最多聚合 180 ms | 512 MB |

`native_legacy` 到 `native_actual` 是评测口径校正，不能计为新增产品优化。两个文件在本轮开始时均与当前工作区相同，实际产品已经使用 SpeechBuffer。后续真实应用播放还包含 VAD、网络、浏览器、扬声器缓冲等开销，本实验从完整输入 PCM 和已知结束边界计至首段输出 PCM 就绪。

## 输入和质量核对

20 条既有录音与 8 条新增 macOS Samantha 合成录音，每条件运行两轮。每块使用相同输入、相同固定种子乱序，模型块顺序正向再反向。每块重新加载模型，准备固定指令前缀并预热；用于音频预热的 q35-01 排除在主要延迟统计之外。主要统计每条件 54 次；另保留新问题 27 次、第二轮 27 次以及新增 8 题的分组结果。

原生 4B 使用相同的每题每轮随机种子，使分段条件的回答可逐条比较。种子只在实验 worker 中设置。所有进入 TTS 的片段拼接后必须与 Bot 交付文字一致；保留片段原文、就绪时间、PCM 哈希、合成时间和连续播放缓冲估计。合成成功、失败或取消时，父进程均在 finally 中恢复原生 worker。内核中已经开始的计算会先完成，暂停发生在解码 token 之间。

所有题目均属于开发或回归材料，新增题的参照回复已用于分段安全检查，不作为未查看盲测。回答是否保持相同、录音内容是否可恢复和真人听感是不同指标；自动转写不能替代 MOS 或儿童实际会话评价。

## 产物和复现

- `results.json`：原始结果、文字事件时间线、语音片段及准备时间。
- `summary.json`：分组延迟、文字一致性、成对耗时差、波形和源码验证。
- `frozen.json`：冻结源码、实验差异和本轮开始时的默认配置哈希。
- `cases.json`：可复现题目，英语输入文件由 `prepare` 在执行副本生成。
- `tts-alignment-profile.json`：未选用的音素对齐改写试验，30 对、60 次同文本推理；中位只省约 1.5 ms，未作为主要结果的提速方案。
- `smooth-results.json`、`smooth-summary.json`、`frozen-smooth.json`：短尾词质量修正后的独立 112 次前后交错复测。第一块修正版 28 次保留自同源码同设置的上一轮，接着对照 0、对照 1、修正版 1，组成 ABBA。
- `smooth-audio-verification.json`：最终 112 份独立转写，8 份中英混合回复另强制中文。候选 54 份核心内容获得支持，2 份待听辨；不作为真人音质评分。

执行目录为 `.local/qwen35-first-audio/workspace`；在此用原工作区 `.venv/bin/python -m scripts.evaluate_first_audio --outer /Users/chenqiankun/Documents/real-time-voice --output /path/to/results.json` 运行。需要已有的本地模型和语音虚拟环境。脚本检测其他 `evaluate_*` / `transcribe_*` 进程，先等待其结束，发现计时重叠则排除后重跑。被资源争用影响的 `contended-run.json` 只作诊断，不参与正式延迟结论。

产品代码保持冻结副本的行为。所有新增分段与调度仅通过 `scripts/experimental_*` 及本轮评测入口启用，没有接入默认应用路由。

本轮没有写入 `.env.local`。原工作区配置在其他并行工作期间有外部变化，记录前后哈希但不回滚；实际语音 LLM 仍为 3B。测量始终使用固定副本，所有实验语音参数均经复核。
