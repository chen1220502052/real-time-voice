# 最终离线独立集与端点诊断

13 条新问题，其中 12 个完整任务、1 个缺词澄清控制。offline 的输入和 ASR worker/Bot/提示词在首次推理前冻结；修改前后各两遍。最终只调整了离线 runner 不调用的端点策略，summary.json 用源码哈希和还原代码块核验离线计算链路未变。因此 offline 仍作为独立评估，12 个完整任务不含缺词控制。

本目录 live 结果暴露 300ms 句间停顿导致提前截断，已经转为诊断数据，不参与最终实时延迟或完成率。最终实时结果另用 evals/bilingual_voice_live_release 六条新题。

详情见 docs/29-3b-bilingual-voice-evaluation.md。实际 WAV 位于 .local/bilingual-voice-final。
