# 本地演示与验收

## 一键启动

默认演示使用可控 Stub，不依赖大模型，适合稳定展示协议、双工、打断、音频播放和诊断台：

```bash
./scripts/run_demo.sh
```

浏览器访问 `http://localhost:8765`。页面加载时只做服务与浏览器能力检查；只有点击“拨通”才请求麦克风权限。

真实模型模式使用 FunASR、Qwen2.5 0.5B（Ollama）和 Piper：

```bash
./scripts/run_real.sh
```

## 2–3 分钟演示脚本

1. 打开页面，指出顶部服务状态、三列控制台和底部统一事件时间线。
2. 点击“拨通”。观察 `Connecting → Ready → Active`，并听到固定开场白；同时观察机器人 `Thinking → Synthesizing → Speaking`。
3. 对麦克风说一句话。观察 `SpeechStarted → Speaking → SpeechEnded`、实时/最终转写、流式回复和延迟指标。
4. 在机器人生成或播放时再次说话。确认声音立即停止、旧回复保留且标为“已打断”，时间线出现 `barge_in_detected → reply_invalidated → playback_stopped → reply_cancelled`。
5. 使用组件、级别、Turn 筛选器定位事件；勾选“高频包”可查看 100 ms 入播放队列事件。
6. 点击“挂断”，确认进入 `Closing → Closed`，麦克风停止，连接关闭。

## 故障注入

Stub 支持用环境变量稳定复现延迟和失败：

```bash
DEMO_ASR_DELAY_MS=80 ./scripts/run_demo.sh
DEMO_LLM_CHUNK_DELAY_MS=300 ./scripts/run_demo.sh
DEMO_TTS_FRAME_DELAY_MS=120 ./scripts/run_demo.sh
DEMO_VAD_THRESHOLD=0.03 ./scripts/run_demo.sh
DEMO_ASR_ERROR=1 ./scripts/run_demo.sh
DEMO_LLM_ERROR=1 ./scripts/run_demo.sh
DEMO_TTS_ERROR=1 ./scripts/run_demo.sh
```

## 测试命令

```bash
python3.11 -m pytest -q
RUN_REAL_MODELS=1 python3.11 -m pytest -q -m real_model
python3.11 scripts/stability_test.py --duration 1800
```

真实模型用例独立标记，默认回归始终使用注入 Stub，不会意外加载模型。

## 2026-09-07 本机基线

环境：Intel macOS、Python 3.11.15、Chrome 152、本机回环连接。

| 项目 | 结果 |
| --- | --- |
| 默认自动化回归 | 23 passed，2 个真实模型用例默认跳过；连续运行三次均通过 |
| 真实模型门禁 | FunASR + Piper 加载/推理及 Ollama `qwen2.5:0.5b` 流式回复共 2 项通过，38.79 秒 |
| 浏览器 Stub 开场白 | 首音频播放约 132 ms；17 个下行包完整播放；5 秒无回声误触发 |
| 浏览器打断样本 | 从 `barge_in_detected` 到 `playback_stopped` 约 40–50 ms |
| 10 秒稳定性冒烟 | 207 个真实 WebSocket 会话，0 错误，清理前残留任务 0，峰值 RSS 原始值 36,896,768（macOS 为字节） |
| 30 分钟稳定性门禁 | 1800.13 秒、38,708 个真实 WebSocket 会话、0 错误、清理前残留任务 0、峰值 RSS 42,270,720 字节（约 40.3 MiB） |

30 分钟门禁使用同一 `stability_test.py`；任一错误或残留任务超过允许值会以非零状态退出。

## 当前限制

- 媒体链路仍采用 WebSocket + PCM S16LE 16 kHz 单声道 100 ms 包，不是 WebRTC。
- 猫动画只根据音频 RMS 切换开口/闭口图片状态，不包含表情、口型音素或视频编码。
- 自动重连、事件导出、阈值可视化编辑属于 P1，不在本次 P0 验收范围。

## 端到端时间轴验收（2026-10-22）

录音导出 schemaVersion=2。麦克风帧用 AudioWorklet 的 `currentFrame/sampleRate`
定位到录音原点，保留启动间隔、帧间空隙和未上行帧；不再从零直接拼接。
机器人 WAV 文件名为 `robot-scheduled.wav`，表示排程 PCM，并裁掉打断后未播放
的排程，不能称为扬声器实录。`physicalOutput=null` 表示没有独立物理/回环采集。

同一 `replyId` 下必须区分：

| 里程碑 | 口径 |
| --- | --- |
| `firstPacketReceivedMs` | JS WebSocket 回调观察到第一包；不是网卡到达或出声 |
| `firstScheduledCtx` / `firstSignalScheduledCtx` | 第一帧/首个非静音样本计划播放的 AudioContext 时间 |
| `graphOnsetCtx` | 输出旁路 AudioWorklet 观察到非静音样本进入音频图；不是扬声器实听 |
| `deviceOutputEstimateMs` | `getOutputTimestamp` 推算的设备输出时间；明确为浏览器估计 |
| `physicalOutput` | 只有独立校准的物理/回环录音才能填写；人工点击不能代替 |

首包携带服务端同域单调时钟时间戳：输入尾帧收到、定稿处理开始/完成、Bot 请求、
首文本、TTS 开始、首 PCM、媒体帧入队、出队、获取发送锁。发送完成时间通过后续
`playback_stream_started` 补齐。客户端以 `replyId` 合并，缺失保留 null，不能填零。
ASR 定稿事件的客户端到达时间只用于观察事件投递，不能作为回复流水线开始时间。

`timing_ping/pong` 提供服务器与客户端时钟偏移区间，不按 RTT/2 猜单向延迟。
单向结果必须同时给出不确定区间；上行+下行合计可以利用偏移相消计算，但仍包含
WebSocket/JS 投递等待。不同会话、不同 replyId、不同时间域不能直接相减。

`recordingTimelineAudit` 在同一次录音内比较当前时间轴与“同样帧直接拼接”的反事实，
给出 `timelineBiasMs`。该值才是录音原点/空隙造成的偏差，不得把所有剩余差额归到
浏览器。`sourceSignalEndCtx` 采用 abs(PCM16)>=300 阈值，需报告其不是语言学句尾。

独立测试，不启动本地语音服务：

```bash
python3.11 -m pytest -q -m 'not real_model and not real_cloud'
node tests/frontend_timing_test.cjs
node tests/frontend_playback_test.cjs
python3.11 scripts/analyze_latency_trace.py <远端真实语音报告.json>
```

`check_english_voice.py` 的延迟改为非静音样本到首包收到，WAV 文件末尾到首包另列。
它使用按 100ms 发送的测试音频，样本采集时间属于模拟输入模型，播放回执也为模拟；
不能拿它替代真实浏览器/物理出声验收。分析脚本按同一 replyId 的连续时间点闭合，
不把 ASR 处理时长等重叠诊断项重复累加。开场白没有用户输入，相关指标为 null。

### 本轮验证状态（不是性能结论）

- 轻量回归 245 passed，3 deselected；前端时间轴与 9 项原播放时钟/停止场景通过。
- 香港隔离候选服务使用真实 NLS/Piper，开场白的首文本约 8.7ms、TTS 首 PCM
  约 201.8ms、媒体帧等待约 15.1ms。开场白是固定话术，这些数字不能代表 LLM
  回复、浏览器播放或物理出声性能。
- 用户 WAV 测试收到空 ASR 定稿，门禁失败；未改动的线上基线使用同一份既有
  WAV 也出现空定稿。NLS 适配器代码和相关运行环境与线上一致，尚未确定空文本根因。
- Chrome 自动化页面读取/执行超时，浏览器和物理输出的本轮实测未完成。
- 未替换线上版本；隔离候选服务测试结束后停止，本地语音服务未启动。
  `约950ms` 差额仍未归因，不得把失败样本当作有效回复延迟样本。
- 原始对照报告保存在忽略目录 `.local/timing-audit-20261022/`，远端候选目录
  `/var/tmp/rtv-timing.vDvg9y` 留存供后续诊断；无新增公开监听端口。

### 空 ASR 定稿进一步诊断（2026-10-22）

已通过 `scripts/probe_nls_transport.py` 在香港 ECS 的一次性进程中绕过 VAD/Bot
复现，无需启动任何本地语音服务。系统与 certifi 两组 TLS 验证均通过；NLS SDK
和独立 HTTPS Upgrade 请求均收到 HTTP 403。响应分类为 `Meta:ACCESS_DENIED`、
`token invalid`（不记录原始响应里的 Token），不是音频格式、VAD 阈值或 Bot 导致。

诊断证据：认证方式为 `configured_token`，未配置可自动刷新的 RAM 凭证；SDK
只触发 close、没有 start-ready 回调，130 次 `send_audio` 调用时 SDK 内部状态
均为未就绪，全部被忽略。SDK 的底层连接错误未转发到应用的业务错误回调，且
adapter `_on_nls_close` 未记录失败、`_start_recognition_now` 只检查业务错误，
因此握手失败被错误地当作正常空文本。不能仅凭 invalid 推断具体失效时刻。

后续修复需两部分：更新有效 Token 或由用户配置仅具所需 NLS 权限的 RAM 凭证，
并补齐 start-ready / premature-close 检查，使鉴权或连接失败明确报 ASR_FAILED，
保留原有后台发送线程、FIFO flush 和本地 VAD/打断路径。尚未修改线上凭证或部署
该修复；本次是诊断结论，不是通过真实多轮/打断/性能门禁的声明。

浏览器复测：扩展标签页列表、标题及 URL 可即时读取，页面截图和 CDP 调试通道
仍超时。用户提供的权限截图与禁用列表检查排除了已知网站禁止项，但无法据此
确定具体连接故障；没有重装扩展或调整授权。建议按官方流程重开 Chrome 后复测，
仍失败则在新聊天验证连接、收集反馈，而不是继续扩大浏览器权限。

## ECS 角色改造验收与发布（2026-10-22）

后续状态：已于同日完成新版边听边想开关对照及门禁，以仅配置发布
`rtv-20261022-prefetch-on-r2` 开启预生成，后经用户确认发布提示词优化版
`rtv-20261022-prompt-opt-r3`，预生成保持开启；下述 RAM 角色发布的“开关关闭”是该次
发布时状态，不是当前状态。完整指标、局限与备份见
[双工设计：开关对照与配置发布](04-duplex-dialogue-design.md#开关对照与配置发布)。
提示词优化及发布记录见 [后端设计第 11 节](03-backend-design.md#11-首字首句与提示词优化2026-10-22已发布)。

- 轻量回归 **255 passed，3 deselected**；前端时间轴与原有 9 项播放/停止场景通过。
- 角色模式在独立远端进程中已实际取得 STS 身份并调用 CreateToken，握手 101，
  收到 TranscriptionStarted / SentenceEnd / TranscriptionCompleted，非空文本。
  130 次 20ms SDK 发送均处于就绪状态，无手填 Token 参与。
- 使用失效手填 Token 的负向真实握手返回 403，应用明确返回 `NLS_AUTH_FAILED`，
  SDK 音频发送次数为 0，不再作为正常空文本报告。
- 隔离服务 `rtv-ram-e2e-verify` 已跑通四轮真实 WAV → NLS → English Bot/LLM →
  Piper → WS 音频；ASR 内容分别为问候、能力询问、请求慢速和 23+45，最终数字答复
  为 68。报告 `status=passed`、errors=[]、physicalOutput=null。播放回执为测试模拟，
  没有宣称浏览器/物理实听通过。
- 连接中断恢复后，确认两次真实语音打断通过：尚未播放（回执 0ms）及部分播放
  （回执 550ms），`lateOldAudio=0`、errors=[]。播放进度由客户端测试夹具声明，
  不等同于麦克风/扬声器声学测量。
- Bot 恢复与幂等通过：完整请求重放、游标重放、修改请求返回 409、重复播放事实、
  未播放回复不丢失用户事实，以及部分播放后不重复已听前缀。
- 命令验证通过：开场白、命令重试、连续 5 次空闲提醒不挂断；明确告别的挂断动作
  等待对应播放确认，过期确认无效。隔离日志队列溢出、40010005、ASR_FAILED 均为 0。
- **已发布 `rtv-20261022-ram-timing-r1`**，角色自动发现可用，生产配置移除旧 NLS
  Token/AK，保留 Appkey、模型、VAD/打断参数和关闭的预生成开关。56 个 backend/web
  文件 SHA256 与已测试候选及本地一致；依赖使用隔离环境经 HTTPS PyPI 安装并验证
  的版本，存放 `runtime_deps/nls_role`，未修改生产虚拟环境。
- 发布包 SHA256：`443db8f48ce3946d00126c922a4571a42e9a8a33a4a7dbb616bbf9159a124a14`。
  发布前应用、配置、SQLite 备份位于
  `/var/backups/real-time-voice/rtv-20261022-ram-timing-r1`，目录受限，包含旧凭证，
  不应公开或下载到共享目录。发布脚本检查活动会话和源文件变化，并提供失败回滚。
- 发布后通过公网 `wss://aimaoren.com/ws` 两轮真实 WAV → NLS → LLM → Piper 冒烟，
  定稿非空、errors=[]；另通过公网复测未播放/部分播放两次打断，errors=[]、
  `lateOldAudio=0`。隔离服务已停止，本地语音服务未启动；未重启 ECS 或修改安全组。
- 原始隔离报告在 `/var/tmp/rtv-ram-verify.buQBUk/`，本地副本与发布后报告在忽略目录
  `.local/ram-role-evidence/`。失效 Token 负向用例预期为 failed，不计作正常识别成功。
- 浏览器调试通道及实际声学出声尚未验收。现有指标区分首包接收、计划播放、音频图
  输出和设备估计，`physicalOutput=null`；不能据本次脚本测试认定原来的额外约 950ms
  已归因或消除。
