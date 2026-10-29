# Real-time Voice Avatar

## 按单元检索的分层对话

`/text.html` 支持 3–9 年级教材选择。SQLite 保存单元词汇、累计词汇快照和已审核问答，
依次使用有原文依据的教材回复、提前编写的常见问答；两层回复均按当前累计词汇范围过滤。
只有找不到可靠匹配时才调用 Deep Agents + 现有 DeepSeek，一次生成常见、简短、适龄的回复，
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
范文先检索网上原文，取得主题、长度和已学词汇检查均通过的短文时提供原文链接和节选，否则用 DeepAgents + DeepSeek 生成原创范文。
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

VAD 检出"开始说话"不会立即打断机器人，而是通过四层门控降低误打断率
（咳嗽、敲键盘、环境噪声、机器人回声都会被过滤）：

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
