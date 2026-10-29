# 双工语音与文本机器人协议规范

本文是语音双工链路与文本机器人接口的**实现基准协议**（normative），与代码一致：

| 层 | 协议 | 定义文件 | 用途 |
|---|---|---|---|
| L0 | 语音 WebSocket 协议 v1 | `backend/protocol.py` + `backend/app.py` | 浏览器 ↔ 服务端，控制/媒体/观测三条通道 |
| L1 | Bot 服务协议 v2 | `backend/bot_contract.py` | 媒体引擎 ↔ Bot 服务：事实进、内容流出，幂等可重放 |
| L2 | 模型适配协议 v1 | `backend/text_bot.py` | Bot 服务 ↔ 模型适配器（内部边界，无状态） |

版本号互不隶属：L0 的 `version:1` 是 WebSocket 消息版本，L1 的 `version:2` 是
Bot 服务版本，L2 的 `BotRequest.version` 恒为 1（内部模型适配契约）。

---

## 一、L0：语音 WebSocket 协议 v1

连接：`/ws`（生产 wss），aiohttp `WebSocketResponse(heartbeat=10)`。
一条连接承载一个会话；所有 JSON 消息均带 `"version": 1`。

### 1.1 三条下行通道（服务端 → 客户端）

共用同一 socket，靠消息类型区分，**语义上必须按三条独立通道对待**：

| 通道 | 消息类型 | 背压策略 |
|---|---|---|
| 控制面 | `prepare_result`, `create_session_result`, `asr_result`, `assistant_delta`, `playback_interrupt`, `reply_complete`, `reply_failed`, `close_session`, `error` | 不得被媒体面阻塞（duplex 内独立队列） |
| 媒体面 | `avatar_frame`（JSON）+ 二进制音频帧 | 有界缓存，发送节拍 100ms/帧，回复首帧突发发送 |
| 观测面 | `session_event`（timeline）、`runtime_stats` | 满则丢最旧，永不反压控制面 |

### 1.2 连接与会话生命周期

```
客户端                          服务端
  |-- prepare ------------------>| 资源预留（ResourceManager）
  |<-- prepare_result ----------| ok / NO_RESOURCE / AUDIO_FORMAT_UNSUPPORTED
  |-- create_session ----------->| 建 DuplexDialogue，grade∈{6,7,8,9}
  |<-- create_session_result ---| ok + sessionId + textBotProtocolVersion=2
  |                              | ★ 契约顺序：客户端收到 result 后，
  |                              |   服务端才 pick_up（生成开场白）
  |-- audio(二进制, seq=0,1,…) ->| direction=0 校验，sequence 连续校验
  |<-- asr_result/assistant_delta| 流式识别与回复
  |<-- avatar_frame + 音频帧 ----| replyId, generationEpoch, sequence
  |-- client_event ------------->| playback_progress / playback_stopped / playback_completed
  |-- close_session ------------>| initiator=user（或服务端判定再见/错误时主动下发）
  |<-- close_session -----------| initiator=server → 客户端 expectedClose=true
  |<-- closed ------------------| 控制面终止标记
```

### 1.3 上行消息（客户端 → 服务端）

**控制消息**（JSON）：

| type | 关键字段 | 说明 |
|---|---|---|
| `prepare` | `audio{codec,sampleRate,channels,frameDurationMs}` | 仅接受 `pcm_s16le/16000/1/100ms` |
| `create_session` | `grade` | 年级 6–9；决定词表、语速、开场白 |
| `close_session` | `sessionId, initiator` | 用户挂断 |
| `client_event` | `event`（见 1.5 交互事实） | 播放回执，驱动"听到多少"的语义投影 |

**音频帧**（二进制，22 字节头 + 3200 字节 PCM = 3224 字节固定长度）：

```
HEADER_FORMAT = "!HBBQIQ"   # network byte order
| magic 0x5652 (2B) | version 1 (1B) | direction (1B) |
| session_hash (8B, sha256(sessionId)[:8] BE) | sequence (8B) | timestamp_ms (8B) |
| PCM s16le 16kHz mono 100ms (3200B) |
```

- `direction`: 0=上行（客户端→服务端，唯一合法值），1=下行
- `sequence`: 从 0 起单调连续；`speech_time = (sequence+1)*100`（ms）
- 乱序/丢包策略：前向跳号后重同步到新包（`SequenceValidator`），**重复包拒绝**；
  服务端 `QUEUE_OVERFLOW` 时丢最旧上行帧，实时性优先

### 1.4 下行消息（服务端 → 客户端）

| type | 触发 | 关键字段 |
|---|---|---|
| `prepare_result` / `create_session_result` | 握手 | `ok, code, message`；result 附 `sessionId, grade, textBotProtocolVersion, audio` |
| `asr_result` | 每帧 ASR/VAD 结果 | `status`(0静音/1起音/2部分/3定稿), `text`, `startTime, endTime, speechTime, turnId` |
| `assistant_delta` | 回复文本增量 | `turnId, replyId, generationEpoch, text`（只含增量） |
| `avatar_frame` | 每个媒体包先行 | `sequence, avatarState, turnId, replyId, generationEpoch` |
| `playback_interrupt` | 打断/被取代 | `replyId, generationEpoch, reason`(barge_in/superseded), `partialText` |
| `reply_complete` | 该回复全部音频已**发送**（非已播完） | `turnId, replyId, generationEpoch, text`（全文） |
| `reply_failed` | LLM/TTS/播放失败 | `code`(LLM_FAILED/TTS_FAILED/…), `component`, `partialText` |
| `close_session` | 任一方挂断 | `initiator`(server), `reason` |
| `error` | 协议/组件错误 | `code, message, component?, turnId?, replyId?, generationEpoch?` |
| `session_event` | timeline 观测 | `event`（结构见 1.5） |
| `runtime_stats` | 每 0.5s | 各队列深度、收发包计数、errorCount |

下行音频帧与上行同构（`direction=1`），**sequence 独立计数**（重发/突发不受上行影响）。

### 1.5 交互事实（client_event 载荷，也是 L1 的 interactions）

`web/app.js` 自动为 playback 类事件按 replyId 递增 `sequence`；服务端同样记账，
两端 sequence 语义一致：

| eventType | 必填 | 语义 |
|---|---|---|
| `playback_progress` | `replyId, sequence, playedMs` | 播放中周期上报（客户端 ticker） |
| `playback_stopped` | `replyId, sequence, playedMs` | 打断/中断（含 before_playback 零播放） |
| `playback_completed` | `replyId, sequence` | 该回复音频全部播完 → 服务端 `_finalize_reply` 写历史 |
| `generation_cancelled` | `replyId` | 服务端在 `_interrupt` 时自行记录（duplex→observe） |
| `speech_segment` | `replyId, sequence, text, audioEndMs` | 服务端记录：TTS 文本段与音频偏移对齐（duplex→observe） |
| `text_displayed` | `replyId` | 文本通道消息已展示（text_api 用） |

约定：**"发送"不等于"听到"**。语义历史只允许从已确认交付的内容投影
（`bot_service.delivered_text`：按 speech_segment 的 audioEndMs 与
playback_stopped.playedMs 截断），`reply_complete` 仅表示发送完毕。

### 1.6 打断（barge-in）判定（服务端策略，非协议字段）

`duplex._maybe_barge_in`：VAD status=1 起音后进入 pending，须同时满足：

1. 持续说话 ≥ `BARGE_IN_CONFIRM_MS`（默认 500ms）
2. 期间出现 ASR 部分文本（噪声有能量无字；`BARGE_IN_TEXT_GRACE_MS`=500ms 宽限）
3. 当前回复年龄 ≥ `BARGE_IN_MIN_REPLY_AGE_MS`（默认 400ms）
4. 距上次打断 ≥ `BARGE_IN_COOLDOWN_MS`（默认 1500ms）

确认后 `_interrupt`：`generationEpoch+1`（围栏失效旧回复）、丢弃排队媒体、
下发 `playback_interrupt`，并记录 `generation_cancelled` 事实。

---

## 二、L1：Bot 服务协议 v2

定义：`backend/bot_contract.py`。连接载体：进程内调用（voice 经
`BotSession` 适配；text_api 经 HTTP，见 `backend/text_api.py`）。
核心思想：**媒体引擎不构造语义历史、不存 bot 状态**；bot 服务自己拥有账本。

### 2.1 请求（ConversationRequest，version=2）

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `requestId` / `sessionId` / `turnId` | 128 位内 ASCII `[A-Za-z0-9_-]` | 标识符 |
| `event` | `session_start` / `user_message` | 开场白 / 用户消息 |
| `channel` | `voice` / `text` | 影响交付投影（text 无播放概念） |
| `text` | ≤4000 字符 | user_message 必填非空 |
| `options` | Mapping | 如 `{"grade":7}`；会话内不可变更（`BOT_SESSION_OPTIONS`） |
| `replyId` | 可选 | 缺省取 requestId |
| `generationEpoch` | int ≥0 | 打断代际；新代际可取消未完成的上一代运行 |
| `interactions` | ≤256 条事实 | 自上次请求以来累积的交互事实 |
| `afterEventSeq` | int ≥0 | 断线重连重放游标（resume） |

`fingerprint()` = 请求全字段（除 `afterEventSeq`）的规范化 JSON——
**同一 requestId 重发必须指纹一致**（`BOT_ID_CONFLICT`），这是幂等键。

### 2.2 响应（BotEvent 流，version=2）

| type | 字段 | 说明 |
|---|---|---|
| `text_delta` | `text` | 增量文本（服务端持久化并编 `eventSeq`） |
| `done` | `finishReason` = stop/length | 终止事件，必须恰好一个 |
| `error` | `code, message` | 终止事件；结构化错误码不泄漏 provider 细节 |

约束：事件携带单调 `eventSeq`（1 起）；`afterEventSeq` 重放从游标后续传；
订阅数归零且未终止 → 取消生成（客户端消失即止损）。

### 2.3 会话与并发规则

- `session_start` 同一 sessionId 只允许一个 requestId（`BOT_SESSION_EXISTS`）
- 单会话单进行中的生成：前一代未被 `generation_cancelled` 事实取消、
  且 epoch 未推进 → `BOT_SESSION_BUSY`
- 打断后语义投影等待客户端 `playback_stopped` 回执，宽限
  `BOT_STOP_GRACE_MS`（默认 150ms，上限 500）——`settle_delivery`
- 单会话请求数上限 1000（`BOT_SESSION_LIMIT`）

### 2.4 语义历史投影（model_query）

Bot 服务从账本投影 v1 消息列表（≤32 条 / 6000 字符）：

- user 消息：每条 `user_message` 请求的 text
- assistant 消息：`delivered_text()` 结果——**只含确认听到的部分**
  （voice 按播放位置截断 speech_segment；text 要求 done + text_displayed）
- `deliveryContext`（≤2 条）：被打断回复的 `{replyId, generatedText,
  deliveredText, status:"not_fully_delivered"}`——模型据此知道"生成过但
  没被听到"，仅在用户明确说"继续/重复"时恢复该草稿

---

## 三、L2：模型适配协议 v1

定义：`backend/text_bot.py`。这是 Bot 服务与具体模型之间的**内部边界**，
适配器必须无状态：全部上下文由 Bot 服务以上下文消息形式提供。

### 3.1 BotRequest（version=1）

- `messages`：≤32 条 `BotMessage(role=user|assistant)`，总长 ≤12000 字符；
  `user_message` 必须 user 结尾，`session_start` 必须为空
- `options` / `reply_id` / `generation_epoch` / `delivery_context`（≤2 条，
  字段固定 `replyId/generatedText/deliveredText/status`）
- 全部标识符校验同 L1

### 3.2 BotEvent 与流约束（stream_bot 统一校验）

- 事件 `request_id` 必须与请求一致（关联校验）
- 必须恰好一个终止事件（done/error）；done 前无文本 → `BOT_EMPTY_REPLY`
- 超时 30s 按"等待 bot 的时间"计费（yield 给 TTS 的时间不计）
- 输出上限 4096 字符；异常统一映射为 `TextBotError`
  （BOT_TIMEOUT/BOT_PROTOCOL_ERROR/BOT_FAILED/BOT_UNAVAILABLE），
  不向上泄漏 provider 异常

### 3.3 双工侧可选预生成（VadPrefetch）

```text
offer(request)        双工提交 ASR 中间文本，非阻塞；安静窗口成立后提前生成
seal(turnId)          VAD 定稿边界，停止启动新候选并记录时间
stream(request)       走正式 L1 接受与历史投影；输入和上下文逐字一致才接管
cancel_pending()      双工清后台候选（同步取消，打断时不等待内容任务）
aclose()              回收后台任务
```

实现为 `backend/vad_prefetch.py`，属于双工领域；默认 Agent 工厂不再包装
SpeculativeTextBot/SpeculativeAgent。旧 PreparingTextBot 和缓存类只保留为兼容代码，
不是 L1 Bot 的接入要求。普通 Bot 仍只实现 stream/observe/aclose。

BotSession 为明确 side_effect_free=True 的 L2 生成器提供可选的 generation_snapshot
适配器。快照由 Bot 构造，不写正式请求、不推进对话；加速器只比较完整请求身份，
不解释历史或回滚业务状态。正式请求仍通过 BotService 接受、校验幂等、重新投影并
逐条持久化输出，再消费匹配候选。未知或有副作用的 Bot 不提前调用，正常接入不受影响。

DUPLEX_PREFETCH_ENABLED 控制开关；旧 LLM_SPECULATIVE 仅作未配置新开关时的兼容值，
两者都没有时关闭。安静门控默认 120ms，一旦成立后续文本更新不重等；每轮最多 3 次，
只保留最新候选。不做前缀/语义/礼貌词复用或 near-miss grace。
effectiveLeadMs 记录被采用候选的有效提前量，remainingWaitMs 记录定稿后剩余等待；
adoptedReady 和命中率不能替代实际开关对照结果。

`adapt_text_bot()` 仍识别 v1 协议或 legacy `stream_reply` 接口
（`LegacyTextBotAdapter` 翻译）。L2 不感知 v2；模型历史仍只由 Bot 投影。

---

## 四、标识符与错误码速查

**标识符**：`sessionId`（`sess_*`）> `turnId`（`turn_*`，一次用户说话）>
`replyId`（`reply_*`，一次回复）> `requestId`（bot 服务幂等键）。
`generationEpoch` 每开新一轮回复 +1，是打断围栏的失效判据。

**主要错误码**：

| 层 | 码 |
|---|---|
| L0 协议 | `PROTOCOL_VERSION_UNSUPPORTED`, `INVALID_MESSAGE`, `AUDIO_SEQUENCE_ERROR`, `QUEUE_OVERFLOW`, `INVALID_PLAYBACK_EVENT`, `INVALID_GRADE`, `NO_RESOURCE` |
| L0 组件 | `ASR_FAILED`, `LLM_FAILED`, `TTS_FAILED`, `PLAYBACK_FAILED`, `REPLY_FAILED` |
| L1 服务 | `BOT_ID_CONFLICT`, `BOT_SESSION_EXISTS`, `BOT_SESSION_BUSY`, `BOT_SESSION_OPTIONS`, `BOT_SESSION_LIMIT`, `BOT_UNKNOWN_REPLY`, `BOT_EVENT_CONFLICT`, `BOT_RESTARTED`, `NO_RESOURCE` |
| L2 适配 | `BOT_TIMEOUT`, `BOT_PROTOCOL_ERROR`, `BOT_FAILED`, `BOT_EMPTY_REPLY`, `BOT_OUTPUT_LIMIT`, `BOT_UNAVAILABLE` |

---

## 五、设计取舍备忘

1. **发送 ≠ 听到**：reply_complete 只表传输完成；语义只信 playback 回执。
   被断回复进历史的是"已听前缀"，未听部分经 deliveryContext 告知模型。
2. **幂等优先于速度**：fingerprint 去重 + 账本重放，允许客户端安全重试/重连。
3. **控制面永远优先**：三条队列物理分离，媒体积压不得延迟打断与挂断。
4. **媒体引擎零语义**：历史构造、交付判断、幂等全部在 bot 服务侧，
   模型适配器无状态——LLM 可水平替换。
5. **草稿永不直接播出**：边听边想的产物只有经 L1 采纳（输入+上下文一致）
   才能进入 TTS。

## 六、延迟诊断扩展（不改变媒体协议或双工策略）

L0 新增可选 `timing_ping {pingId}`，响应 `timing_pong` 包含 `pingId`、
`serverReceivedMs`、`serverSentMs`、`clockDomain: server_monotonic`。
不要求会话、不消耗 Bot 状态、不执行任何业务命令；pingId 限 1–64 字符。

首个下行 `avatar_frame.timing` 关联 `turnId/replyId/generationEpoch` 的服务端时间点，
后续 `session_event(playback_stream_started).attributes.timing` 补齐发送完成时间。
PCM 包头、100ms 帧长、序列验证、发送锁和 epoch 围栏均不变。时间戳只作观测，
不能驱动打断或推进 Bot 历史；Bot 请求/响应标准接口不变。

事件 envelope 的 `clockDomain` 显式区分 `server_monotonic` 与 `client_performance`。
前端导出 schemaVersion=2 的录音日志和同 replyId 阶段表。设备估计、音频图输出、
计划播放、首包收到分列；没有实测时 `physicalOutput` 必须为 null。完整验收口径见
`05-demo-and-acceptance.md` 的“端到端时间轴验收”。

### ASR 认证错误扩展

仍使用原有 `error.code=ASR_FAILED`，可选 `reasonCode` 区分 NLS 角色凭证获取、
Token 刷新、鉴权、未就绪和提前断连。原因码只接受服务端白名单，公开 message
由固定安全文本生成，不传输原始 SDK 异常、Token 或签名。Bot 标准接口、语义状态、
打断与媒体帧协议不变。认证模式和控制台配置见后端设计第 10 节。
