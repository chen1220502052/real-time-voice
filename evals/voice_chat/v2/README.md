# 第二轮 3B 语音评测

最终结果见 [第二轮报告](../../../docs/26-3b-grounded-voice-evaluation.md)。语音继续使用 `qwen2.5:3b`，不微调，也不切换到 9B。

本轮先修复喜欢与最喜欢的区别、AI 年龄、中文词义，然后多次验证。每次查看一批题的失败后，那批题就转为开发／回归数据。`heldout.json`、`confirmation.json`、`release-check.json`、`final.json`、`acceptance.json`、`checkpoint.json`、`verification.json` 是先后使用的历史批次；文件名中的 final/acceptance 不代表目前的最终结果。没有把它们修复后的重跑称为独立验证。

**最终独立集是 `shipping.json`**：40 条全新题目，未复用此前任何完整对话历史，即使忽略年级也不重复。源码和规则在首次执行前记录于 `frozen-shipping.json`；运行后未修改产品代码或该批评分规则。两个版本各运行两轮，AB/BA 交替，共 160 次 Bot 请求。

- `baseline-prompts.json`：本轮开始前捕获的上一版提示词。比较基线通过评测内部的临时补丁关闭新加的确定性处理、语言处理和提示词，恢复上一版提示词与语义路径；通用传输／断句层由两版共享。补丁只在串行的评测进程中使用。
- `results.json`：最终 160 条已交付回复、逐题评分、错误、时延、回复来源与代码摘要。
- `review.json`：编程助手对全部 80 条新版回复的逐条复核；不是独立教师评分。
- `stage-summaries.json`：所有历史批次统计，保留失败与非单调变化，避免只展示中途最好的一次。

评分仍是确定性规则：检查事实词、明确否定、语言、格式、长度和追问，不能代替完整语义审查。失败闭合仍计为失败，不因“没有讲错”就算成功。多义词列出多个含义时，即使命中正确词，也可能没有充分按语境消歧。

测量经过实际 EnglishChatBot / CurriculumChatBot 的文字交付，使用隔离单元、没有回答库命中和数据库写入。只调用本地 3B；双方配置相同，温度 0.3、上下文 4096、最多 256 个输出 token。这里的时延不包括 ASR、TTS、浏览器或扬声器。

只有明确要求中文且走模型生成的请求才缓冲到语言检查结束；若语言不合格，最多增加一次同一个 3B 的转换。没有使用 9B。`languageRepairRequests`、`languageRepairMs` 记录这部分开销；这类回复的 provider token 统计可能反映最后一次模型调用，实际交付时间以 `firstTextDeltaMs` 为准。普通英文回复继续流式输出。确定性回复不会调用模型，不能把其亚毫秒响应当成 3B 推理速度。

复现最终独立集（重跑只能称复现，不能再作为新的独立题）：

```bash
.venv/bin/python -m scripts.evaluate_voice_chat \
  --dataset evals/voice_chat/v2/shipping.json \
  --baseline-file evals/voice_chat/v2/baseline-prompts.json \
  --split heldout --pipeline --repeat 2 \
  --output .local/voice-eval-v2/reproduce.json
.venv/bin/python -m pytest tests/test_voice_grounding.py tests/test_voice_eval.py tests/test_local_llm.py tests/test_english_chat.py tests/test_curriculum_chat.py -q
```

增补新题时，保持来源／场景整体隔离，不将同一素材的改写拆到调试集和验证集。新题应包括喜欢但未说最喜欢、明确最喜欢、事实更新、不同人物、身份与故事年龄、问题翻译、单词释义、上下文消歧和中文材料理解。不要为了让保留集通过而事后修改答案规则。
