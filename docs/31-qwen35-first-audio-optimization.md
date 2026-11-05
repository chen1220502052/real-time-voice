# Qwen3.5 4B 首段语音优化复测

2026 年 11 月 5 日，Apple M4 Pro、24 GB 统一内存。修正版实验 4B 的首音中位延迟从 522 ms 降至 464 ms，减少约 58 ms；同题配对的中位节省为 54 ms。保留原来的首段文本，优先合成首段声音，并修正后续短语尾部的分段。默认服务未接入这个实验，语音模型仍为 `qwen2.5:3b`。

## 速度与测量口径

| 条件 | 全部热请求首音 P50 | 新问题首音 P50 | 首段 TTS P50 |
| --- | ---: | ---: | ---: |
| 现有 3B 标定 | 440 ms | 421 ms | 115 ms |
| 4B 真实缓冲对照 | 522 ms | 519 ms | 142 ms |
| 4B 首段优先与后续短语修正 | **464 ms** | **459 ms** | **86 ms** |

3B 来自主试验，最后两行来自质量修正后的前后交错复测。每条件 28 条相同输入、两轮；主要延迟统计排除用于预热的 turtle 题，剩 54 次，新问题 27 次。优化后与本轮 3B 标定的中位差约 24 ms；新问题单独看约差 38 ms。设备、题目和运行窗口都会影响差距，不能视为固定额外开销。

最后一组按“修正版 0 轮、对照 0 轮、对照 1 轮、修正版 1 轮”运行。第一块保留自同源码、同设置的已完成测试，参照原始记录和哈希完整保留。54 组成对请求中 49 组更快，平均节省约 50 ms。P95 从 689 ms 降至 676 ms，主要收益发生在中位响应。完整合成中位耗时约 892→889 ms。

首音从完整输入 PCM 及其结束边界可用时计到首段输出 PCM 就绪，包含 ASR、实际语音 Bot、分段与 TTS；不含现场 VAD、浏览器、网络及扬声器缓冲。模型与语音加载、固定指令前缀准备和预热另记。精确音频缓存关闭，TTS 速度 0.95、音量 0.2，模型温度 0.3、thinking 关闭、4096 上下文、256 输出 token，均未为提速缩减。

上一轮评测直接使用 `take_speech_segments`，实际对话则使用已经存在的 `SpeechBuffer`。本轮主试验中，旧入口为 570 ms，真实缓冲为 536 ms；约 35 ms 的同题配对差属于测量口径校正，不能计作新增产品优化。[上一轮报告](30-qwen35-4b-latency-optimization.md)已补充说明。

## 采用的实验改动

首段 TTS 开始后，让本轮创建的原生 MLX 4B 子进程在解码 token 之间短暂等待，首段 PCM 返回后立即恢复。已经开始的计算先完成。成功、失败和取消路径都在 finally 中恢复子进程。这减少了首段合成期间继续生成后文的 GPU 竞争。

TTS 的临时张量分配器缓存上限从 128 MB 调至 512 MB，允许复用更多中间内存；它与缓存问题答案或整段音频不同。模型、量化、声音和合成精度均保持一致。本轮没有单独拆分调度与分配器预算各自贡献了多少毫秒。

首段沿用实际对话的原分段。后续已有至少 600 ms 播放余量时，允许最多 180 ms 的文字聚合，并把 `take off`、`fall away`、所有格、介词和短尾词留在同一短语中。初版首段优先方案曾将 `off.` 单独合成，Whisper 随后漏掉该词；修正版针对这类边界继续核对，不能仅凭拼接后文字相同就判语音质量通过。

纯粹尝试三个词的首段，因数字陈述和句法保护，P50 为 497 ms、P95 为 767 ms，没有优于首段优先方案，未选用。音素时长对齐的向量化改写在 30 对试验中只节省约 1.5 ms，也未选用。

## 回答与录音检查

修正版和对照的 56 组识别输入、完整回答及首段文本全部相同。每题每轮使用相同的实验随机种子，提示词、事实约束和业务拒绝规则不变，所有送入 TTS 的文本拼接后必须保留原文。题目属于开发与回归材料，本轮没有教师盲测；内容一致不代表模型原有的事实错误得到修复。

后续分段修正后的中位语音长度为 8.36 秒，对照为 8.14 秒；中位片段数均为 4。按 PCM 就绪时间和连续播放计算，两个条件均没有超过 1 ms 的播放供给缺口。这是播放缓冲估算，没有进行真人 MOS 测试。

正式证据为主试验 280 次与质量修正复测 112 次，共 392 次请求；预热、开发冒烟和资源争用轮单独保存。波形逐份核验 SHA256、16 kHz 单声道 PCM16、非空和非零 RMS。独立转写与短尾词复核结果保存在下列音频记录中，自动转写的标点、金额格式及中文同音字差异不直接视为漏读。

其中 280 份 4B 回复与 8 条新增输入经独立 Whisper large v3 turbo 转写，20 份中英混合回复另保留强制中文转写。最终修正版 56 份录音中，54 份的核心内容获得转写支持；一份 `mentioned/mention` 尾音差异和一份中英混合回复留待人工听辨，不作音质无损承诺。对照有 3 份待听辨。金额、25 减 3、铅笔与贴纸数量、hot pan 的否定词均保留；初版出现的 `off` 和 `in the rain` 漏词在最终两轮对应转写中均未重现。

可直接听辨[尾音差异录音](/Users/chenqiankun/Documents/real-time-voice/.local/qwen35-first-audio/smooth-workspace/.local/first-audio-output/native_priority_smooth/opt-hold-03-0.wav)和[中英混合录音](/Users/chenqiankun/Documents/real-time-voice/.local/qwen35-first-audio/smooth-workspace/.local/first-audio-output/native_priority_smooth/onset-08-1.wav)。

## 默认配置和复现

本轮没有写入默认配置，没有更改产品后端或访问 ECS。开始与结束的全文件哈希显示，原工作区 `.env.local` 在并行工作期间有外部变化，因此不声明它全文件前后一致，也没有覆盖那项变化。实验始终读取不变的固定副本，语音参数及产品源码经哈希复核；语音 LLM 配置仍为 `qwen2.5:3b`，启动脚本保持原值。

主试验固定副本为 `.local/qwen35-first-audio/workspace`，核验 421 个文件；质量修正副本为 `.local/qwen35-first-audio/smooth-workspace`，核验 422 个文件。脚本会等待其他评测和转写进程结束，发现计时重叠则排除后重跑。资源争用轮未参与性能结论。

在质量修正副本运行原工作区的 `.venv/bin/python -m scripts.evaluate_first_audio --outer /Users/chenqiankun/Documents/real-time-voice --output /path/to/results.json --profiles native_priority_smooth native_actual` 可重测。需要此前已下载并校验的原生 MLX affine 4 bit 权重、原有 Ollama 模板渲染模型及本地语音环境。`native_priority_smooth` 是评测条件，不是可直接填写到默认服务配置中的 Ollama 标签。

- [最终原始结果](../evals/first_audio/smooth-results.json)、[最终汇总与成对比较](../evals/first_audio/smooth-summary.json)、[最终源码冻结](../evals/first_audio/frozen-smooth.json)
- [主试验原始结果](../evals/first_audio/results.json)、[主试验汇总](../evals/first_audio/summary.json)、[运行说明](../evals/first_audio/README.md)
- [主试验独立转写](../evals/first_audio/audio-verification.json)
- [最终独立转写与逐条核对](../evals/first_audio/smooth-audio-verification.json)、[结束状态核验](../evals/first_audio/final-verification.json)
- [实验入口](../scripts/evaluate_first_audio.py)、[解码间等待](../scripts/experimental_native_worker.py)、[后续短语保护](../scripts/experimental_smooth_audio.py)

首段优先、取消恢复、分段文本保留、数字及短尾词保护的 14 项测试通过。实验条件尚未接入默认应用路由。
