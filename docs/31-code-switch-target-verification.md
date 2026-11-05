# 英语目标词漏识与短词错识：实验未通过，启用缺词保护

2026-11-05。继续使用 **qwen2.5:3b Q4_K_M**，没有微调，没有改用 9B。识别默认仍为 Parakeet int8 + tiny 语言判断 + Whisper large-v3-turbo，播报仍为 Kokoro MLX。

**本轮没有把 window 漏识和短词错识解决到可全面启用的程度。** 自动恢复方案在新独立集上新增错误，已关闭。最终本地启用的改动是识别文本缺少目标词时直接澄清，避免 3B 自行编一个词或生成不相关的例句。

## 最终启用的改动

语音 grounding 使用纯文本语法检查，覆盖“我想知道这个英语单词的中文意思”“请告诉我这个词的中文含义”“请用中文解释英文单词的意思”等空目标问法。不再受原先几条固定短语或“用中文”关键词的限制。引用的中文词、已经识别出的英语词、多个词的比较和普通聊天不会被此空槽检查改成缺词。

识别器原始转写保持原样，不往其中补参考答案。不清楚实际目标词时，回复“我没有听清要解释的英语单词，请把单词再说一遍或发文字”。这属于安全澄清，**不能计为完成原词义任务**。

配置明确为 `LOCAL_ASR_TARGET_VERIFY=0`、`LOCAL_ASR_MULTILINGUAL_ENGINE=whisper`。实验实现与失败录音保留，默认环境不会调用新增的切片复核。没有新模型下载，没有训练、LoRA 或 9B 改写。

## 最终默认版本：另一批新录音验证

在关闭实验并完成上述保护之后，再冻结产品源码，建立 8 个全新问题和 3 个噪声控制。5 个完整任务包含混说 window、中文房子释义、英文 apple、AI 生日、英文 dog；3 个原话本就没有实际目标词的问题。两种版本各跑一次，每 100ms 按真实时钟送入 VAD。此处是小规模语音验证，不是总体识别率估计。

| 指标 | 修改前 | 最终默认版本 |
| --- | --- | --- |
| 真正缺词时正确澄清 | 1/3 | 3/3 |
| 完整任务完成 | 3/5 | 3/5 |
| 静音、低噪声、高噪声不回复 | 3/3 | 3/3 |
| 运行错误 | 1 | 0 |
| 全部有声回复首段 PCM P50 / P95 | 922 / 1692 ms，7 条有音频 | 808 / 1057 ms，8 条有音频 |

window 仍被原 ASR 省略：修改前生成了不相关的饭菜句子，修改后澄清，但两者在完整任务中都算失败。中文“房子”仍只被重复而未解释，也算失败，未从分母移除。apple、AI 生日、dog 三题通过。缺词问题中，修改前一次解释了原话没有的“饥饿”，另一次例句生成失败；最终版本三题都直接澄清。

两组回答路径和有声数量不同，不能把上述分位数当成一般语音识别提速的证明。最终 ASR 没有改变，主要减少的是缺词后无效的生成/重写。

## 自动恢复实验：完整独立对照与拒绝原因

尝试组合现有英语识别器的 token 时间位置、Whisper 中文对齐和已加载的 tiny 模型，对限定为一个英语词的问法复核。只使用真实波形与模型概率，不向解码器提供候选词、课程词表、历史偏好或参考答案。裁剪位置也不使用评测 source segment 元数据。

第一版使用较大的 Whisper 再识别切片，部分请求识别超过 2 秒，并误拒绝正确 tree，转为诊断。第二版用现有 tiny 做切片复核；两个完整识别器已同意的词不重复切片，限制额外解码次数。正式对照在全部产品源码冻结后进行，后续未再根据输出修改该实验版本。

正式集：16 个完整任务，其中 12 个混说（10 个新目标词 fish/dish、coat/goat、light/night、park/bark、desk、flag，另有两种全新 window 录音）、2 个中文身份/偏好、2 个英文任务。另有 2 个中文词、2 个真正缺词、3 个噪声控制。整段识别每题两遍；实时测试是预先选择的 10 个完整任务和全部控制，各两遍。重复两遍不算额外独立问题。

| 实验指标 | 原链路 | 自动恢复实验 |
| --- | --- | --- |
| 整段混说目标词英文拼写保留 | 6/24，25.0% | 6/24，25.0% |
| 整段完整任务通过 | 14/32，43.8% | 14/32，43.8% |
| 整段错目标试次，包含数字误识 | 6/24 | 6/24 |
| 实时混说目标词英文拼写保留 | 4/16，25.0% | 6/16，37.5% |
| 实时完整任务通过 | 8/20，40.0% | 10/20，50.0% |
| 实时 window 正确恢复 | 0/4 | 2/4 |
| 实时全部回应首音 P50 / P95 | 1054 / 1641 ms | 1093 / 1792 ms |

虽然实时一题 window 的两次请求都恢复成功，另一问法仍两次失败，整段识别也未恢复这两种 window 问法。实验还把中文“单词”的音节误当成英语 **dance**，随后解释成“跳舞”；两个模型同意一个词并不足以证明它来自实际英语目标音频。coat→code、night→9 仍存在。实验不能默认启用，不能用实时成功的一个问法概括成“window 已解决”。

整段首音全部回应 P50/P95：原链路 822/2326ms，实验 883/1169ms。基线有一次 3B 冷加载约 10.8 秒，该次首音 12.82 秒，原始数据完整保留；剔除这一明确冷加载试次后原链路为 757/1853ms（31 条），实验 883/1169ms（32 条）。不据此宣称实验更快。整段口径没有 VAD 端点，不能与实时延迟混为一谈。

此实验中原链路发生一次例句补全，实验没有；两边没有运行错误。实时噪声控制各 6/6 不回复。中文词控制也暴露既有质量问题：书包被反向答成 schoolbag，台灯被识别为抬灯后可能编造解释。它们在逐题记录中明确列为失败，没有宣传为中文质量通过。

## 方法、边界与回归

本机 Apple M4 Pro / 24GB，macOS Tingting 普通话与 Samantha/Daniel 英语分别合成后拼接。保留全部重采样 PCM，不按幅度裁去弱辅音。正式实验 23 个 WAV、43 个源段；最终默认验证 11 个 WAV、10 个源段，全部按源段和整段 SHA-256 检查。新数据在首次识别前冻结，不根据识别成败筛题。

两组各有独立的源码冻结、版本、模型摘要、原始结果和逐题评分。正式实验的源码另存 `.local/code-switch-target-final/frozen-candidate/`，与 frozen.json 一致；它与最终默认版本不同。最终默认版本的 12 项源码/词表核验匹配其自己的冻结清单。

相关回归 **153 passed、1 skipped**。包括：缺词语法、引用中文/英语、多个词的比较、对齐不一致、复核分歧与上限、既有收音/端点、偏好和身份保护等。已有测试通过不等于所有问法正确：诊断集“你和我一样也是九岁吗”仍暴露 3B 自称十三岁的既有漏洞，本轮没有修复，不能宣称身份问法全面通过。

延迟定义为完整输入话音结束到服务端首段 PCM，包含端点、识别、3B 与 TTS，不含浏览器缓冲、网络和扬声器起音。最终默认验证在测试前额外预热 3B；噪声没有音频，不参与首音分位数。分位数线性插值，小样本不构成稳定总体性能估计。

输出 WAV 保存并用 Whisper 交叉转写；此核对与主路径共享权重，混合语言转写也可能漏掉中文段，不能代替真人听感或儿童实录。仍需要自然中英混说录音来判断真实识别质量；本轮不宣称已解决目标词问题。

## 证据和复现

- 自动恢复实验：[结果统计](/Users/chenqiankun/Documents/real-time-voice/evals/code_switch_target_final/summary.json)、[逐题记录](/Users/chenqiankun/Documents/real-time-voice/evals/code_switch_target_final/review.json)、[冻结清单](/Users/chenqiankun/Documents/real-time-voice/evals/code_switch_target_final/frozen.json)、[输入与源码校验](/Users/chenqiankun/Documents/real-time-voice/evals/code_switch_target_final/integrity.json)。
- 最终默认保护：[统计](/Users/chenqiankun/Documents/real-time-voice/evals/code_switch_target_guard/summary.json)、[逐题记录](/Users/chenqiankun/Documents/real-time-voice/evals/code_switch_target_guard/review.json)、[冻结清单](/Users/chenqiankun/Documents/real-time-voice/evals/code_switch_target_guard/frozen.json)、[与原链路的源码差异](/Users/chenqiankun/Documents/real-time-voice/evals/code_switch_target_guard/baseline-to-serving.patch)。
- [回归日志](/Users/chenqiankun/Documents/real-time-voice/.local/code-switch-target-guard/tests.log)。录音位于结果 JSON 的 output 路径；输入在相应 `.local/…/inputs/`。旧版裁剪录音和首轮诊断没有删除。

最终默认验证可从仓库根目录执行：

```sh
.venv/bin/python -m scripts.build_code_switch_target_guard
.venv/bin/python -m scripts.evaluate_code_switch_target_guard --variant baseline --mode live --repeat 1
.venv/bin/python -m scripts.evaluate_code_switch_target_guard --variant candidate --mode live --repeat 1
```

重建音频会生成新的文件，重跑原始对照应使用已保存的 WAV、SHA 和本轮 baseline 快照。candidate 会检查源码冻结；当前源码不能直接重跑旧自动恢复实验，要使用其归档版本。`evaluate_code_switch_target_final` 明确启用实验开关，仅用于复现失败，不应当成当前默认配置。

本地服务于 2026-11-05 10:04:08 Asia/Shanghai 在空闲时重启，健康检查正常、无活跃会话；ASR 预热约 3.26 秒、TTS 约 7.46 秒。只更新本地服务，没有部署到 ECS。启动健康记录另保存在 `.local/code-switch-target-guard/service-after-health.json`。
