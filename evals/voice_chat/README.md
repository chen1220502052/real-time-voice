# 3B 语音对话评测

本套评测只调用本机 Ollama `qwen2.5:3b`，不微调、不调用云端裁判，也不使用生产服务器。比较原提示词与新的语音提示词，并独立检查 Bot 实际交付的文字。

本次结果见 [2026-11-03 评测报告](../../docs/25-3b-voice-evaluation.md)。语音模型未显式指定时默认使用 3B，不继承文字聊天／写作的 9B 配置。

## 数据与来源

`cases.json` 包含 240 个测试回合，来自 54 个基础素材／行为场景；改写题和不同年级版本并非彼此独立的真实用户样本。

| 类别 | 回合数 | 内容 |
|---|---:|---|
| 课文 | 96 | 24 条已核对课文原句，每条四种问法；保留教材页路径、页面哈希和原句哈希 |
| 写作配套 | 24 | 日常作息、兴趣活动的原创短文与对话问题 |
| 阅读配套 | 36 | 植物、午餐、上学用品的原创短文与问题 |
| 听力文字稿配套 | 12 | 动物故事主题的原创短文与问题；只测文字理解 |
| 多轮与行为 | 72 | 事实更新、追问、换题、多项回答、身份、中文求助等 18 场景，各四个年级版本 |

课文摘录来自仓库已有的 `backend/data/chat/textbook-sentences.json`，目前这部分是四年级上册；不能据此宣称已覆盖三至九年级教材。其余测试包含三至九年级，但网页 Level 1/2 不等于国内年级认证。网上材料参考以下原站编辑内容，未使用评论区，也没有将原创配套短文标成教材或官方原文：

- British Council：[My day](https://learnenglishkids.britishcouncil.org/read-write/writing-practice/level-1-writing/my-day)、[P3 My Favourite Activities](https://www.britishcouncil.hk/sites/default/files/p3_my_favourite_activities.pdf)。
- British Council：[Growing a plant](https://learnenglishkids.britishcouncil.org/read-write/reading-practice/level-1-reading/growing-plant)、[School lunch menu](https://learnenglishkids.britishcouncil.org/read-write/reading-practice/level-1-reading/school-lunch-menu)、[School note](https://learnenglishkids.britishcouncil.org/read-write/reading-practice/level-1-reading/school-note)。
- British Council：[The lazy bear](https://learnenglishkids.britishcouncil.org/listen-watch/short-stories/lazy-bear)。听力配套题为原创故事文字稿，不是对原站音频或 ASR 的测试。

来源清单、检索核对日期、原创材料文本和哈希均随数据保存。没有采集孩子的私人聊天。

## 调整与独立验证

按整个教材单元、整个网页来源或整个行为场景分组，再用固定哈希分为 48 条开发题、192 条保留题。同一材料的改写和不同年级版本不会跨集合。开发集用于调提示词；保留集开始前冻结提示词和规则，不根据保留集失败项继续调整。

`baseline-prompts.json` 是修改前捕获的两个真实语音路径的提示词。扩展课程的相关年级资料在运行时用同一检索函数补充。新版由 `backend/voice_prompt.py` 共享，教材路径附年级和单元，扩展路径附相关年级资料。

每一题对两个版本交替采用 AB/BA 顺序；重复轮次反转顺序。固定温度 0.3、8192 上下文、最多 256 个输出 token。预热不计入热启动统计。报告保存模型摘要、量化信息、实际提示词快照、数据／代码哈希、原始回答、逐项失败原因和耗时。没有固定采样种子；两轮结果不能作为统计显著性证明。

## 评分边界

自动规则检查指定事实、明确的错误事实、中文要求、追问数量、明显格式、标点和最多 45 个英文词。这是**规则通过率**，不是完整的正确率或儿童教学质量评分。关键词出现不保证推理正确；自然度、遗漏、否定和词汇难度还需逐条看答案。历史开发结果中的同义表达误判，在运行保留集前统一修复，重新评分不改原始回答。

`firstTokenMs` 是模型首 token；`firstSentenceMs` 是首个完整句子稳定的时间；直接模型评测的 `firstSpeechChunkMs` 是文本按语音切段函数可切出首段的估算时间。上述指标都不含 ASR、TTS、网络到浏览器、真实扬声器或物理麦克风。

`--pipeline` 则经过真实 EnglishChatBot / CurriculumChatBot 的流式交付和限长逻辑。它使用隔离只读单元、不检索回答库、不写会话数据库，报告 `firstTextDeltaMs`。因此它可发现答案被截断等问题，仍不等于完整音频端到端验收或线上业务命中率。

## 复现

从仓库根目录运行，先确保本机 Ollama 正在运行且已有 `qwen2.5:3b`：

```bash
.venv/bin/python -m scripts.build_voice_eval --check
.venv/bin/python -m scripts.evaluate_voice_chat --split dev --repeat 2 --output .local/voice-eval/dev.json
.venv/bin/python -m scripts.evaluate_voice_chat --split heldout --repeat 2 --output .local/voice-eval/heldout.json
.venv/bin/python -m scripts.evaluate_voice_chat --split all --variants candidate --pipeline --output .local/voice-eval/pipeline.json
.venv/bin/python -m pytest tests/test_voice_eval.py tests/test_local_llm.py tests/test_english_chat.py tests/test_curriculum_chat.py -q
```

修改题目生成器后运行 `scripts.build_voice_eval` 重建版本，并重新检查来源分组。不要把修复某题后的同一次保留集重测当作新的独立验证。新增教材应另留未见过的单元做下一版验证。
