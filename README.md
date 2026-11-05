# Real-time Voice Avatar

## 本地英语学习站（当前配置）

当前本机使用 Ollama `qwen3.5:9b` 处理开放式文字聊天、范文生成和作文批改，不需要云端 API Key。
三年级的 287 条词汇及短语已用 [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M)
生成独立示范音：美式女声 `af_heart`、速度 0.9、24 kHz WAV。播放直接读取音频文件，不实时加载 TTS。

```bash
# 首次安装后端依赖并启动 Ollama；语音环境先按下节准备。
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,local-voice]'
ollama pull qwen3.5:9b
bash scripts/run_learning_local.sh
```

打开 `http://127.0.0.1:8765/`。启动脚本读取被 Git 忽略的 `.env.local`，默认只监听本机；
`LLM_PROVIDER=ollama`、`OLLAMA_NATIVE_BASE_URL=http://127.0.0.1:11434` 和
`OLLAMA_MODEL=qwen3.5:9b` 选择原生 Ollama 接口。也可单独设置 `CHAT_PROVIDER` / `WRITING_PROVIDER`
及对应的 `CHAT_MODEL` / `WRITING_MODEL`。本地模型接口仅接受回环地址，且不发送云端密钥。
首次加载模型会比热请求慢；当前机器为 M4 Pro、24 GB 内存。

写作默认“自然表达 · 标注新词”，优先简单、连贯的英语，并列出表外词及中文释义。
仍可选择“教材词汇 · 限制新词”和“严格模式 · 只用已学词”。后两者保留独立词汇校验；
三年级早期词表较小时可能无法生成合格范文，会明确提示重试，不把超纲内容标为合格。
本地模式不调用联网范文搜索。实时英语语音对话已接入本地 Parakeet、Ollama 与 Kokoro，首页“语音对话”可进入。

复现单词示范音（先完成下方教材资料导入）：

```bash
python3.11 -m venv .local/tts-venv
.local/tts-venv/bin/pip install 'kokoro==0.9.4' 'misaki[en]==0.9.4' 'spacy>=3.8,<3.9' 'transformers>=4.48,<4.52' 'huggingface-hub<1' soundfile scipy
.local/tts-venv/bin/python -m scripts.build_grade3_word_audio
```

首次从官方仓库下载模型、音色及英文分词模型，之后在本机合成。支持断点续跑；
全部完成后才更新 `backend/data/practice/word-audio.json`。音频与生成清单存于
`英语/本地示范音/kokoro-af-heart/`，清单记录模型指纹、音色、速度和音频指纹。
`英语/` 不提交 Git，新机器需导入教材并运行生成脚本。课文原音仍使用出版社录音。

## 本地实时英语语音对话

`bash scripts/run_learning_local.sh` 默认启用本地语音：WebRTC VAD 判断说话起止，Parakeet TDT v3（MLX/Metal）
在说话过程中每 600 ms 尝试更新完整上下文快照，静音 100 ms 时重新识别当前完整话语。
已有教材聊天或本机 Ollama 生成回复，Kokoro `af_heart` 合成语音。
浏览器仍使用现有 WebSocket、16 kHz PCM16、100 ms 包和打断围栏；麦克风在点击“拨通”后才申请权限。
打开 `/voice.html` 或从首页的“语音对话”进入，保留当前教材单元及年级。

主环境安装 `.[local-voice]`，语音模型准备在 `.local/asr-venv` 和 `.local/tts-venv`。
实时 ASR 在已有 ASR 环境安装并下载固定版本权重（约 2.5 GB，运行时转为 bfloat16）：

```bash
.local/asr-venv/bin/python -m pip install 'parakeet-mlx==0.6.0'
.local/asr-venv/bin/hf download mlx-community/parakeet-tdt-0.6b-v3 --revision ed2b7e8c15f9aaa0b5772e2efb986255eaef7e15 --include config.json model.safetensors
```

教材批量对齐仍使用下节的 Whisper，不受实时模型切换影响。
它们通过持久化子进程运行，主服务不导入 MLX/PyTorch；模型启动后预热，后续通话复用。
模型推理不联网、不使用 API Key。Ollama 需先在本机运行，语音启动时同时预热聊天模型，默认保温 30 分钟。
语音默认使用已下载的 `qwen2.5:3b`（新机器先运行 `ollama pull qwen2.5:3b`）；
`VOICE_CHAT_MODEL` 单独控制语音模型，文字聊天和写作继续使用各自的模型配置。

本机 `.env.local` 配置：

```dotenv
VOICE_ENABLED=1
ASR_BACKEND=parakeet
TTS_BACKEND=kokoro
AGENT_BACKEND=english_chat
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen3.5:9b
VOICE_CHAT_MODEL=qwen2.5:3b
LOCAL_ASR_LANGUAGE=en
LOCAL_VAD_END_MS=500
TTS_CPU_THREADS=4
```

识别最长 30 秒；提前快照每次最多一个在途任务，推理繁忙时跳过新快照，不积累识别队列。
这是完整上下文的增量重识别，不是缓存编码器的原生流式推理；持续说话会使旧快照失效，
最终文字必须来自覆盖全部最新语音、含至少 100 ms 尾静音的快照。中间文字用于字幕、可选预准备及打断确认，
正式回复仍等待有效定稿。模型 token 置信度的几何平均默认须达到 `LOCAL_PARAKEET_MIN_CONFIDENCE=.9`，
没有有效文字时不编造回复；它是模型内置信度，不代表校准后的正确率。

正常断句静音门槛为 `LOCAL_VAD_END_MS=500`；当前快照已完成、文字至少稳定 180 ms、置信度足够时，
可在至少 300 ms 静音后定稿。若英语短语以冠词、介词、连词或助动词等明显未完形式结尾，
或停在 “I would like…”、“I think…”、“My favorite…” 等未完成短语，
则等待至少 900 ms 给学习者续说。这是有限的断句启发式，不是语义完成检测；
继续说话会归零静音计数并使旧快照作废。

Parakeet 在机器人有活动回复时每 300 ms 尝试更新完整上下文快照；其他时间仍是 600 ms。
至少 300 ms 有声语音、置信度 ≥ `.94`、至少两个词（`stop/wait/enough` 可单词），
且文字来自近期音频时，即可撤销旧回复播放权，继续听用户说完。纯犹豫声和无有效文字的噪声不触发打断。
文字与机器人相同的片段只暂缓 partial 确认；只有同时存在很强的音频相关性才作为疑似回声忽略，
避免直接屏蔽学生跟读。这是泄漏回声的保守兜底，浏览器仍启用声学回声消除。
如果用户在机器人首包发出前续说，只挂起首包，不阻塞 ASR；确认插话后丢弃旧包，噪声结束则恢复。
浏览器还用 20 ms 能量窗口保护尚未开始的播放：连续两个窗口有声时暂缓首音，
安静 200 ms 后自然放行，连续背景能量最多挂起 900 ms。它不会直接截断正在播放的回复；
普通回复到达时该窗口已经过期，不增加固定播放等待。
`barge_in_confirmed`、`reply_audio_held`、`overlap_ignored` 会记录决策证据及原因。

`ASR_BACKEND=mlx` 可恢复 Whisper `large-v3-turbo`：静音 200 ms 启动完整识别，等待配置的断句门槛。

Apple Silicon 可通过 `LOCAL_PARAKEET_QUANT_BITS=8` 将 Parakeet 的可量化全连接／嵌入层改为 8-bit，卷积保持 BF16；`16` 使用原始精度。4-bit 在本地单词测试中出现退步，服务端不接受该模式。当前短前瞻增量转写实验也存在明显识别退步，在线仍使用完整上下文快照。

Kokoro 可选 MLX BF16 后端，沿用已有 `hexgrad/Kokoro-82M` 权重及 `af_heart` 音色，不重新下载或替换音色。建议使用独立环境：

```bash
python3.11 -m venv .local/tts-mlx-venv
.local/tts-mlx-venv/bin/pip install 'mlx-audio==0.6.4' 'misaki[en]==0.9.4' torch scipy
.local/tts-mlx-venv/bin/pip install 'en-core-web-sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl'
```

在 `.env.local` 设置 `LOCAL_TTS_ENGINE=mlx`、`LOCAL_TTS_PYTHON=.local/tts-mlx-venv/bin/python`，重启本地服务后生效。`LOCAL_TTS_MLX_CACHE_MB` 默认 128。回退时将引擎设为 `torch`，解释器恢复为 `.local/tts-venv/bin/python`。实时语音文本模型仍使用原来的 3B 配置。语音后端在启动时加载并预热，通话中不会临时下载安装依赖。
`LOCAL_ASR_SPECULATIVE=0` 可关闭快照提前计算以作对比；它不发布中间文字，也不使用快速断句。
Kokoro 从更低幅度的起音开始保留语音，并留 30 ms 余量，避免把弱辅音当作静音剪掉。
浏览器仍保留三包抖动预缓冲，首包排程余量从 60 ms 降为至少 25 ms；
若设备的 `AudioContext.baseLatency` 更大，则保留该值加 12 ms 的渲染余量。
带教材单元的语音聊天也按稳定的完整单词逐步交给 TTS，无须等待整个句子生成；句尾仍须通过完整性校验。
本地语音使用较短的独立提示词；新问题只携带已播出的历史，不重复注入无关的未播草稿。
追问、继续和重复仍保留需要的草稿与实际播放信息；文字聊天和写作的提示词不受影响。
Kokoro 在同一模型和音色下复用 G2P/音色对象，直接合成音素，省去未使用的词时间戳处理。
`LOCAL_TTS_CACHE=1` 默认仅在 worker 内存中缓存完全相同的文字、语速、音量，最多 64 条、8 MiB（按编码音频计）；
长度超过 160 字符的片段不缓存，不写磁盘，进程退出即清空；设为 `0` 可测首次合成。
首包计时中的 `localTts` 区分请求总耗时、worker 推理、音素转换、神经网络合成、后处理及缓存命中。
Ollama 的内部计时记录在 `bot.latency` 日志中，仅保存耗时与计数，不记录提示词、音频或密钥。
挂断释放通话名额，模型继续保温；停止主服务时退出语音子进程。设置 `VOICE_ENABLED=0` 可运行文字与教材页面。

浏览器重复验收：仅在本机地址给语音页加 `&diagnostics=1`（无其他参数时用 `?diagnostics=1`）。
在诊断面板选择本地 WAV，再拨通；“播放”把样本送入正常 MediaStream / AudioWorklet / WebSocket 链路，
“插话”会在下一次回复的音频图开始输出 0.8 秒后送入指定样本。诊断模式不会申请物理麦克风。
加载四个样本后可运行“完整验收”：依次测试首轮回复、第二个问题、第三个样本插话和第四个问题。
下载诊断报告可查看各阶段耗时、实际采集与调度时间、打断及播放完成事件；音频图时间不等同于扬声器实听时间。
报告额外记录 `localAsr` 的请求开始、结果就绪、断句确认时间和 worker 实际推理耗时；
`asrRequestWallMs` 是被采用的最终快照的单次 ASR 请求时间，不是所有提前快照的累计计算时间；
`asrOverlappedEndpointMs` 是该请求与断句重叠的部分，
`asrWaitAfterEndpointMs` 是确认停顿后仍要等的部分，不能把三项再次相加。

真实音频链路验收脚本为 `scripts/check_local_voice.py`：使用本机测试账号环境变量
`LOCAL_TEST_USERNAME` / `LOCAL_TEST_PASSWORD`，运行 `.venv/bin/python -m scripts.check_local_voice`；
加 `--interrupt` 验证在机器人说话时插话。脚本只接受回环服务地址，使用生成的语音样本，不录制麦克风，
将识别文本、回复 WAV、协议错误与资源释放结果保存在 `.local/live-voice-e2e/`。

## 本地语音补齐与文本对齐

Apple Silicon 本机批处理使用 `mlx-community/whisper-large-v3-turbo`（MLX/Metal）识别英语并提取词级时间戳，
Kokoro `af_heart`（CPU 4 线程）生成缺音示范句。它们分别安装在 `.local/asr-venv` 与 `.local/tts-venv`，
不增加网站运行环境的模型依赖。M4 Pro、24 GB 已进行真实推理验证。

```bash
python3.11 -m venv .local/asr-venv
.local/asr-venv/bin/pip install 'mlx-whisper==0.4.3' soundfile scipy
.local/asr-venv/bin/hf download mlx-community/whisper-large-v3-turbo --include config.json '*.safetensors' '*.npz'
# TTS 环境和 Kokoro 权重按上节安装；处理阶段只读取本机缓存。
.venv/bin/python -m scripts.prepare_local_lessons prepare
.local/asr-venv/bin/python -m scripts.prepare_local_lessons transcribe
.venv/bin/python -m scripts.prepare_local_lessons align
.local/tts-venv/bin/python -m scripts.prepare_local_lessons synthesize
.venv/bin/python -m scripts.prepare_local_lessons publish
```

首批范围为已保存参考文本的 70 段初中同步录音，以及 90 条有教材原图依据的四上示范句所对应的原音。
另从 12 个缺录音单元的教材原图中逐句核对了 62 句对话节选，包含八上新版全部六个单元；
来源和原图指纹保存在 `backend/data/practice/supplemental-sentences.json`。
第三方时间轴存在混入整章文字的情况，因此不直接采用下载时间点：只发布与独立 ASR 逐词匹配的参考句，
保留原文和音频指纹，不把识别稿变成教材原文。已核验的示范句若未匹配到原音，则生成明确标识的合成示范音；
没有可靠文字依据的缺口仍保留待补。八上旧版录音单列版次，不附到新版 *My Dream Job*。

产物位于被 Git 忽略的 `英语/本地语音处理/`：`inventory.json` 为来源清单，`asr/` 为可断点复用的识别结果，
`lesson-audio.json` 为页面资源包，`字幕/` 保存 SRT/VTT，`report.json` 保存处理统计。
字幕覆盖可确认的练习片段，原始识别稿另存于 `asr/`，不将未经核对的识别文字显示成教材原文。
合成句按单元及原图拼接为连贯节选，按实际采样帧数生成单句时间点；它们补充的是节选示范，不代表整册原配录音已收齐。
章节页新增“逐句听读”、单句播放/跟读和字幕下载，显示真实录音来源与本地合成标识。
资源包或主索引更新后，目录在下一次请求自动刷新；首次升级代码仍需重启后端。
误存为 MP3 的 JPEG 不再进入音频列表，其原文件保留，另恢复为可读图片。

## 本地三年级资料恢复

代码仓库包含已核对的词汇快照，教材原图与原音需单独取回。当前可恢复科普版三年级
上下册，共 24 个单元、287 条词汇和短语、207 条教材录音、202 张课本图片（含 15 张词表原图）：

```bash
.venv/bin/python -m scripts.import_grade3_resources
```

脚本只读取出版社公开页面及其中链接的图片、录音，过滤播放器占位图，按原网页配对图片与录音。
15 张词表原图必须与仓库已核对的 SHA-256 一致才会启用词表；单元、词汇、原创口语任务及学校版次待核对状态沿用原有记录。
资源保存到被 Git 忽略的 `英语/`，来源和文件指纹见 `英语/三年级资料清单.json`；再次运行使用本机缓存，并保留其他年级已有索引。
公开来源目前通过 HTTP 提供资源，脚本不关闭 TLS 校验。更换或补齐资源后须重启服务以清除目录缓存。

重启后在首页选择“三年级 → 教材同步 → 科普版 → 上册/下册 → 单元”。
课文原音、原图、词表和原创主题口语可用；单词独立示范音按上节用本地 Kokoro 生成。
预置文字问答可离线使用，开放式对话与 AI 写作可选择上节的本地模型或原有 DeepSeek 配置。
本次未恢复人教版，也不代表其他年级的原始资源已安装。

```bash
.venv/bin/python -m pytest -q tests/test_grade3_resource_import.py tests/test_grade3_resources.py tests/test_chat_library.py
```

## 按单元检索的分层对话

`/text.html` 支持 3–9 年级教材选择。SQLite 保存单元词汇、累计词汇快照和已审核问答，
依次使用有原文依据的教材回复、提前编写的常见问答；两层回复均按当前累计词汇范围过滤。
只有找不到可靠匹配时才调用配置的本地 Ollama 或 Deep Agents + DeepSeek，一次生成常见、简短、适龄的回复，
按完整句子交付。**模型兜底取消词汇白名单和超纲重写**，因此兜底回答可能包含新词。
对话策略会根据重复输入、多轮追问、换话题和练习状态选择回复。完整生成回答自动入库；明确通用的回答可共享复用，个人信息和不能确定的内容仅保留在原会话。详见 [对话状态与生成库](docs/23-dialogue-state-and-generated-library.md)。

批量词句库已扩充到 591 条预置回复，并单独保存 90 条带出处的教材语句记录。Yes/No、单词回答和 Why 可按实际上一轮上下文检索，近期回复可轮换；未核验的教材原文如实提示缺失。覆盖清单、命中率和时延口径见 [批量对话库验收](docs/24-bulk-dialogue-library-and-benchmark.md)。

安装 `python3.11 -m pip install -e '.[chat]'`，沿用现有 DeepSeek 配置，
可先运行 `python3.11 -m scripts.build_chat_library` 导入词句库，再执行 `bash scripts/run_text.sh`。
`CHAT_LIBRARY_DB` 指定教材快照，`CHAT_DIALOGUE_DB` 指定独立的对话状态和生成回答库，默认均与 `ACCOUNT_DB` 同目录。词表更新不会覆盖生成记录。
文字页切换语音会携带当前单元；不带单元参数的旧语音入口保持原年级聊天方式。


三年级已接入人教版上下册 12 个单元、科普版上下册 24 课的本地教材图片、录音和原创口语任务。
首页课程选择、账号同步、文字与语音聊天均支持三年级；回复更短，语音默认速度为 0.75。
科普版三年级 15 张词表原图已逐项识读，新增 287 条词汇和短语，支持单词练习、累计词汇和作文；下册 Unit 12 为复习课，无新增词。人教版仍需核验匹配词表后才能生成作文，旧版复习词库仅作聊天补充。
人教版三年级上册 Unit 3 的录音标题与 PDF 标题有差异，仍需核对学校教材。
资源更新后运行 `python3.11 scripts/build_grade3_practice.py`，再重建相应聊天参考包和音频片段索引。

## 单元写作助理

章节页“跟读、听力、口语”旁新增“作文”，也可访问 `/writing.html`。支持学生自己写作与 AI 点评；
云端模式的范文先检索网上原文，取得主题、长度和已学词汇检查均通过的短文时提供原文链接和节选，否则用 DeepAgents + DeepSeek 生成原创范文；本地模式直接生成原创范文。
科普版 3–9 年级现有 14 册、118 个单元的 127 张词表原图已整理为 3,844 条单元词汇和短语，并补充有前序课文原图依据的基础词。
按实际教材系列累计到当前单元，优先使用已学词汇并保证表达自然、内容连贯；默认允许最多 4 个不同表外词、出现次数占比不超过 5%。每篇初稿都会再做一轮全文润色，服务端仍独立检查词汇与长度；API 保留零表外词模式。
聊天与写作共用 SQLite 单元词库和累计快照，词表更新后在下一次请求自动刷新；热请求使用缓存集合检查常见词形，无须重启。
缺少前序版次会明确提示；人教版尚无完整词表原图，不用主题补充词冒充已学词。安装 `python3.11 -m pip install -e '.[writing]'` 后沿用现有密钥启动。
详见 [单元写作助理](docs/20-unit-writing-assistant.md)。

## 猫猫英语学习首页与样板单元

启动现有服务后，`/` 为新的学习首页，包含今日、课程、专项练习和学习记录。
六年级原创样板“我的周末计划”包含单词、句子、听力、阅读、口语和复习，
可答题、查看反馈、刷新续学，登录后将进度和复习安排保存到数据库，可跨设备续学。
随包附带原创合成录音，无需模型服务即可完成课程练习。

右上角“切换课程”可选择 4–9 年级、教材同步／分级拓展以及教材版次和单元，
登录账户会记住上次选择，并在打开页面时读取。教材同步使用已有目录和章节录音；完整拓展样板目前
只有六年级，其余年级显示待补充。切换课程不会清除样板学习进度。

现有聊天已归入口语模块：`/voice.html` 为原实时语音页，`/text.html` 为文字聊天；
文字服务模式访问语音入口会转到文字聊天。原 `/index.html` 保留为语音页兼容地址。
样板口语采用临时录音对照和自查，没有自动发音评分。课程选择与学习进度按账户保存到后端数据库，支持跨设备恢复。
课程说明与验证方式见 [样板单元实现](docs/18-sample-learning-unit.md)。

## 教材章节练习

打开 `/practice.html`，按教材版本、年级、册次和章节进行单词／课文跟读、听力和主题口语练习。示范优先使用现有官方教材录音，支持调速、选段循环和本机录音对照，不调用 TTS。当前文章跟读需对照手边教材；逐句文本与自动评测尚未接入。覆盖范围与资源部署说明见 [章节练习模块](docs/16-chapter-practice.md)。

## 分年级英语聊天

已实现六至九年级聊天 Agent，文字与语音共用 TextBot v2 服务接口。Bot 自己保存会话和幂等结果，双工只管理交互与播放事实；不做课程任务、评分或学习档案。

- 纯文字页面：配置现有模型后运行 `./scripts/run_text.sh`，打开 `http://127.0.0.1:8870/text.html`，不加载 ASR、VAD 或 TTS。
- 命令行：`python3.11 -m backend.text_cli --grade 7`，支持 `--base-url`、`--model` 和重复的 `--message`。
- 语音：`AGENT_BACKEND=english_chat ./scripts/run_hybrid.sh`，页面选择年级后拨通。
- [TextBot 标准接口](docs/12-text-bot-interface.md)：请求、事件、取消、错误、旧 Bot 适配和真实链路验证。

模型配置示例见 [.env.example](.env.example)。优先使用 `LLM_BASE_URL / LLM_MODEL / LLM_API_KEY`，仍兼容现有 Ollama 与 DeepSeek 环境变量；密钥只写入本机 `.env.local`。

## 猫猫英语产品与架构规划

- [分年级实时英语聊天架构](docs/11-english-tutor-agent-architecture.md)：当前首版仅做年级配置与自然语音聊天，暂不做教学闭环、评分或知识库检索。

产品后续面向初中生英语口语与听力练习。当前仓库仍为 Web 与 Python 实时语音实现，小程序尚未落地：

- [学生英语小程序 PRD](docs/09-student-english-miniprogram-prd.md)：学习场景、交互、教学反馈、数据和验收要求。
- [微信小程序迁移调研](docs/10-wechat-miniprogram-migration-research.md)：客户端适配、后端复用、真机验证、内部测试及公开运营条件，核查日期为 2026-10-20。

## 当前实现

本地实时语音虚拟人 MVP。浏览器与 Python 后端通过 WebSocket 建立全双工通道：上行发送 16 kHz 单声道 PCM（100 ms/包），后端执行 VAD/ASR → 对话 Agent → 流式 TTS → RMS 猫嘴动画，下行音频与动画由同一 `sequence/replyId/generationEpoch` 对齐。

页面不只是猫咪通话页，也是一套实时诊断控制台：可查看会话、用户与机器人状态，ASR partial/final、LLM 流式文本、分 Turn 延迟、播放缓冲、队列深度、错误，以及可按组件/级别/Turn 筛选的统一时间线。

语音页默认勾选“录制本次对话”，可在拨通前取消。录音仅保存在当前页面内存，最长 10 分钟；“停止录音”不挂断通话。挂断后点击“回放与测延迟”，可查看同步双声道波形（左为麦克风，右为 AI 实际音频渲染）、逐轮定位回放，以及输入尾帧到定稿、模型首文本、TTS 首包等耗时。被打断后尚未渲染的音频不会进入渲染音轨。

“下载录音包 ZIP”一次下载麦克风、计划播放、实际渲染、同步双声道四个 WAV，以及事件／耗时日志和人工标注。通话中的下载是当时的快照，完整留档请在挂断后下载；刷新或离开页面会清除内存录音。计划播放音轨仅供排查调度，实听对照优先用同步双声道或实际渲染音轨。

程序的“停声→AI 渲染”不包含扬声器、声卡或蓝牙的实际出声延迟。测量真实实听间隔时，用手机在同一录音里录下自己和电脑扬声器的声音，导入“录音回放与延迟测量”（限 80 MB、10 分钟），勾选同场录音声明，再通过波形、游标和回放标记“我说完”与“AI 出声”。两点间隔使用同一文件时钟计算，负值表示重叠；支持关联轮次和导出 JSON。导入与回放都在本机完成，人工实听标注与程序音频图测量分别保存，不自动伪装成硬件回环测量。

本地 Parakeet 模式在拨通准备阶段先预热语音模型，再实际执行一段静音 ASR，准备完成后才开始采集。语音模型默认使用 4096 token 上下文（`VOICE_CHAT_NUM_CTX`）；文字聊天和写作保留原来的 8192。Parakeet 的 MLX 空闲缓存默认限制为 128 MB（`LOCAL_ASR_MLX_CACHE_MB`），避免中间张量长期占用统一内存。尚未执行的已取消语音快照会退出队列；已提交的原生推理仍正常收完并丢弃结果，以保持响应顺序。录音日志额外保存 ASR worker 排队、启动、通信耗时及内存快照。

同一后端进程会在通话期间保留本地推理使用权；其他本地文字／写作推理需等待通话释放，超过等待时限会提示繁忙。拨通失败、准备超时和断开都会释放使用权。该约束不控制其他程序直接访问 Ollama；多个独立进程同时加载大模型时，仍可能因系统内存不足发生换出和重新加载。

## 最快启动（稳定 Stub）

```bash
python3.11 -m pip install -e '.[dev]'
./scripts/run_demo.sh
```

打开 `http://localhost:8765`。页面加载只检查服务与浏览器能力；点击“拨通”时才申请麦克风权限。Stub 会输出可播放音频并支持打断，不需要先下载模型。

## 真实模型启动

```bash
python3.11 -m pip install -e '.[asr,tts,observability,dev]'
ollama pull qwen2.5:0.5b
./scripts/run_real.sh
```

默认真实能力组合：

- ASR/VAD：Paraformer-zh-streaming + FSMN-VAD（CPU）
- Agent：Ollama OpenAI 兼容接口，`qwen2.5:0.5b`
- TTS：Piper `zh_CN-huayan-medium`，输出重采样至 16 kHz
- 动画：100 ms 音频包 RMS + 滞回阈值，开口/闭口两态

## 推荐混合模式（智能语音交互 NLS）

阿里云智能语音交互 NLS 只负责识别有效说话片段；FSMN-VAD、Ollama 和
Piper 仍在本机运行。AppKey 和 Token 只通过本地环境变量提供，不要写入仓库：

```bash
python3.11 -m pip install -e '.[asr,nls-asr,tts,observability,dev]'
export ALIYUN_NLS_APP_KEY='智能语音交互项目 AppKey'
export ALIYUN_AK_ID='RAM 用户 AccessKey ID'
export ALIYUN_AK_SECRET='RAM 用户 AccessKey Secret'
./scripts/run_hybrid.sh
```

`ASR_BACKEND=nls` 使用已购买的智能语音交互项目。服务端通过 AccessKey
调用 `CreateToken`，缓存返回的 Token，并在过期前 5 分钟自动刷新；已建立
的识别流不会因为旧 Token 到期而中断。`ALIYUN_NLS_TOKEN` 仅作为控制台
临时 Token 的测试降级配置。如云端不可用，
可将 `ASR_BACKEND` 改回 `funasr`，使用已下载的本地 Paraformer。NLS 与
百炼文本大模型是不同产品和计费项；本项目的 LLM 默认仍是本地 Ollama。

ECS 远端部署推荐显式设置 `ALIYUN_NLS_AUTH_MODE=ecs_ram_role`，通过实例 RAM
角色取得自动轮换的 STS 凭证，再获取 NLS Token。角色模式不读取长期 AK，也不回退
到手填 Token；仅使用 IMDSv2。配置、权限和发布门禁见
[ECS RAM 角色部署说明](docs/03-backend-design.md#10-ecs-ram-角色认证2026-10-22)。

代码仍保留 `ASR_BACKEND=dashscope`，用于未来切换到百炼
`paraformer-realtime-v2`，但它需要单独的 `DASHSCOPE_API_KEY`。

建立会话后，文本 Bot 接收内部 `pick_up` 信号并固定输出“您好，我是猫猫，您有什么需要帮助的吗？”。内部控制信号保留在审计历史中，但不会进入 LLM 对话上下文。

## 测试

```bash
python3.11 -m pytest -q
RUN_REAL_MODELS=1 python3.11 -m pytest -q -m real_model
python3.11 scripts/generate_asr_fixture.py
RUN_REAL_MODELS=1 python3.11 -m pytest -q tests/test_real_models.py::test_real_asr_and_tts_models_load_and_run
python3.11 scripts/stability_test.py --duration 300
python3.11 scripts/long_session_test.py --duration 1800
```

`stability_test.py` 验证短会话反复建连/释放；`long_session_test.py` 在一个
WebSocket 中持续制造多轮语音、回复并采样 RSS、线程、异步任务和队列深度。
默认测试全部注入 Stub，不会意外加载真实模型。端到端用例会把真实 16 kHz
PCM 包送过 WebSocket 会话并验证返回音频、尾包、序号异常、打断优先级、
有界队列、Reply 故障恢复和连接清理。

`tests/fixtures/zh_help_16k.wav` 是由项目内 Piper 模型生成、带前后静音且按
100 ms 对齐的中文测试音频，可由 `generate_asr_fixture.py` 重建。本地
Paraformer 门禁要求它产生完整 VAD 生命周期和非空 final。云端 NLS 复验可在
配置凭证后额外设置 `RUN_REAL_NLS_FIXTURE=1`；该测试默认不运行，避免自动产生
云端调用。

完整演示流程、故障注入与实测基线见 [本地演示与验收](docs/05-demo-and-acceptance.md)，逐条 PRD 证据见 [P0 验收矩阵](docs/06-prd-acceptance-matrix.md)。

## 打断门控（防误打断）

本地 Parakeet 使用上述实时文本门控，可通过 `LOCAL_BARGE_IN_MIN_CONFIDENCE=.94`、
`LOCAL_BARGE_IN_MIN_VOICED_MS=300` 调整。Whisper 批量识别仍等待有效定稿。
以下持续语音门控用于其他 ASR 后端，降低咳嗽、键盘、环境噪声和回声造成的误打断率：

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `BARGE_IN_CONFIRM_MS` | `500` | 持续说话确认窗：用户说话需持续该时长才执行打断；设 `0` 恢复即时打断 |
| `BARGE_IN_TEXT_GRACE_MS` | `500` | 文本确认宽限：确认窗内 ASR partial 无文字时，再给云端该时长的追赶窗口，仍无文字则否决（原因 `no_text`） |
| `BARGE_IN_MIN_REPLY_AGE_MS` | `400` | 回复最小年龄：机器人开口后该时长的语音触发不予打断（防回声） |
| `BARGE_IN_COOLDOWN_MS` | `1500` | 打断冷却：两次打断之间的最小间隔 |
| `VAD_SPEECH_NOISE_THRESHOLD` | 模型默认 | FSMN-VAD 灵敏度透传（`speech_noise_thres`，0.6 默认），越大越严格 |
| `VAD_MAX_END_SILENCE_MS` | `600` | 本地 VAD 端点尾静音阈值（200-6000）。不显式设置时模型动态调度会让短语音挂起长达 2s。注意实际端点 ≈ 配置值 −150ms（语音转静音过渡），550 时约 400ms |
| `NLS_MAX_SENTENCE_SILENCE_MS` | `600` | NLS 云端断句静音阈值（200-6000），与本地并行计数；不要为提高预生成命中率而延长断句等待。 |

门控决策（挂起/确认/取消及原因）会出现在页面时间线的 `strategy` 组件中。
打断为两级确认：① 本地 VAD 持续说话 ≥500ms（快通道）；② 若 ASR 已有过
partial 文本而当前突发始终无文字，多等一个宽限窗口后按噪声否决——
能量持续但无内容的多为噪声。NLS 音频上行在适配器内由独立发送线程完成，
不占用 VAD 消费循环。

## 对话风格与多语言

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `LLM_SYSTEM_PROMPT` | 内置猫猫 persona | system prompt：语音对话风格（简短口语、纯文本、跟随用户语言），可用此变量覆盖 |
| `DUPLEX_PREFETCH_ENABLED` | `0` | 双工独立 VadPrefetch 开关；1 开启。安静收尾期间提前请求，文本与完整上下文一致才接管，不做语义复用。未设置时兼容旧 LLM_SPECULATIVE。 |
| `DUPLEX_PREFETCH_STABLE_MS` | `120` | 安静窗口成立时间；后续 ASR 更新不重等，不改变 VAD 和打断判断。 |
| `DUPLEX_PREFETCH_MAX_PER_TURN` | `3` | 每轮预生成预算，只保留最新候选。 |
| `DUPLEX_PREFETCH_QUIET_RMS` | `0.008` | 仅用于预生成的安静门控，不是 VAD/打断阈值。 |
| `LLM_SPECULATIVE` | 未设置 | 旧配置兼容值；新开关未配置时才读取，不再在 Agent 工厂包装缓存 Bot。两者均未设置时关闭。 |
| `BOT_STATE_DB` | `.local/bot-state.sqlite3` | Bot 独占 SQLite 会话与幂等记录；每个文件只允许一个服务进程。远端使用专用可写状态目录。 |
| `BOT_STATE_RETENTION_DAYS` | `7` | 不活跃会话的保留天数，范围 1 至 30；不保存学生录音。 |
| `BOT_STOP_GRACE_MS` | `150` | Bot 构造下一轮上下文时等待最终停止回执的上限，不阻塞双工停止播放。 |
| `PIPER_MODEL_PATH_EN` | 自动探测 | 英文 Piper 音色路径；默认在主音色目录下找 `en_*.onnx`。按回复文本自动选音色：含 CJK 用主音色，纯拉丁文用英文音色 |

## 对话质量指标（E2E 监控）

`scripts/real_e2e_client.py` 内置四个端到端指标，随测试输出 JSON 报告：

| 指标 | 口径 | 目标 |
|---|---|---|
| 回复延迟 | 用户说完（音频流结束）→ 机器人首个音频帧；另报 ASR 定稿→出声分段 | ≤800ms |
| 误打断率 | 回复播放中注入噪声（`--noise`），3 秒内出现 `playback_interrupt` 计误穿透 | 0 |
| 抢话率 | 回复开声早于该轮用户音频流结束（句中停顿就抢答） | 0 |
| 回复质量 | 语言一致性 / 可朗读性（无 markdown/emoji）/ 长度 1-4 句；相关性自然度用 `scripts/judge_replies.py`（DeepSeek 裁判 1-5 分） | — |

```bash
PYTHONPATH=. python3.11 scripts/real_e2e_client.py \
  --fixture tests/fixtures/en/en_hello_16k.wav \
  --fixture tests/fixtures/en/en_pause400_16k.wav \
  --noise tests/fixtures/noise_burst_800ms.wav
python3.11 scripts/judge_replies.py <报告.json>
```

Fixture 由 `scripts/gen_fixtures.py` 生成（macOS `say` 合成语音 + Python 合成噪声），
覆盖：英/中不同句长、句中停顿 300/400/1500ms、800ms/2s 噪声突发、键盘敲击声。
远程服务端测试加 `--url wss://<域名>/ws --auth user:pass`；服务器访问与日志用
`python3.11 scripts/ssh_deploy.py '<远程命令>'`（凭证读 .env.deploy，不回显）。
场景基线（远程实测）：pause300 不分裂（0 抢话）；400ms 停顿会断句属预期（回复后
用户继续说话由打断接管）；键盘敲击/持续噪声 0 误打断；中文命中时定稿→出声 ~160ms。

## 日志与 Trace

服务端默认输出 JSON 结构化日志。安装 observability extra 后可将 OpenTelemetry span 输出到控制台：

```bash
OTEL_CONSOLE_EXPORTER=1 ./scripts/run_demo.sh
```

前后端运行时事件使用同一事件字段：`eventId/eventType/component/level/sessionId/turnId/replyId/generationEpoch/sequence/timestampMs/monotonicMs/attributes`。

## 当前范围

- 当前媒体传输是 WebSocket PCM，不是 WebRTC。
- 猫动画没有表情、音素口型或视频编码。
- 自动重连、事件导出、声学回声抑制和阈值可视化编辑仍属于后续工作。


### 多用户账户

默认启用账户密码登录。启动服务后打开首页会进入登录页，点击「注册新账户」即可创建账户并登录。账户名为 3–32 位英文字母、数字、下划线或短横线（不区分大小写），密码为 8–128 位。页面右下角可以退出登录。

- 密码通过独立随机盐的 scrypt 哈希存储；登录使用 HttpOnly、SameSite=Strict Cookie，HTTPS 下自动设置 Secure。登录有效期为 7 天，退出后立即撤销。
- 所有学习页面、教材 API、文字聊天和语音 WebSocket 均要求登录。对话会话绑定账户，其他账户不能复用其会话标识；旧的无主对话记录不会自动分配给新账户。
- 学习进度、答题与复习记录、文字草稿和课程选择保存在账户数据库的 `learning_state` 表中。登录同一账户后，打开或刷新页面即可跨设备续学。浏览器仅保留未同步变更用于失败重试；录音仍只在本机临时回放，不上传。
- 升级后会自动将当前账户原先保存在浏览器中的记录导入数据库；仅当对应数据库记录尚不存在时导入。原有未登录的匿名记录不自动归属任何账户。旧浏览器记录保留，清空已同步进度后不会再次导入旧记录。
- 页面显示同步状态，断网后可重试，离开页面会尝试提交未同步变更。另一设备已经修改同一份记录时会提示冲突并停止覆盖；可点「使用云端进度」加载数据库版本，未提交内容会在原浏览器保留恢复副本。关闭页面前应等待「学习档案已同步」。
- 账户、会话与学习档案数据库默认为 `.local/accounts.sqlite3`，可用 `ACCOUNT_DB` 指定持久路径。部署的 systemd 服务使用 `/var/lib/real-time-voice/accounts.sqlite3`，升级与备份时应保留。数据库含密码哈希，勿提交到版本库。
- 自建部署应使用本仓库更新后的 Nginx 配置，关闭原先的共享 Basic Auth，使用应用登录页。仅在隔离开发环境中可设 `ACCOUNT_AUTH_ENABLED=0` 恢复旧模式；生产环境保持启用。
