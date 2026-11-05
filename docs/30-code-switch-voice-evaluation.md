# 中英混说目标词保护与独立语音评测

> 2026-11-05 后续：新增目标词复核实验未通过独立短词测试，保持关闭；已增加缺词澄清保护。最新结果见 [后续报告](/Users/chenqiankun/Documents/real-time-voice/docs/31-code-switch-target-verification.md)。

2026-11-04，本地 Apple M4 Pro / 24GB。继续使用 qwen2.5:3b Q4_K_M，没有微调，没有换成 9B。最终默认仍为 Parakeet int8 + tiny 语言判断 + Whisper large-v3-turbo；中文/英文播报仍为 Kokoro MLX。新增的 Qwen3-ASR 0.6B 仅作为可选实验，没有启用到本地服务。

本轮修复了英语词前停顿时过早结束收音的问题；普通中英混说的短词漏识、错识仍未达到全面验收标准。测试数据为合成语音，不代表儿童实录表现。

## 已生效的改动

- VAD 开始前保留 500ms 音频，覆盖投票确认语音之前较弱的英语辅音。
- 停顿快照尚在识别时继续接收 PCM，最长等到 900ms 再结束。此前在 500ms 时进入同步等待，会丢失推理期间恢复说出的英语词。对于“请解释”“英语单词”等尚未说完的中文引导语，也等待目标词。
- 引号内的英语词正常参与解释，避免把 `“pig”` 等误判成没有目标词；明确引用的中文词仍可解释。确实没听清目标词时澄清，不能由 3B 猜词。
- 已核验词表能覆盖的单词释义与例句组合任务，用已核验释义校正中文定义，检查例句包含目标词。补全仍至多一次 3B 请求，保持原有身份与偏好保护。

没有把评测参考词注入识别提示词，没有将答案补回转写，没有通过放宽噪声阈值提高成绩。实验 Qwen ASR 只接收音频和固定的逐字转写要求，保留模型真实 token 对数概率与拒绝阈值。

## 最终默认链路：新的停顿专项集

源码和新题目在第一次推理前冻结；修改前后均使用原有 Whisper 路线。四个全新目标词 teacher、window、monkey、music，各重复两遍，在中文引导语后插入 600ms 停顿，再说英语词和中文后缀。每 100ms 按真实时钟送入同一收音流程。

| 指标 | 修改前 | 修改后，最终默认 |
| --- | --- | --- |
| 结束前已接收到完整目标词音频 | 0/8 | 8/8 |
| 转写保留正确英语目标词 | 0/8 | 6/8 |
| 完成原始任务 | 0/8 | 6/8 |
| 全部回应首段 PCM P50 / P95 | 提前截断，不列速度成绩 | 789 / 855 ms |
| 成功任务首段 PCM P50 / P95 | 无成功任务 | 789 / 858 ms |
| 静音、低噪声控制不回复 | 4/4 | 4/4 |

teacher、monkey、music 两次均成功；window 两次均在完整音频已收到的情况下被 Whisper 漏掉，回复澄清，仍计任务失败。不能把“收到目标词音频”当成“识别正确”。此集验证特定停顿缺陷，不是普通中英混说的总体正确率；重复两遍仍只有四个独立问题。

延迟从完整输入话音结束，到服务端首段 PCM 返回，包含端点、识别、3B 和 TTS，不含浏览器缓冲、网络和扬声器起音。预热不计入。原实现的部分结果相对于完整话音结束为负，是过早回答，不能当成低延迟。分位数为线性插值，小样本只描述本轮本机表现。

## 独立普通混说集：新识别器暂不采用

另建立 16 个主任务：12 个中英混说词义任务、2 个中文身份/偏好任务、2 个英文任务；另有中文词解释、真正缺词、静音与噪声控制。独立主任务每题两遍。整段音频一次送入识别，已知结束边界，不包含 VAD；两种识别器使用相同依赖版本和相同输入。

| 整段音频指标 | 原 Whisper | Qwen3-ASR 0.6B 实验 |
| --- | --- | --- |
| 混说目标词英文拼写保留 | 8/24，33.3% | 14/24，58.3% |
| 全任务按释义、例句质量审阅通过 | 16/32，50.0% | 23/32，71.9% |
| 全部回应首段 PCM P50 / P95 | 704 / 1454 ms | 371 / 1136 ms |
| 成功任务首段 PCM P50 / P95 | 685 / 1377 ms | 551 / 1305 ms |

实验能恢复 pen、pig、sun 等部分词，但新增 bed→bad、bag→bad，hat 也仍错成 had；原链路 hat→head 同样失败。football 被翻成“足球”时，语义任务可以完成，但不算保留英文拼写。fun 的一次例句出现不自然的 “Having fun … is very happy”，严格质量计失败。因此不能仅按功能部分齐全声称 75% 质量通过。

两边各触发 2 次有上限的例句修复，没有运行异常；未发生 9B 改写。实验延迟与原链路的任务成功构成不同，且此处没有端点，不能据此声称线上语音更快。

该组还保留一次实时实验及中间版本的 Whisper 收音消融，但它们运行于最终非阻塞收音修复之前，只作诊断，不代表最终默认服务。普通整段音频指标使用的 worker、grounding、提示词与最终版本 SHA-256 一致，只有不参与整段识别的 provider 端点实现后来修改。最终默认版本另用上述全新四题停顿集冻结验证。

实验权重固定为 `mlx-community/Qwen3-ASR-0.6B-8bit`，revision `89e96d92ba34aca20b3e29fb10cc284097d1219f`，权重 SHA-256 为 `b5bfe4abc1b4c6e58b633096682ec2b6297298add1527119936107d211adf0e8`。接口依据 [MLX Audio 官方实现](https://github.com/Blaizzy/mlx-audio/blob/main/mlx_audio/stt/models/qwen3_asr/README.md)。较大的 ASR、整词表上下文与切片重识别的诊断也未稳定解决短词，未接入默认产品。

## 输入完整性与评测限制

本轮排查发现，先前合成录音构建脚本按音量阈值裁剪首尾，仅留下很短的余量，可能损伤低音量英语辅音。这是评测输入问题。旧的 18 题及上一轮相关混说短词结果保留作历史诊断，不能作为自然语音识别率或与本轮完整波形成绩直接比较；不影响文字偏好、身份等独立回归结论。

本轮正式两组输入保留每段源语音的全部重采样 PCM，合并后逐段校验 SHA-256。普通集 22 个 WAV、41 个源段；停顿集 6 个 WAV、12 个源段，均再次校验通过。没有根据识别能否成功挑选或删除输入。普通集为 Tingting 普通话与 Samantha/Daniel 英语分段合成，停顿集英语为 Samantha；拼接音色与自然中英混说仍有差异，需要儿童实录继续验证，当前不应宣称问题已全部解决。

输出 WAV 保存，并用 Whisper 交叉转写核查；该转写也可能遗漏混合播报中的中文段，不能替代人工音质评分。任务评分依据原问题、实际转写、回答和例句要求逐题记录；澄清、错词的正确释义均不算完成。评分清单与所有失败保留在 review.json。

## 验证与本地启用

相关回归 **136 passed、1 skipped**，包括延迟快照期间恢复说话、低能量辅音前置音频、引号内目标词、中文词解释、词义校正及既有偏好/身份保护。最终测试后产品源码未再修改；停顿集冻结文件的全部 11 项源码/词表 SHA-256 核验一致。

2026-11-04 18:32:46 Asia/Shanghai 已在本地服务空闲时重启，健康检查正常，无活跃会话。配置明确设置 `LOCAL_ASR_MULTILINGUAL_ENGINE=whisper`；对话仍为 qwen2.5:3b。ASR 预热约 1.74 秒，TTS 约 6.67 秒。没有部署到 ECS。

实验切换需单独准备 `.local/code-switch-asr-venv`（mlx-audio 0.6.4、parakeet-mlx 0.6.0、mlx-whisper 0.4.3）和离线缓存，再显式设置 `LOCAL_ASR_PYTHON` 与 `LOCAL_ASR_MULTILINGUAL_ENGINE=qwen3`；当前保持关闭。

## 证据与复现

- [统计与最终源码核验](/Users/chenqiankun/Documents/real-time-voice/evals/code_switch_voice_release/summary.json)、[逐题审阅](/Users/chenqiankun/Documents/real-time-voice/evals/code_switch_voice_release/review.json)、[输入完整性](/Users/chenqiankun/Documents/real-time-voice/evals/code_switch_voice_release/input-integrity.json)。
- [最终默认停顿集](/Users/chenqiankun/Documents/real-time-voice/evals/code_switch_pause/cases.json)、[源码冻结](/Users/chenqiankun/Documents/real-time-voice/evals/code_switch_pause/frozen.json)、[源码差异](/Users/chenqiankun/Documents/real-time-voice/evals/code_switch_pause/baseline-to-current.patch)。
- [普通独立集](/Users/chenqiankun/Documents/real-time-voice/evals/code_switch_voice_release/cases.json)。完整输入和输出音频在 `.local/code-switch-release/`、`.local/code-switch-pause/`，各结果 JSON 引用对应路径。
- [回归日志](/Users/chenqiankun/Documents/real-time-voice/.local/code-switch-release/tests-complete.log)、[更新后健康检查](/Users/chenqiankun/Documents/real-time-voice/.local/code-switch-release/service-after-health.json)。

仓库根目录运行停顿集：

```sh
.venv/bin/python -m scripts.build_code_switch_pause_eval
.venv/bin/python -m scripts.evaluate_code_switch_pause --variant baseline --repeat 2
.venv/bin/python -m scripts.evaluate_code_switch_pause --variant candidate --repeat 2
```

重建会生成新的 macOS 系统语音文件，原始对照应使用保存的 WAV 与哈希；基线快照必须取本轮 `.local/code-switch-pause/baseline/`，不能用当前产品覆盖。普通集对应 `scripts.build_code_switch_voice_release` 与 `scripts.evaluate_code_switch_voice_release`；此 runner 的 candidate 明确启用实验 ASR，不能误读成当前默认链路。原始 JSON、冻结配置与运行日志优先于重跑生成的随机模型措辞。
