# 独立自动恢复实验：未采用

16 个主任务（12 混说）、4 个语言/缺词控制、3 个噪声控制。整段各两遍；预选实时 10 个主任务及全部控制各两遍。全部 PCM 与源段哈希校验。该 candidate 明确启用实验复核，不是当前默认服务。

自动恢复不稳定，新增把中文单词音节当成 dance，未启用。原始失败与冷加载数据保留；review.json 是人工按题目与文本审阅的标签，不是音质盲听评分。源码在 .local/code-switch-target-final/frozen-candidate/ 按原路径保存，匹配 frozen.json；当前产品源码已有安全保护变更，不再与本实验相同。

报告：../../docs/31-code-switch-target-verification.md。
