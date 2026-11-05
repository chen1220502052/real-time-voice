# 3B 中文识别、播报与例句修复

> 2026-11-04 后续核查：此前合成混说录音的音量裁剪可能损伤英语短词的弱辅音。下列语音成绩保留为历史诊断，不应直接视作自然混说识别率或与完整波形新成绩比较。文字偏好、身份等独立回归结论不因此失效。完整波形与最新默认链路结果见 [后续报告](/Users/chenqiankun/Documents/real-time-voice/docs/30-code-switch-voice-evaluation.md)。

继续使用 qwen2.5:3b 对话模型，没有微调。改动是本地语音组件、词表路由和有上限的同模型补全；作文与文字模型的选择保持原配置。

Parakeet 保留英语识别，实时英语草稿不额外运行中文模型。停顿后的完整快照加入 tiny Whisper 语言判断；普通话或原英语识别被拒绝时，调用已缓存的 multilingual Whisper large-v3-turbo。原来的噪声置信度过滤仍保留，不通过把置信度写成 1 来放行。中文完整转写参与原来的端点判断。

Kokoro 使用同一 82M 权重，中文改走 Misaki 中文分词和 zf_xiaobei 音色，英文保留 af_heart。混合回答分语言送入发音组件；完整英文短语及其数字保留在英文段。中文单词释义不再交给英文发音器逐字读成 Chinese letter。

词义快捷回复遇到例句、造句或 make/write a sentence 等复合任务会交给 3B 完整回答。单词问法先去掉 the word 等包装，并支持中文“的意思”。未要求的例句括号翻译不再挤掉第二个例句。需要补全时最多增加一次同 3B 的结构化请求，检查英文例句数量与可播报句数；失败明确报错，不发布只有释义的残缺答案。问句形式的例句不会让第二个例句被提前截断。没有可靠词表释义的词不编造例句。原来的偏好与 AI 年龄保护继续生效。

## 本机准备

当前本机所需权重和中文依赖已经缓存。重新安装环境时，TTS 环境需要 misaki[zh]，并缓存 hexgrad/Kokoro-82M 的 voices/zf_xiaobei.pt；英语 voice 仍是 af_heart.pt。ASR 环境需要 mlx-whisper 和缓存的 whisper-large-v3-turbo，以及固定版本 mlx-community/whisper-tiny（78c52ab98ca87f570bc57ad852e15ef7060f9f76）。工作进程保持离线加载，不在通话中下载模型。

LOCAL_ASR_MULTILINGUAL 默认 1；LOCAL_ASR_LANGUAGE=en 仍用于英语主路径，中文回退不被它强制成英文。KOKORO_VOICE_ZH 可选择已缓存的中文 voice 文件。MLX 与 Torch Kokoro 两种实现使用相同语言路由。本次正式音频测试使用 MLX，Torch 仅安装中文依赖并通过组件测试。

## 验证口径与结果

本机 Apple M4 Pro / 24GB，Ollama qwen2.5:3b Q4_K_M；ASR 为 Parakeet int8 + tiny 语言判断 + Whisper turbo 回退，TTS 为 Kokoro MLX BF16。独立输入使用 macOS Tingting 普通话、Samantha/Daniel 英语，中英混说为分别合成后拼接。不是儿童实录或独立音色泛化测试。

| 测试口径 | 修改前完成 | 修改后完成 | 修改后首段音频 P50 / P95 |
| --- | --- | --- | --- |
| 完整音频、已知结束边界：12 个完整任务，各测两遍 | 4/24，16.7% | 18/24，75.0% | 全部回应 662 / 820 ms；成功任务 646 / 844 ms |
| 新实时录音：6 个任务，每 100ms 按真实时钟送入 VAD，各测两遍 | 0/12 | 8/12，66.7% | 全部回应 992 / 1712 ms；成功任务 914 / 1758 ms |

实时计时从输入话音结束到服务端首段 PCM 返回，包含端点、识别、3B 回答与 TTS，不含浏览器缓冲、麦克风链路或实际扬声器起音。离线口径不含端点。不能把它们当成同一个延迟指标，也不能因修改前多数请求没有正确回答，就把其较低返回时间说成速度优势。分位数采用线性插值，小样本仅描述本机本轮测试。

离线独立集另有 1 个故意不含目标词的问题，用于检查安全澄清，不放入任务完成率分母。两种口径的静音、低噪声控制均不产生回复。离线中文整句完成 8/8、英语提问完成 8/8、中英混说仅 2/8；新实时集中文 4/4、英语 4/4、中英混说 0/4。重复两遍不算额外独立问题。

**中文整句识别和中文播报已可用，中英混说尚未解决到可验收的程度。** 例如 milk、kind、careful、raincoat、elephant 在这些混合合成录音中仍可能被省略。不能通过把参考答案补进转写来提高分数；缺词时会澄清，澄清在完整任务中仍计失败。这一瓶颈在识别端，不能用微调 3B 来掩盖。

实时新集两次使用了一次有上限的例句补全（2/12 任务试次）。离线集没有触发补全。没有 9B 改写，也没有模型训练。实际输出 WAV 经过 Whisper 交叉转写；对 doctor、farmer、clever 这类双语录音额外保留固定中文/英文转写，避免自动语言判断漏掉中文或英文部分。转写器与中文回退使用同一权重，属于组件核对，不是独立人工音质评分。

相关测试为 123 passed、1 skipped。本地服务已经在空闲时更新，2026-11-04 14:03:58 Asia/Shanghai 启动，健康且无活跃会话；ASR 预热约 2.39 秒，TTS 约 5.69 秒，测试计时排除预热。ASR 工作进程 MLX 活跃内存从约 0.786GB 增至 2.480GB；这是模型活跃内存，不是整机 RSS。此轮只更新本地服务，没有部署到 ECS。

## 端点修复与独立性

实际实时诊断录音在词义问句后有约 300ms 停顿，再继续请求例句。原先 stable_transcript 在 300ms 时结束本轮，造成半个请求进入 Bot，甚至得到相对于完整话音结束为负的“首音延迟”。这些全部算提前截断失败，不能列为速度成绩。词义/例句类任务现在保留至少 500ms 的端点窗口；普通对话保留原来的短窗口。对应假时钟回归测试和原始录音回归结果单独保存。

最终离线集首次推理时已经冻结当前 ASR worker、提示词、grounding 和 Bot 的源码。本轮最后只改动了端点策略，离线 runner 直接发送整段 PCM，不调用该策略。summary.json 核验：其余源码 SHA-256 不变；撤销该端点代码块后 provider 的 SHA-256 与离线冻结版本完全一致。实时部分另外新建六题，全部源码在推理前重新冻结。最终测试之后不再根据输出改产品。

此前 24、20、16 题都已转为诊断集，原始失败没有删除。第一批 baseline 还存在冻结模块 DATA 路径错误，整批 baseline 排除；之后修正评测脚本，完整重跑对照。第二、三批暴露的漏词编造目标词、中文“哪一种”被忽略、材料年龄添写等问题已经分别加入保护和回归测试。不能将这些用于调试的数据再称为独立验收集。

另试了 [Sherpa 官方 SenseVoice 接口](https://k2-fsa.github.io/sherpa/onnx/sense-voice/pretrained.html) 的 int8 模型，在诊断录音中仍漏掉部分混合短词，因此没有切换生产路径；本轮最终结果始终使用同一套 Parakeet/Whisper/Kokoro 链路。

## 记录与录音

- 最终离线集：[cases.json](/Users/chenqiankun/Documents/real-time-voice/evals/bilingual_voice_final/cases.json)。当前目录的 live 结果是端点修复前的诊断结果，不参与最终实时成绩。
- 最终实时集：[cases.json](/Users/chenqiankun/Documents/real-time-voice/evals/bilingual_voice_live_release/cases.json)、[统计与源码核验](/Users/chenqiankun/Documents/real-time-voice/evals/bilingual_voice_live_release/summary.json)、[逐题审阅](/Users/chenqiankun/Documents/real-time-voice/evals/bilingual_voice_live_release/review.json)。
- [中文身份播报](/Users/chenqiankun/Documents/real-time-voice/.local/bilingual-voice-live-release/candidate/b03-live-0.wav)、[中文释义与两个英文例句](/Users/chenqiankun/Documents/real-time-voice/.local/bilingual-voice-live-release/candidate/b02-live-0.wav)。
- [源码差异](/Users/chenqiankun/Documents/real-time-voice/evals/bilingual_voice_live_release/baseline-to-candidate.patch)。
