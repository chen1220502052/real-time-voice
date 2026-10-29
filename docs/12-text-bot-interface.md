# 文本 Bot 标准接口与状态一致性

版本：TextBot v2。更新日期：2026-10-21。

2026-10-23 增补：文字 HTTP 接口默认使用 `CurriculumChatBot`（Deep Agents + DeepSeek）。
`options` 必须包含 `chapterId`，可同时传入与教材匹配的整数 `grade`（4–9）；
年级、单元不匹配或未知选项在接收生成任务前返回 400。`GET /api/text-bot/scopes/{chapterId}`
返回累计词汇数量、来源、缺失册次及 `chatPolicy`。同一会话不能改变 `options`，切换教材应创建新 sessionId。
最终回复独立检查：表外词种类最多 1，出现次数 / 英文 token 数 ≤ 3%；专有名词和学生输入词不自动豁免。
工具可检查草稿；服务端最多生成三次，只发送通过检查的最终文本，故文字页面等待审核后整条显示。
失败返回 `CHAT_VOCABULARY_FAILED`，不发送失败草稿；依赖、配置错误分别为 `CHAT_NOT_INSTALLED`、`CHAT_NOT_CONFIGURED`。
原语音 Bot 及测试注入的旧 Bot 仍支持原来的仅年级选项。

2026-10-22 增补（已发布：rtv-20261022-short-stream-r5）：`text_delta` 本就是任意长度的文本增量，不保证完整句。
语音通道的英语 Bot 可以在整句结束前交付完整单词前缀；文本通道仍保留整句策略。
公共请求/事件字段、eventSeq 重放、幂等规则不变；内部模型请求由现有 channel 选择输出策略。
双工自行决定短首段和后续自适应分段，其他 Bot 不必支持播放缓冲或预合成接口。
提前交付后若生成失败，返回 error 而非将半句话标记 done；已播前缀不能撤回，未播内容仍由
speech_segment 与真实播放回执决定是否进入下轮历史。实现与门禁见后端设计第 12 节。

双工只报告交互事实并消费流式文本；Bot 服务负责会话历史、内容状态、播放事实的语义解释、请求幂等和恢复。上游不裁剪助手历史，不保存或回传 Bot 状态。当前仍部署为单个 Python 服务，模块之间使用标准契约，不要求拆成两个网络服务。

## 职责边界

| 双工与客户端 | Bot 服务 |
| --- | --- |
| ASR partial 和 final、用户输入、稳定关联 ID | 保存输入、生成记录与交互事件 |
| 打断门控、取消栅栏、音频队列与流式 TTS | 从播放事实构造模型历史，保留未播放草稿供语义续接 |
| 报告播放进度、停止、完成与 TTS 文本音频对齐 | 请求去重、流式重放、取消和重启后的终态恢复 |
| 打断正确率、抢话率、设备播放与交互延迟 | 回复内容、多轮记忆、年级策略和内容状态一致性 |

双工不读取提示词、业务状态字段或模型供应商参数。Bot 不操作麦克风、音频队列或播放，不决定何时执行打断。sessionId 是关联标识，不是账号身份或访问授权。

职责总览及打断恢复时序见 [聊天架构中的职责与服务边界](11-english-tutor-agent-architecture.md#职责与服务边界)。结构图只表达逻辑职责，时序图单独表达立即停播、异步事实转交与 Bot 状态恢复，避免把音频路径和内容状态混为一条链路。

## 标准生成请求

入口：POST /api/text-bot/replies。

```json
{
  "version": 2,
  "sessionId": "sess_001",
  "requestId": "req_002",
  "turnId": "turn_002",
  "replyId": "reply_002",
  "generationEpoch": 3,
  "event": "user_message",
  "channel": "voice",
  "text": "I like playing basketball.",
  "options": {"grade": 7},
  "interactions": [
    {"eventId":"stop_001","type":"playback_stopped","replyId":"reply_001","sequence":8,"playedMs":530}
  ],
  "afterEventSeq": 0
}
```

- version 固定为整数 2；sessionId、requestId、turnId 由调用方稳定生成，限 1 至 128 个 ASCII 字母、数字、下划线或连字符。
- replyId 默认等于 requestId；同一会话的 requestId、replyId、turnId 不得复用于另一轮生成。generationEpoch 是语音取消栅栏，不是业务状态版本。
- event 为 user_message、session_start、time_out 或 hang_up。仅 user_message 的 text 非空（最多 4000 字符）；命令的 text 必须为空。connect、pick_up 是 session_start 的规范化别名，在指纹计算前统一；接通使用新 sessionId。
- channel 为 voice 或 text，一个会话内不能切换；options 属于 Bot。原语音实现接受 grade=6/7/8/9，默认 7；默认文字实现要求 chapterId，可附匹配的 grade=4–9。切换选项需新会话。
- 不接受 messages、任意 system prompt 或 Bot 状态。Bot 自己加载会话记录并构造模型上下文。
- interactions 是可选事实批次，最多 256 条。语音桥接携带近期事实，避免后台写入尚未完成就请求下一轮；它不是语义对话历史。
- afterEventSeq 是断点重放游标，不属于生成内容，不改变请求指纹。HTTP 请求体上限 64KiB。

同一轮重试必须使用相同内容。新到达的播放事实走独立事件入口，不通过修改原请求体重试提交。

## 流式响应与幂等

响应为 HTTP 200，Content-Type 为 application/x-ndjson；每行一个事件，不是 SSE。

```json
{"version":2,"requestId":"req_002","eventSeq":1,"type":"text_delta","text":"That sounds fun. "}
{"version":2,"requestId":"req_002","eventSeq":2,"type":"text_delta","text":"Who do you play with? "}
{"version":2,"requestId":"req_002","eventSeq":3,"type":"done","finishReason":"stop"}
```

失败终态示例：

```json
{"version":2,"requestId":"req_002","eventSeq":2,"type":"error","code":"BOT_CANCELLED","message":"Reply generation was cancelled"}
```

Bot 在调用模型前保存请求指纹，逐条保存输出后再交付消费者。eventSeq 从 1 递增，重放保持内容与顺序。

| 情况 | 行为 |
| --- | --- |
| 相同 sessionId 和 requestId，相同内容，仍在生成 | 订阅同一任务，不新增模型调用 |
| 相同请求已结束 | 重放保存事件，不重新计算历史或调用模型 |
| 相同 requestId，内容不同 | HTTP 409 BOT_ID_CONFLICT，不改变已接受请求 |
| afterEventSeq=N | 只返回大于 N 的事件，失败终态同样可重放 |
| 最后一个订阅者取消或断线 | 取消生成、释放容量，保存 BOT_CANCELLED；原 ID 不重新生成 |
| 进程在已接受请求完成前重启 | 保留已保存增量，补 BOT_RESTARTED；不自动重试供应商 |

重新生成失败回复需要新的 requestId、turnId、replyId。幂等保证限定在记录保留期内，不是供应商侧分布式事务，也不保证断电时一定生成完全文。已完成重放不占模型容量，其他语音会话运行时也可重放。

成功流必须有非空文本和一个 done；失败以 error 终止。done 只表示文本生成完成，不表示已播放或显示。

## 命令、进度推送与动作（v2 能力扩展）

health 与 create_session_result 的 textBotCapabilities 宣告 commands、events、actions、actionWhen 和 pushScope。现有消费者需识别 progress/action，不能将控制动作当作 TTS 文本；PCM/WebSocket 媒体协议仍为 v1。所有命令使用相同请求入口、关联 ID、持久化与重放规则，而不是把控制信号拼进用户文本。

| 输入命令 | 当前英语 Bot 策略 | 双工职责 |
| --- | --- | --- |
| connect / session_start | 按年级返回开场白，不调用模型 | create_session 成功后恰好下发一次并播放 |
| time_out | 主动确认用户是否还在；任意次数均只温和询问，不告别、不产生挂断动作 | 默认空闲 30 秒后报告；生成/播放中或用户讲话中不触发 |
| hang_up | 返回告别文本，随后 action(hang_up)，最后 done | 等待本回复真实播放完成回执再挂断 |

超时计数由 Bot 从已交付的 time_out 请求记录计算，仅用于询问措辞，不作为挂断条件。未播放提醒不推进计数；新的用户消息重置计数。双工只检测“多久没有交互”，不编写提醒话术；任何一侧都不得仅因用户持续空闲而主动挂断。BOT_IDLE_SECONDS=0 可关闭自动空闲检测；用户手动点击挂断、断线和紧急关闭仍立即执行，不等待告别。

语音控制入口支持 `{"type":"bot_command","version":1,"requestId":"cmd_001","command":"time_out"}`，command 可为 time_out 或 hang_up。相同连接内相同 requestId/command 不重复执行，ID 冲突报错；最多 512 个显式命令 ID，超限需新会话。connect 由 create_session 负责，不允许客户端重复触发开场。HTTP 则使用标准生成请求的 event 和稳定 ID；相同命令的重试仍重放原事件。

进度示例（同一未完成请求中的主动输出）：

```json
{"version":2,"requestId":"req_task","eventSeq":1,"type":"progress","pushId":"working_1","text":"I'm still working on that. Thanks for waiting. "}
{"version":2,"requestId":"req_task","eventSeq":2,"type":"text_delta","text":"Here is what I found. "}
{"version":2,"requestId":"req_task","eventSeq":3,"type":"done","finishReason":"stop"}
```

Bot 的异步迭代器可在等待工具/模型时 yield progress，不必等任务完成。后台工作线程或任务也可调用可信进程内接口 `BotService.push_progress(session_id, request_id, generation_epoch, push_id, text)`，经同一持久化事件流主动送达正在监听的双工。它不是定时拉取，也没有对学生公开的任意话术注入 HTTP 端点。跨进程 Bot 应在原 NDJSON 长连接上推送同类事件；本版不提供脱离活动请求的广播、离线通知或新回合主动外呼。

progress 必须是最多 240 字符的完整可播话术，每请求最多 8 条，并计入 4096 字符总输出限额。相同 pushId/内容返回相同已保存事件，不再次入流；改内容返回 BOT_ID_CONFLICT，旧 epoch、新的迟到推送、终态后推送或 action 后推送返回 BOT_STALE_PUSH；已有推送的幂等查询可在终态及重启后重放。写入与普通事件串行化，先落盘再发布，eventSeq 不重复。

默认语音路径不再调用内容生成器的 can_adopt/can_prepare。预生成调度和精确接管属于双工侧 VadPrefetch；外部 Bot 只需正常流式接口，不需要实现内容相似度或缓存策略。只有具备无副作用生成适配器时才开启提前调用。

英语 Bot 已对单次模型流读取超过默认 4 秒的等待提供最多两条进度话术（BOT_PROGRESS_DELAY_SECONDS），不重启模型请求；已有正式回答时不再自动插入等待话术。主动 progress 共用回复的 replyId/epoch、TTS、播放事实、抢话门控和取消流程；打断会关闭底层任务，旧任务不能创建新的回复。请求总超时不被 progress 无限续期。进度话术只有确认交付后才进入助手历史，未播内容仍属草稿。

挂断动作示例：

```json
{"version":2,"requestId":"req_end","eventSeq":1,"type":"text_delta","text":"It was nice chatting. Talk to you soon! "}
{"version":2,"requestId":"req_end","eventSeq":2,"type":"action","action":"hang_up","when":"after_playback"}
{"version":2,"requestId":"req_end","eventSeq":3,"type":"done","finishReason":"stop"}
```

动作白名单目前只有 hang_up，必须有前置非空话术，一个回复最多一个动作，action 后只允许终态。普通模型文本、Markdown、JSON 中的“挂断”字符串都不能执行动作；当前英语 Bot 仅在显式 hang_up 命令或用户精确告别表达时产生结构化动作。time_out 无论累计多少次都不能产生挂断动作，后续业务 Bot 也须遵守这一空闲规则。

消费者以 (sessionId, requestId, eventSeq) 去重动作；双工只保存当前 replyId/epoch 的待执行动作，收到本回复 playback_completed 且已发出 reply_complete 后执行。发送完 PCM 不代表播放完；旧回执、playback_stopped、打断和新 epoch 会使旧动作失效。没有播放完成回执则不因生成结束而强制挂断。文本页面将动作解释为结束当前文字会话，而非关闭浏览器。

## 交互事实入口

入口：POST /api/text-bot/interactions。用于独立提交晚到的回执；每批 1 至 256 条。

```json
{
  "sessionId": "sess_001",
  "interactions": [
    {"eventId":"segment_001","type":"speech_segment","replyId":"reply_001","sequence":1,"text":"Cats are lovely. ","audioEndMs":500},
    {"eventId":"progress_001","type":"playback_progress","replyId":"reply_001","sequence":6,"playedMs":510},
    {"eventId":"stop_001","type":"playback_stopped","replyId":"reply_001","sequence":8,"playedMs":530}
  ]
}
```

| type | 含义与附加字段 |
| --- | --- |
| speech_segment | TTS 文本段完成合成；text 保留原空白，audioEndMs 是段末在本回复 PCM 内的累计位置 |
| playback_progress | 客户端确认进度；playedMs 不包含排队音频和播放间隙 |
| playback_stopped | 客户端停止时的最终 playedMs，未开始时为 0 |
| playback_completed | 客户端确认本回复播放尾部完成 |
| generation_cancelled | 双工废弃生成任务，不代表音频从未播放 |
| text_displayed | 文字消费者确认已完整接收并显示成功回复 |

事实包含稳定 eventId、replyId 和非负整数 sequence。sequence 用于同一回复播放事件的因果排序；重复事件使用同一 eventId。相同 ID 相同内容不重复应用，不同内容返回 409、整批回滚。不能把其他会话的回复进度写入当前会话。位置必须是有限非负数字，拒绝布尔值、NaN、负数和字符串。

浏览器通过现有 client_event 上送事实，语音桥接规范化到同一序列；TTS 对齐由服务端提供。浏览器每 250ms 按 AudioContext 时钟报告进度，在停止音源之前采样最终位置。不能用已接收包数、已调度包数或服务器发送位置替代。完成回执检查音频时钟，AudioContext 挂起时墙上时间到期不算完成。这仍不是物理扬声器或人耳注意力的证明。

## 内容状态恢复

Bot 保留完整生成记录和事实，不改写已发生的事件；从事实投影有效上下文，不让旧请求回写一个可能覆盖新状态的业务快照。

| 交付状态 | 模型 messages 中的助手内容 |
| --- | --- |
| 未开始播放或没有确认进度 | 不加入助手消息 |
| 部分播放 | 只保留段末位置不超过确认进度的连续完整 TTS 段 |
| 完成播放且生成成功 | 完整回复 |
| 文字生成成功且收到 text_displayed | 完整回复 |

部分 TTS 段不按字符比例推算。当前没有音素级强制对齐，可能保守遗漏实际已播放的半段，不能让模型猜测播放事实。

用户输入在接受请求后保存，不随助手打断回退。未播放草稿与已交付前缀进入独立 deliveryContext，由英语 Bot 提示词标注“不是已经说过的话”。模型可据此响应“继续说”，但不能把未播放问题当作学生已听到的问题。只携带最近两条未完整交付回复，不向 assistant 历史塞“没听到的后半句是……”备注。

停止是不可逆事实：晚到 completion 或停止后的进度不能把被打断回复提升为完整交付；重复和较旧进度不会让已确认位置倒退。Bot 只在构造内容上下文时最多等待 BOT_STOP_GRACE_MS（默认 150ms，最多 500ms）接收最近打断的最终停止回执，超时采用已有确认段。双工停止播放和清理旧音频不等待这一窗口。更晚回执修正后续请求，不能回改已发出的新回复。

客户端应在实际停止目标 replyId 的音源时立即发送 playback_stopped；从未开始也需发送 playedMs=0。不能在通话结束时为所有历史 replyId 批量补 playback_completed；已停止的回复不得再确认完成。重复停止只报告一次，迟到的旧回复打断不得停止新回复的音源。已有最终停止事实时，Bot 不消耗回执宽限窗口。

## 预生成与双工保持条件

进程内 ConversationBot v2 提供 stream(request)、observe(sessionId, facts)、aclose()。不要求文本 Bot 实现 prepare、候选缓存或语义复用；预生成由双工独立模块 backend/vad_prefetch.py 管理，DUPLEX_PREFETCH_ENABLED=0 可完全关闭。旧 LLM_SPECULATIVE 仅在新开关未设置时作为兼容值。

BotSession 可选只读适配器 generation_snapshot(query) 返回由 Bot 自行投影的不可变模型请求；仅对 side_effect_free=True 的内部生成器开放，普通有状态/副作用 Bot 不会被提前调用。此适配器不写正式请求，不推进业务状态，也不是新增的外部 HTTP 必选接口。有状态 BotService 与无副作用内容生成可以共存，不要求复制或回滚 Bot 数据库。

安静帧后默认去抖 120ms，主要利用 VAD 收尾窗口；安静窗口一旦成立，后续文本更新立即重新请求，不重新等待 120ms。默认每轮最多 3 次，只保留最新候选。定稿时不等待候选启动；模型输入文本与已投影的全部上下文必须逐字一致才接管，大小写、标点、礼貌词、数字或对象变化都重新生成，不做前缀覆盖率、语义复用或 near-miss grace。

正式请求仍先经过 BotService 幂等接受、最新历史投影与持久化流程；可选 generator 回调只替换内容流来源，不跳过账本。草稿输出包括 action 在内都不能提前发布，候选失败不伪造成功。已接受请求的重放不重新调用生成器。

effectiveLeadMs 是被采用候选从启动到 VAD 定稿的有效提前量，remainingWaitMs 是定稿后到首段可用文本的等待，adoptedReady 是接管时是否已有文本，adoptionWaitMs 是接管后到首次消费文本的耗时。命中但未就绪也可能节省时间；提前量不能替代实际开关对照收益。日志只含计时、标识和计数，不包含完整草稿。

语音侧保留 replyId 与 generationEpoch 栅栏、独立有界控制和媒体队列、原打断门控及 100ms PCM 帧。生成超时只计算等待 Bot 的时间，不计 TTS 背压；默认 30 秒、4096 输出字符。英语 Bot 按完整短句输出，词数是软目标，异常断流不伪造成功。

Bot 可先于 TTS 完成生成并保存结果；这不表示交付，也不会让未播放文本进入语义历史。取消网络调用不保证供应商停止计费。

### 故障边界补充（2026-10-21）

响应头发出前就建立订阅所有权，准备响应失败或最后一个订阅断开会取消尚未结束的生产任务并释放容量。同一会话串行接受请求，不同会话的重放不等待其他会话的停止回执宽限窗口。

存储保存故障发生在 HTTP 响应头发出后时，只结束不完整流，不补造未提交的 done/error；消费者必须把缺少终止事件视为失败，不能视为完整成功。随后的新请求返回 503，health 返回 503，需先恢复存储。现有已提交事件不因此改写，服务重启后原 running 请求记录为 BOT_RESTARTED，保留不重复执行语义。

语音客户端漏发 playback_completed 时，全部 PCM 发送后最多再等待 PLAYBACK_ACK_TIMEOUT_SECONDS（默认 15 秒），随后 reply_failed，code=PLAYBACK_ACK_TIMEOUT。该失败不等同于已播放或用户主动停止；Bot 仍只使用已确认完整段构造历史，after_playback 挂断动作失效。正常完成、打断及关闭会话都会取消旧回复计时，旧计时或回执不得影响新回复。WebSocket/PCM v1 和 TextBot v2 的字段、命令及正常流格式不变。

## 持久化与运行约束

BotStore 使用 SQLite 保存文字、请求指纹、输出事件和交互事实，不保存学生录音。数据库工作在后台线程执行，不在音频发送或打断路径等待。默认路径为项目 .local/bot-state.sqlite3，远端配置 BOT_STATE_DB=/var/lib/real-time-voice/bot/state.sqlite3。专用目录 0700，数据库与锁文件 0600。

当前只支持一个服务进程拥有数据库，文件锁阻止第二个 worker 同时启动。多实例需要共享事务存储、任务所有权和订阅传输，不能直接增加 worker 数量。

默认保留最近活跃会话 7 天，BOT_STATE_RETENTION_DAYS 可设 1 至 30 天；启动及接受新请求时按小时检查过期会话。每会话最多 1000 个生成请求。模型历史限制为最近最多 32 条完整消息、约 6000 字符，原始记录不因此改写。共享试用口令保护 API，但不提供学生级身份隔离，sessionId 不能作权限凭证。

## 兼容与错误

WebSocket 控制与 PCM 协议仍为 v1，建会和 health 返回 textBotProtocolVersion=2，health 返回 textBotStateOwner=bot。文字页面、命令行与语音桥接使用服务 v2。

backend/text_bot.py 的 BotRequest v1 是内部无状态模型适配器；EnglishChatBot 与旧 stream_reply 经此层调用模型。HTTP v1 只为旧客户端兼容，不拥有 v2 状态与幂等保证；新调用方必须显式发送 version=2。

| 错误 | 行为 |
| --- | --- |
| 400 INVALID_BOT_REQUEST | 格式、选项或版本不合法 |
| 413 | 请求体超过限制 |
| 429 NO_RESOURCE | 新生成容量不足，尚未接受生成，可原样重试 |
| 503 BOT_STORAGE_FAILED | 已检测到输出日志保存失败，拒绝新生成；恢复存储并重启后按幂等日志恢复，不自动重跑已接受的请求 |
| 409 BOT_ID_CONFLICT 或 BOT_EVENT_CONFLICT | ID 内容冲突 |
| 409 BOT_SESSION_BUSY 或 BOT_SESSION_OPTIONS 或 BOT_SESSION_EXISTS | 会话不允许此操作 |
| 409 BOT_UNKNOWN_REPLY 或 BOT_SESSION_LIMIT | 回复不属于会话，或达到会话限制 |
| 流内 BOT_TIMEOUT、BOT_UNAVAILABLE、BOT_PROTOCOL_ERROR、BOT_EMPTY_REPLY、BOT_OUTPUT_LIMIT | 生成失败，HTTP 200 不代表成功 |
| 流内 BOT_CANCELLED、BOT_RESTARTED | 已接受请求取消或服务重启，原 ID 仅重放终态 |

## 验证入口

```bash
python3.11 -m pytest -q -m 'not real_model and not real_cloud'
node tests/frontend_playback_test.cjs
node --check web/app.js
PYTHONPATH=. python3.11 scripts/check_english_chat.py --url http://127.0.0.1:8874 --grades 6 7 8 9
PYTHONPATH=. python3.11 scripts/check_english_voice.py --url ws://127.0.0.1:8875/ws --grade 7
PYTHONPATH=. python3.11 scripts/check_bot_recovery.py --url http://127.0.0.1:8874
PYTHONPATH=. python3.11 scripts/check_voice_interrupt.py --url ws://127.0.0.1:8875/ws
```

确定性测试检查模型调用次数、进度投影、恢复和幂等；真实模型检查多轮语义；真实 WAV 经 ASR、Bot、Piper、WebSocket 验证语音链路。脚本模拟回执不等于浏览器物理麦克风扬声器验收，不能用替身耗时或少量样本声称设备首声、P50/P95、打断正确率已达标。

启动预热除加载 VAD 权重外，还用零音频预热本地特征提取与推理，再清空缓存，不开启付费 ASR。WebSocket 断线清理独立于请求处理器的取消信号，确保 Bot 保存和 ASR 清理包含异步等待时仍能释放容量；单次关闭等待上限 5 秒。
