# 最终默认链路停顿专项集

四个全新问题，中文引导语后显式插入 600ms 停顿，再说 teacher/window/monkey/music，每题两遍。原始源段完整保留并校验。另有静音与低噪声各两遍。仅验证该停顿故障，不代表自然语音总体正确率。

baseline/candidate 均强制原有 Whisper ASR，candidate 为最终默认源码；冻结清单中的 multilingualModel 是随 runner 保留的实验模型元数据，本组没有调用它。完整目标音频被接收与词被正确识别分别计分。0/8→8/8 收到完整英语词，0/8→6/8 正确完成，window 仍两次漏词。基线提前截断的负首音时间不列为速度成绩。

frozen.json 保存源码与模型摘要，baseline-to-current.patch 保存本轮差异，原始结果完整保留。汇总与逐题评分见 ../code_switch_voice_release/{summary,review}.json；报告见 ../../docs/30-code-switch-voice-evaluation.md。
