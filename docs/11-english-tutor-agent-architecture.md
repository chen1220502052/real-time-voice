# 猫猫分年级实时英语聊天架构

状态：轻量版架构 v0.7，双工侧独立 VAD 收尾预生成、精确匹配与开关对照。更新日期：2026-10-21。

首版只做一件事：学生选择年级，与猫猫进行自然、连续的实时英语聊天。年级影响用词、句子复杂度和语速，不引入课程任务、评分、学习档案或教学闭环。现已实现独立文字入口和语音接入；运行实例与线上部署需分别验证，不能以代码完成代替发布。

## 首版范围

| 做什么 | 暂时不做什么 |
| --- | --- |
| 选择年级，开始和结束通话 | 教材版本与单元绑定、课程和任务进度 |
| 以英语进行连续多轮聊天 | 自动测级、评分、主动逐句纠错 |
| 按年级调整表达和语速 | 学习报告、错题本、长期学生画像 |
| 听不懂时简短中文解释，然后继续英语聊天 | 独立听力题模式、答案判定、教学状态机 |
| 沿用现有语音打断、字幕和猫咪动画 | 多 Agent、知识库检索、向量数据库 |

复用当前 Web 页面和香港服务器，Bot 使用本机 SQLite 保存会话事件和幂等结果，不新增数据库服务或微服务，不把小程序开发并入这次改造。更完整的 [学生英语产品需求](09-student-english-miniprogram-prd.md) 保留为后续方向，不作为本次全部交付范围。

## 职责与服务边界

从左到右看客户端、实时交互和 Bot 内容服务。蓝色区域负责交互效果和可选预生成加速，绿色区域负责回复内容与状态；颜色只是辅助，职责同时写在区域标题中。TextBot v2 是正式内容服务契约；预生成通过可选的只读生成适配器工作，双工不直接读取 BotStore 或拼装模型历史。

这是逻辑组件图，不是多进程部署图：当前双工与 Bot 仍运行在同一个 Python 服务中，SQLite 由 Bot 独占。

~~~mermaid
%%{init: {"theme":"base","flowchart":{"curve":"linear","nodeSpacing":26,"rankSpacing":38},"themeVariables":{"fontFamily":"Arial, PingFang SC, Microsoft YaHei, sans-serif","fontSize":"14px","lineColor":"#64748b","primaryTextColor":"#172b4d"}}}%%
flowchart LR
    subgraph CLIENT["客户端"]
        direction TB
        WEB["语音页面<br/>麦克风 · 播放 · 字幕 · 猫咪动画"]
        TEXT["文字页面 / CLI<br/>文本输入 · 显示回执"]
    end

    subgraph INTERACTION["实时交互领域 · 不管理内容状态"]
        VOICE["DuplexDialogue<br/>VAD / ASR · TTS / PCM 队列<br/>打断门控 · replyId / epoch 栅栏"]
        PREFETCH["VadPrefetch · 可关闭<br/>静音收尾窗口 · 候选取消<br/>文本和上下文完全一致才接管"]
        VOICE -->|"中间文本 / VAD 定稿"| PREFETCH
    end

    subgraph CONTENT["Bot 领域 · 自持会话与内容状态"]
        direction TB
        BOT["BotService / BotSession<br/>请求幂等 · 历史投影 · 打断后恢复"]
        GENERATOR["EnglishChatBot<br/>无副作用的生成器<br/>年级策略 · 资料选择"]
        STORE[("BotStore · SQLite<br/>输入与交互事实 · 生成结果与幂等日志")]
        BOT <-->|"模型上下文 / 生成流"| GENERATOR
        BOT <-->|"读取 / 持久化"| STORE
    end

    LLM["远程 LLM API<br/>流式推理"]

    WEB <-->|"WSS · PCM / 控制 / 播放回执"| VOICE
    VOICE <-->|"TextBot v2<br/>输入与交互事实 / 流式文本"| BOT
    PREFETCH -.->|"可选适配：只读上下文快照"| BOT
    PREFETCH -.->|"提前生成；不写正式状态"| GENERATOR
    TEXT <-->|"TextBot v2<br/>文本输入 / 流式文本 / 显示回执"| BOT
    GENERATOR <-->|"模型请求 / 文本增量"| LLM

    classDef client fill:#f8fafc,stroke:#94a3b8,color:#172b4d
    classDef interaction fill:#eff6ff,stroke:#2563eb,color:#153e75,stroke-width:2px
    classDef bot fill:#ecfdf5,stroke:#059669,color:#064e3b
    classDef external fill:#fff7ed,stroke:#c2410c,color:#7c2d12
    class WEB,TEXT client
    class VOICE,PREFETCH interaction
    class BOT,GENERATOR,STORE bot
    class LLM external
    style CLIENT fill:#ffffff,stroke:#cbd5e1
    style INTERACTION fill:#f8fbff,stroke:#93c5fd
    style CONTENT fill:#f7fdf9,stroke:#86cbb1
~~~

图中双向线表示请求与响应，不表示共享状态；虚线是可选加速路径，不是所有 Bot 必须实现的接口。正式调用仍为 stream 与 observe。VadPrefetch 获取只读生成快照并提前执行无副作用的生成器；VAD 确认后，BotService 正常接受正式请求、重新投影上下文，文本与上下文完全一致才注入候选流，仍逐条落盘后发布。不匹配直接重新请求，不消耗旧 grace 等待。Bot 返回的文本经双工分段、Piper 合成和音频队列交给客户端，不直接从 Bot 下发 PCM。

此图为同进程组件职责图，依据 backend/duplex.py、vad_prefetch.py、bot_service.py 绘制；不表示新建服务或改变线上部署。窄屏可先阅读下方模块表与接口说明，不依赖颜色理解可选与必选路径。

保留现有 NLS、FSMN-VAD、Piper、WebSocket 协议及 Reply 打断机制。年级相关的表达策略属于 Bot；语音侧只按会话配置执行对应语速。年级资料不进入双工策略层，模型历史也不从双工审计对象拼装。

语音入口的 VadPrefetch 独立归双工所有：在安静收尾窗口提前生成，正式请求经 BotService 接受后，只有文本、年级、历史与交付上下文完全一致才接续候选流，否则重新生成。字幕和 TTS 只消费定稿后接管的流；抢话、挂断和断线保留旧回复隔离与候选回收。文字入口不装配加速器。可选适配接口及限额见 TextBot v2 文档。

当前使用远程 LLM；代码中的 OllamaAgent 是兼容接口适配器名称，不代表一定在本机推理。首版继续使用现有模型供应商，不同时做模型迁移。

### 渐进式模块边界（v0.5）

逻辑组件与部署形态不变；本次先隔离故障边界，没有重写双工状态机或增加服务进程。

| 模块 | 单一职责与约束 |
| --- | --- |
| backend/atomic.py | 兼容旧导入和环境配置工厂；原类名继续导出，避免调用方同时迁移 |
| backend/providers/ | common 音频工具与结果类型；asr_local/cloud/nls 适配器；llm、tts、avatar 各自实现供应商细节，保持依赖延迟加载 |
| backend/media_transport.py | 控制与 PCM 共用发送锁；等待锁及元数据发送后重新检查 replyId/epoch，拒绝失效音频；拒绝发送不消耗线序号 |
| backend/reply_lifecycle.py | 每回复播放确认期限；完成、打断、失败均取消计时；超时不推断学生已听到内容 |
| backend/duplex.py | 主编排、ASR 输入、命令/回执、会话关闭；统一持有回复身份、epoch 和共享队列 |
| backend/reply_pipeline.py | 回复启动、Bot 流消费、TTS/PCM、完成与失败收敛；不另建状态快照或调度队列 |
| backend/interruption.py | 每会话打断门控、文本确认与冷却，取消生成和清理旧媒体；不解释 Bot 内容状态 |
| backend/duplex_events.py | 独立事件类型，避免组件循环导入；保留原 DuplexEvent 导入入口 |
| backend/vad_prefetch.py | 双工侧独立加速器：收尾安静门控、文本稳定去抖、候选取消、精确匹配、提前量与剩余等待；可配置关闭 |
| backend/bot_service.py、bot_stream.py、concurrency.py | 请求接受、持久化和生产任务；订阅在创建时即登记，响应头发送前断开也能回收；会话锁替代跨会话全局锁 |
| backend/bot_projection.py | 纯函数从 Bot 账本和播放事实构造模型上下文；双工不持有或回滚内容状态 |

发送栅栏保证旧 PCM 不排在已发出的打断控制之后；已经交给网络或客户端缓冲的内容，仍需客户端按 replyId 丢弃。播放确认默认在全部音频发送后再等 15 秒，可通过 PLAYBACK_ACK_TIMEOUT_SECONDS 配置。超时产生 reply_failed/PLAYBACK_ACK_TIMEOUT，清理回复与挂断意图，后续用户轮次仍可继续；不伪造 playback_completed。

ASR 单轮出错先尝试有界清理，再允许下一轮重新建连；清理超时或发送线程未退出则标记适配器不可复用并关闭当前语音会话，避免旧 SDK 回调污染新轮次。恢复窗口不改变正常路径的 VAD、预生成或打断参数。

输出必须先持久化再通知订阅。保存失败时以 BOT_STORAGE_FAILED 结束等待、拒绝新的生成并使 health 返回 503；不在内存里伪造可重放的终止事件，也不自动重跑已接受的付费请求。恢复存储并重启后由原有 running→BOT_RESTARTED 规则收敛。SQLite 仍限制单 worker，本次没有改变事务结构。

### 首包优化与正确性边界（v0.6）

客户端按 replyId 报告最终停止位置：未开始为 0，重复打断不重复报告，已完成的回复不再报告停止；停止后晚到的完成计时不能改写事实。旧回复迟到的打断只停止它自己的音源及排队帧，不误停新回复。测试客户端也遵守此规则，不能在测试结束时把被打断的所有草稿一律确认完成。Bot 保留原有最多 150ms 的停止回执宽限；已有最终回执时立即继续，不将其误报为数据库读取耗时。

v0.6 的前缀覆盖率、can_adopt 和 grace 接管已退出默认语音路径。v0.7 使用下述独立 VadPrefetch；旧 speculative_bot.py 与兼容测试保留为历史兼容实现，不再由 create_agent 装配。

TTS 在语音适配层逐块转交 Piper 原生 PCM，经容量 2 的线程桥背压传递，不再汇总全部原生块后才返回。每模型单线程执行器仍隔离不可立即中断的 ONNX 调用；取消仅阻止后续块和旧音频发布，不声称正在计算的原生推理已停止。首个已确认文本段可在较短自然逗号边界启动 TTS，未改 Bot 完整短句输出、100ms PCM 帧或打断门控。Piper 若一段只返回一个原生块，该段仍需等待整块，不能承诺所有句子首包都明显加速。

bot.latency 增加 deliveryWaitMs、snapshotMs、projectionMs 阶段耗时；旧 historyReadyMs/providerFirstTokenMs 仍为累计里程碑，供应商等待用 providerFirstTokenMs-providerRequestMs 计算。播放时钟验收和音频实收时延须分开记录，不把收包时刻当作物理扬声器首音。

对于超过 40 字符的首个可朗读段，非数字表达可在 24–36 字符附近的词边界切分，避开常见悬空介词/冠词；前后段拼接必须逐字等于原文本。英文 Bot 仍先输出完整句，缩短的是 TTS 子段而非模型提交单元，不能把模型异常断流的半句话伪装成成功。含数字的表达不走这一缩短路径，以保留算式朗读连贯性。

云 ASR 可选 peek_partial 只提供带锁的最新文本快照，不依赖 Bot。双工在单帧 feed 的 SDK 收尾等待期间，每 50ms 读取更新并交给 VadPrefetch，正常 feed 完成即继续，不额外等满轮询周期。offer 是非阻塞操作，稳定去抖及只读投影由加速器管理；定稿停止启动新候选，打断和关闭取消候选。VAD 起止与打断判断仍只消费原有 feed 结果，参数不变。

### VAD 收尾重叠（v0.7）

在安静音频帧出现后，等待安静窗口成立 120ms 再提前执行生成；后续 ASR 文本更新不重新等待 120ms。这个安静门控只控制额外计算，不决定用户是否说完，也不改变 FSMN-VAD/NLS/打断阈值。默认每轮最多 3 次，保留一个最新候选；文本更新废弃旧候选，定稿不等待稳定计时器或未启动的候选。不做礼貌词等价、前缀覆盖率或语义复用，连大小写和标点变化也不复用；匹配同时包含会话、轮次、年级、全部已投影消息与交付上下文。

配置为 DUPLEX_PREFETCH_ENABLED=0/1；未配置时兼容读取旧 LLM_SPECULATIVE，两者都未设置时默认关闭。DUPLEX_PREFETCH_STABLE_MS 默认 120，DUPLEX_PREFETCH_MAX_PER_TURN 默认 3，DUPLEX_PREFETCH_QUIET_RMS 默认 0.008。重启或新建模块实例读取配置，不通过学生请求修改开关。

Bot 本身仍可有持久状态。只有明确声明 side_effect_free 的内部生成器能通过 BotSession 的可选只读适配器提前执行；草稿不写 requests 表，不推进正式轮次，不发布文本或动作。普通有副作用/未知 Bot 不提前调用，正式流式接入照常。正式接受、请求指纹、eventSeq 和重放均仍由 BotService 管理，外部 HTTP/WS 协议不新增必选字段。

duplex.prefetch 日志与正式 bot.latency 记录 effectiveLeadMs（被采用候选启动到 VAD 定稿的有效提前量；未采用为 0）、remainingWaitMs（定稿到首段可用文本的剩余等待；提前就绪为 0）、adoptionWaitMs（接管执行到首次消费文本）、adoptedReady、prefetches、exactMatch 和 missReason。提前量不等于已证实节省量；实际收益用开关配对实验衡量，另计 ASR 收尾、TTS 和网络。

### 命令、任务进度与结束会话

Bot 输入除用户消息外，现已支持接通、time_out 和 hang_up 命令。开场、空闲询问及明确结束会话时的告别内容由 Bot 管理；双工只检测交互空闲并传命令，不再匹配“再见”文本来直接关闭会话。空闲检测不在用户讲话、生成或播放中抢占回复。无论连续空闲多少次，time_out 只能温和询问，不能告别或产生挂断动作；没有回应不代表用户同意结束通话。

生成流扩展为 text_delta / progress / action / done / error。长任务可以在原流主动推送完整 progress 话术，也可通过 BotService 的受 epoch 栅栏保护的后台推送入口发布；BotService 串行持久化再交付，双工沿用当前回复的 TTS、队列和打断路径。Bot 不下发音频、不操作媒体队列，也不能用旧任务的新推送绕过打断。当前只支持活动请求内的进度推送，不做离线消息或无请求外呼。

Bot 返回告别话术及结构化 action(hang_up, after_playback)；双工等待当前回复的客户端播放完成确认后执行。打断、停止或换回复会废弃待执行动作；硬挂断和断线仍保留立即关闭。命令和输出事件沿用请求幂等及回放，动作消费者按事件序号去重。详细 JSON、限额及能力声明见 [标准接口的命令扩展](12-text-bot-interface.md#命令进度推送与动作v2-能力扩展)。

### 打断后的状态恢复

下面只展开一次打断，区分“立即停播”和“下一轮内容恢复”。前者由双工与客户端完成，不等待 Bot 的语义处理或数据库写入。

~~~mermaid
%%{init: {"theme":"neutral","sequence":{"mirrorActors":false,"wrap":true,"diagramMarginX":16,"actorMargin":32,"messageMargin":24},"themeVariables":{"fontFamily":"Arial, PingFang SC, Microsoft YaHei, sans-serif","fontSize":"14px"}}}%%
sequenceDiagram
    autonumber
    participant C as 客户端
    participant D as 实时双工
    participant B as Bot 服务
    participant S as BotStore

    Note over C,D: 回复 A 正在生成或播放，用户开始插话
    C->>D: 持续上送用户音频
    D->>D: 门控确认打断，废弃 A 的 epoch 与媒体队列
    D->>C: playback_interrupt(A)
    C->>C: 采样音频时钟，立即停止旧音源
    C-->>D: playback_stopped(A, playedMs)
    D-->>B: observe：异步转交取消、进度与 TTS 对齐事实
    B->>S: 去重并保存交互事实

    C->>D: 继续上送新话语
    D->>B: stream：新的 requestId、定稿输入与近期事实
    B->>S: 查请求指纹及会话记录
    S-->>B: 既有请求、输出事件与交付事实
    alt 同一请求已接受
        B-->>D: 接续原任务或重放原事件，不再次调用模型
    else 新请求
        B->>B: 投影已交付历史，隔离未播放草稿
        Note right of B: 停止回执尚未到达时，<br/>仅内容投影最多等待默认 150ms；<br/>不阻塞此前的停播
        B->>B: 接管匹配预生成，或发起新生成
        B->>S: 按 eventSeq 保存输出后再交付
        B-->>D: text_delta / done / error
    end
    D-->>C: 新回复字幕与 PCM，继续使用新 epoch 栅栏
~~~

时序图中的 observe 汇总表达多类事实，它们实际可分批、乱序到达，不要求等待客户端停止回执后才上报生成取消。playback_stopped 也可能比下一轮请求晚到；双工在请求中附带近期原始事实，Bot 负责去重和有界等待。生成但未交付的内容不算“已经说过”，只作为独立草稿供明确的续说请求使用；部分播放采用已确认完整 TTS 段的保守对齐。

## 年级配置

结合已有六至九年级资料，先提供六、七、八、九年级四档，默认七年级。年级是表达风格配置，不是正式英语能力测评，也不意味着严格覆盖该年级教材词表。

| 年级 | 用词与句式 | 通常回复长度 | 初始语速参数 |
| --- | --- | --- | --- |
| 六年级 | 高频生活词、简单短句 | 1 至 2 句，约 8 至 15 词 | 0.85 |
| 七年级 | 简单日常表达，可问喜好和日常活动 | 1 至 2 句，约 10 至 20 词 | 0.90 |
| 八年级 | 适度加入过去经历、计划和原因 | 1 至 2 句，约 15 至 30 词 | 0.95 |
| 九年级 | 可讨论观点和简单假设，仍避免长篇输出 | 1 至 3 句，约 20 至 40 词 | 1.00 |

这些是初始产品参数，不是课程标准或已验证的最佳语速。Piper 的速度值是相对参数，试听后调整，不换算成承诺的每分钟词数。学生要求“简单一点”或“慢一点”时，模型会倾向使用更短表达；当前 TTS 速度由年级固定，暂不声称语音指令能动态调速，也不产生测级结果。

配置集中放在一个模块，例如 backend/english_chat.py：每档只包含年级、词汇与句式说明、目标回复长度、TTS 速度和开场白。不要为每个年级写一套 Agent 类。

## 聊天行为

猫猫是一位友好的 AI 英语聊天伙伴，不是考试老师。它应接住学生的话题，记住本次会话中学生主动提到的信息，再自然追问。

- 默认用英语，一次只问一个主要问题，不连续列出问题或长篇讲解。
- 围绕兴趣、学校生活、动物、运动、食物和周末活动等日常话题聊天，不必按预设课程顺序走。
- 不主动给分或逐句纠错。学生主动问“这句话怎么说”“我说得对吗”时，给一个简短自然的说法，再接回聊天。
- 学生明确说没听懂、要求中文解释，或暂时说不出英语时，给一句简短中文或英语句式帮助，不责备、不强迫完成任务。
- 没听清时先澄清，不把错误转写当成学生的语法错误。
- 不输出 Markdown、列表、JSON 或表情符号供 TTS 朗读，不索取姓名、学校、电话等无关信息。

年级配置由服务端生成，不允许客户端传入任意 system prompt。学生语音和资料示例是聊天内容，不得覆盖固定的角色与安全规则。

## Agent 接口与会话状态

采用 [TextBot v2 标准接口](12-text-bot-interface.md)：stream(ConversationRequest) 返回带 eventSeq 的 BotEvent 流，observe 接收交互事实。文字、命令行与语音使用相同契约。内部 BotRequest v1 和旧 stream_reply 仅作为无状态模型适配层，不再代表双工与 Bot 的服务边界。

实现类 EnglishChatBot 组合现有 LLM 适配器，按“固定聊天规则 + 年级风格 + 当前会话历史”生成回复。每次生成只调用一个聊天模型，不串联规划、检索、评分或总结模型。语音预生成会产生有上限的额外请求；缓存命中不再发正式模型请求，未命中则回退一次正常生成。

接听时拦截现有 pick_up 信号，输出简短英文开场白，例如 “Hi, I'm Kitty! What do you like to do after school?”。不能只改 system prompt 而保留适配器中固定的中文通用开场。

语音会话保留 grade、tts_speed、sessionId、replyId 和 generationEpoch。请求携带稳定 requestId、turnId、当前输入和播放事实，不再携带双工裁剪的 messages 或 Bot 状态。年级选项由 Bot 校验、按会话隔离，不修改全局环境变量或共享模型提示词。

BotStore 保存用户输入、生成结果、请求指纹和交互事实；Bot 从记录投影最近最多 32 条、约 6000 字符的模型历史，不额外调用摘要模型。未播放回复不作为已说过的话；部分播放只保留客户端确认的完整 TTS 段。未交付草稿通过独立 deliveryContext 帮助模型处理“继续说”，不混入 assistant 历史。用户明确表达的信息不随助手打断回退。

Bot 对相同请求复用进行中任务或重放保存结果，冲突 ID 拒绝；取消和重启都形成可重放终态，不自动重新调用模型。重复、乱序、晚到回执由 Bot 去重和投影，双工不解释内容状态或执行回滚。旧 epoch 的文本与音频仍由双工栅栏阻止下发。

持久化仅服务于当前会话恢复和幂等，不建立跨 session 的学生画像。默认保留活跃会话 7 天，过期按会话清理；不保存原始录音。数据库工作在后台线程，当前只允许单进程持有专用 SQLite 文件。详细保留期、错误码与一致性限制见接口文档。

## 前后端最小改动

前端只新增一个年级选择框，选择年级后点击现有“拨通”。通话期间禁用年级切换，挂断后可重新选择，新会话清空旧历史。

建会请求增加可选的 grade，其他音频参数和二进制帧保持不变：

~~~json
{
  "type": "create_session",
  "version": 1,
  "requestId": "req-1",
  "grade": 7
}
~~~

服务端只接受整数 6、7、8、9。省略时使用默认七年级，保持旧页面可用；其他值返回明确错误，不静默接受。建会结果返回实际采用的年级，供页面展示。

| 位置 | 修改范围 |
| --- | --- |
| 新增 backend/english_chat.py | 年级配置、聊天规则、英文开场和 Agent 包装 |
| backend/atomic.py | Agent 工厂已增加 english_chat 选项，复用当前模型调用逻辑 |
| backend/app.py | 校验并保存建会年级，向本次会话注入 Agent 与语速 |
| backend/duplex.py | 管理实时交互、输入与播放事实、TTS 对齐和回复栅栏，不构造模型历史 |
| backend/bot_contract.py 与 backend/bot_service.py | 标准 v2 契约、上下文投影、状态恢复、请求幂等和流式重放 |
| backend/bot_store.py | Bot 独占的持久化事件和请求记录，不保存音频 |
| web/index.html 与 web/app.js | 年级选择、建会传参、通话期间锁定选项 |
| 测试 | 年级配置隔离、英文开场、多轮上下文、真实语音链路和打断回归 |

模型增量到达后，按完整短句整理并立即输出，不等待后续全文。年级词数作为软目标，在句末收尾，不在词数阈值处截断；不提交悬空的 so、because 等句尾。模型返回 length 或异常断流时不伪装成正常完成。继续保持有界队列和现有打断优先级。空文本或结构化输出使用简短澄清句；提供方失败返回可恢复错误，不进行多次自动改写，也不把错误详情朗读给学生。文字页面是 /text.html，接口是 POST /api/text-bot/replies；均已实现。

## 已收集英语资料怎么用

已有资料现通过 scripts/build_chat_materials.py 整理进 backend/data/english/grade_6.json 至 grade_9.json，按年级包含 6、18、16、8 个单元话题，以及 238、884、885、551 条旧版补充词汇。每个包记录源文件路径、SHA256 与资料类型，python3.11 scripts/build_chat_materials.py --check 可验证来源和产物一致。

运行时只读取当前年级的只读素材包，不扫描整个原始资料目录。backend/chat_materials.py 根据最近学生话语的词汇、中文释义及少量话题别名选择两个相关单元，并附最多六条词义参考；学生明确换话题时，最新话语优先。参考中的人名、地点和经历不作为学生或猫猫的身份信息。训练任务、评分说明、整段教材文本和音频不会进入模型。这里只是有界的本地素材选择，不新增向量数据库、联网检索或模型调用。

自然对话规则要求先回答问题、记住已给出的信息、不要每轮机械追问，也不要把身份问题强行转去聊爱好。中文求助给简短解释；没有求助时不强制讲解词汇。年级限制是表达风格和软长度目标，不声称输出逐词只使用教材词表。

旧版词表和新版主题材料不混称为学校正在使用的教材。录音和教材原文不因已下载就自动上线播放。首版聊天不依赖外部教材素材授权或完整整理进度才能跑通。

英语 Bot 在官方 DeepSeek 端点使用 thinking.type=disabled，避免简单日常聊天先消耗内部推理预算；其他兼容供应商不发送此专有字段。配置依据 [DeepSeek 思考模式说明](https://api-docs.deepseek.com/guides/thinking_mode/)。这与 ASR 中间结果驱动的后台预生成完全独立，不关闭边听边想。

## 验收与上线

1. 四个年级都能选择并成功建会；默认值与非法参数行为明确，不同会话的年级设置互不污染。
2. 使用同一组日常问题，人工检查四档表达是否有合理差异；以输出结果评估，不仅检查提示词是否存在。
3. 连续聊 10 轮能接续当前话题，不每轮重新自我介绍，不强制进入提问、答题、评分流程。
4. “没听懂”“慢一点”“不知道怎么说”和主动求助能得到简短帮助；不因此整段变成中文聊天。
5. 真实 ASR、远程 LLM 和本地 TTS 跑通英文聊天；英语单词不被固定字符切分破坏，暂停与打断后不复播旧音频。
6. 记录真实“用户说完到设备首声”的延迟，区分服务器与客户端数据；正常网络、预热下暂以 P50 ≤ 2 秒、P95 ≤ 4 秒为目标，不用 Mock 验收。

先完成年级配置和文本回复测试，再接页面与真实语音，最后部署香港试用环境。沿用现有 HTTPS 和访问口令，不新增账号系统；共享口令仅用于技术试用，不代表完成学生身份与必要授权。原始学生录音不默认落盘，不新增长期档案。

当前单会话容量限制保留，不因新增年级或 Bot 持久化就承诺多人并发。现有 4 核 8GB CPU 服务器作为验证环境，不新增 GPU、外部数据库服务或内容加工任务。
