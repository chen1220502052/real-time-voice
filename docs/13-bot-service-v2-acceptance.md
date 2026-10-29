# Bot 与双工解耦验收记录

日期：2026-10-21。远端版本：rtv-20261021-bot-v2-r1。

Bot 服务已接管会话事实、模型历史投影、未播放草稿的续接、请求幂等和持久化。双工只管理交互与播放事实，不拼接被打断的助手历史，不保存 Bot 业务状态。线上文字与语音入口均使用 TextBot v2，WebSocket 和 PCM 协议仍为 v1。

## 验收结果

| 检查 | 结果与范围 |
| --- | --- |
| 确定性回归 | 160 passed；3 个真实模型标记测试未纳入该次 pytest，真实调用单独执行 |
| 前端播放时钟 | 5 组场景通过：未来帧不算播放、间隙不累计、停止位置冻结、AudioContext 挂起不算完成、旧完成计时器不复活旧回复 |
| 本地真实模型多轮 | 六至九年级各 10 轮，共 40 轮通过；使用与线上相同的 DeepSeek 模型配置 |
| 线上真实模型多轮 | 七年级 10 轮通过，涵盖偏好记忆、换话题、词义和中文求助 |
| 本地和线上恢复接口 | 未播放不回退用户事实、未播放草稿续说、部分交付后续说、晚到 completion 不把打断回复提升为完整交付，均通过 |
| 请求幂等 | 单测验证重复请求只调用一次模型、进行中共享流、取消后重放终态、重启后重放；线上验证完整事件重放、游标重放、冲突 409、重复回执 |
| 浏览器文字页面 | 从页面输入 Curry 偏好，下一轮正确回忆，Bot 保存上下文；页面不回传 messages |
| 本地和线上真实语音 | 两段 WAV 经 NLS、Bot、Piper、WebSocket 完整返回音频，无链路错误；正式两轮均命中预生成 |
| 本地和线上打断 | 未播放时打断与声明播放 550ms 后打断均通过；打断指令之后旧回复音频包计数为 0 |
| 发布一致性 | 13 个应用文件的远端 SHA256 与本地一致；health 返回 v2、stateOwner=bot、preparation=true |

多轮测试包含：问候、年龄、Curry 偏好及回忆、guitar 释义、中文澄清、转到天气、雨天在家画猫及跨轮回忆。断言通过不代表所有开放式内容都经过人工质量评估。

## 恢复示例

线上测试中，生成但未播放的猫话题草稿为“Cats are quiet and soft. They nap in sunny spots.”。用户请求继续后，Bot 从头接回该草稿；它没有被作为已说过的助手消息提交。

部分交付测试确认前缀“Dogs love to run and play.”，剩余草稿为“They wag their tails when happy.”。请求继续后，回复从剩余内容开始，没有重复已确认前缀。用户自己说过的 Curry 偏好在助手被打断后仍能正确回忆。

## 双工回归边界

原打断确认窗、文本确认、最小回复年龄、冷却、VAD 和预生成调参均未改动。模型缓存身份增加 deliveryContext，避免交付上下文改变后误用旧草稿。replyId 和 generationEpoch 隔离、独立控制与媒体队列、100ms PCM 帧均保留。

本地首轮语音回归暴露冷推理积压和断线清理容量占用，已补上零音频 VAD 推理预热及不随请求处理器取消而中断的清理任务。预热不启动付费 ASR；断线清理补了专门的容量释放回归。修复后的本地及线上语音检查均通过。

线上两个样本的录音发送结束到首个收到音频包分别为 1346ms、1452ms。这不是物理设备首声，不是精确最后有声采样到首声，也不是 P50/P95。Bot 侧等待最近停止回执的默认上限为 150ms，只用于下一轮内容投影，不阻塞双工停播。

## 播放与状态限制

语音脚本调用了真实 ASR、LLM 和 TTS，但播放回执由客户端模拟。打断脚本的 0ms、550ms 是声明的测试位置，不是物理扬声器测量。浏览器播放进度逻辑另有 AudioContext 时钟测试；没有据此宣称真实麦克风、回声场景、误打断率或抢话率已全面验收。

部分播放只确认完整 TTS 段，没有音素级对齐，可能保守遗漏实际播放的半段。超出停止回执等待窗的迟到事实会修正后续请求，不回改已发出的新回复。模型对自然续接的表现仍需学生试用评估。

## 发布与保存

- 入口：https://aimaoren.com/ 与 https://aimaoren.com/text.html，原试用口令不变。
- 旧版备份：/var/backups/real-time-voice/rtv-20261021-bot-v2-r1。
- 发布包 SHA256：652ea7ec808fb343a16a20f2fc4a2ecf65a19751d0d49cc36fc7c15903675655。
- Bot 数据库：/var/lib/real-time-voice/bot/state.sqlite3，目录 0700、文件 0600、单进程独占。
- 环境配置仅新增 BOT_STATE_DB 并更新 GIT_COMMIT；原模型凭证、ASR/TTS、打断及预生成参数保留。
- 默认保存会话文字与交互事实 7 天，不保存学生录音，不建立跨会话学习画像。

本地报告位于 .local/bot-v2-multiturn-local.json、bot-v2-multiturn-cloud.json、bot-v2-recovery-local.json、bot-v2-recovery-cloud.json、bot-v2-voice-local.json、bot-v2-voice-cloud.json、bot-v2-interrupt-local.json、bot-v2-interrupt-cloud.json；浏览器截图为 .local/bot-v2-browser.png。报告与试用凭证均不提交 Git。

标准契约见 [文本 Bot 接口](12-text-bot-interface.md)，系统分层见 [分年级聊天架构](11-english-tutor-agent-architecture.md)。

## 文本延迟优化增量（2026-10-21，本地验证，尚未发布）

应用与文本 CLI 各自拥有可关闭的 LLM HTTP 连接池，复用 DNS、TLS 与空闲连接；请求认证头、计时和取消相互隔离，不共享 Cookie，不增加重试。取消或提前结束流仍立即释放当前响应，不为保活而等待剩余生成。连接池在 Bot 任务清理后关闭。独立使用模型适配器时仍按调用关闭连接。

提示词删减重复解释和风格示例，保留分年级资料、多轮记忆、中文澄清、未播放草稿的续接约束和未成年人边界，要求首句简短直接但完整。thinking 仍关闭，模型、max_tokens、句子完整性检查和事件协议均未改变。没有改双工、预生成匹配阈值、等待窗或播放状态投影；SQLite 仍先持久化再发布事件。

新增 bot.latency 内容无关日志：锁获取、幂等检查、上下文就绪、请求落盘、模型请求、建连、响应头、模型原始首字、完整首句就绪/落盘和终态；各里程碑均从 BotService.start 开始计时。eventPersistenceMs 是累计事件落盘耗时，connectionReused 标识连接复用。缓存命中时没有本次模型首字时间，不以 0 代替。日志不记录提示词、回复或凭证；客户端回归脚本另记录 requestId、首句和完整流结束时间，完整流时间不包含后续交付回执。

同机、同真实 DeepSeek API，七年级各两组十轮：优化前首句均值 965/934 ms，优化后 750/747 ms；合并约 950 → 749 ms（约 21%）。第二组完整流结束均值 999 → 803 ms。第一组旧版完整流测量包含回执，因此不用于完整流对比。小样本、顺序测试且回复不完全相同，不能据此承诺固定收益或线上 SLA。这是文本端点时延，不包含 ASR/TTS/物理播放。

六至九年级真实多轮聊天通过，真实 LLM 配合结构化回执的幂等/恢复检查通过。增加连接复用、认证与 Cookie 隔离、取消不影响兄弟请求、跨应用/事件循环关闭测试；自动化套件 165 passed、3 deselected，包含已有预生成、半句和双工回归；前端播放时钟五个场景通过。

本次本地真实语音回归未通过：NLS 连接阶段出现上行 QUEUE_OVERFLOW，随后返回 40010005；尚未完成原因定位，不能宣称真实语音打断端到端通过。没有据此改动 ASR/VAD 或放宽队列、打断参数，也没有部署该优化到远端。前述章节的线上验收是此前版本的结果，不代表本次优化版。

本地证据：.local/latency-before.json、latency-before-repeat.json、latency-after.json、latency-after-repeat.json、latency-grades.json、latency-recovery.json；语音失败诊断日志为 .local/latency-voice-nls.log。

## NLS 故障修复与 Bot 命令增量（2026-10-21）

本节是上述失败后的后续修复，不能与此前已发布 v2-r1 的线上验收混为一谈。

首次启动 NLS 的 token 获取与建连此前同步占用 100ms VAD 消费路径，导致输入积压。现将 Start 与音频放在同一发送 FIFO，网络初始化不再阻塞本地 VAD；服务预热提前获取可续期 token，但不开付费识别流。结束时仍先确认已排队音频发送，再停止识别，flush 超时显式失败，不静默截断。上传队列满时不再丢弃可能包含 Start 的旧队列项，而是报告上传失败。

Stop 后识别对象未及时清空，异常或断线清理可能再次 Stop。现先在互斥锁内摘除对象，只停止一次；已收到 TranscriptionCompleted 的对象不再 Stop。reset 也会清理尚在启动中的识别流，线程等待移到事件循环外。该状态错误与阿里云对 [40010005 的说明](https://help.aliyun.com/zh/isi/user-guide/api-reference)一致：须检查指令顺序，避免停止中的任务再收 Stop。

压力复测另发现 TTS 合成期间 VAD 连续多帧超过 100ms。最终限制 FunASR VAD ncpu 默认 1，Piper ONNX intra-op 默认 2、inter-op 1，关闭空闲线程自旋，减少原生推理池争抢。支持 VAD_CPU_THREADS/TTS_CPU_THREADS 配置（1–4）；不扩输入队列，不改 100ms PCM、VAD 起止策略、打断确认窗、最小年龄、冷却或预生成匹配参数。单帧超过 100ms 仍可能发生，不能保证任意 CPU/网络负载下永不积压。

已实现接通、time_out、hang_up 命令，流式 progress、受 requestId/epoch 保护的 Bot 后台进度推送，以及 after_playback 挂断动作。开场、提醒次数、告别策略归 Bot；双工只检测空闲、播放、打断并执行结构化动作，不再根据“再见”字符串自行挂断。动作被打断或停止后失效，旧完成回执不能挂断新回复。完整设计及限额见 [标准接口](12-text-bot-interface.md#命令进度推送与动作v2-能力扩展)。

最终限额版已有本地实测：六至九年级共 40 轮真实 DeepSeek 文本对话；真实 LLM 配合交付事实的重复请求、游标重放、冲突、未播放/部分播放续接；真实 WAV→NLS→Bot→Piper→WebSocket 两轮；两组真实语音打断（每组未播放与声明播放 550ms 两次），旧音频泄漏均为 0；真实 WebSocket/Piper 的接通、time_out、命令重试与播完挂断流程通过。最终版本日志未再出现 QUEUE_OVERFLOW 或 40010005。长任务后台推送另通过受控异步任务测试：任务尚未结束即已产生 TTS 音频，打断后旧任务不能再次播放；这部分任务耗时是测试夹具，不冒充真实业务工具调用。

命令与语音测试的播放完成回执由客户端模拟，仍不是物理麦克风/扬声器、回声、多人环境或大样本误打断率验收。先前无 CPU 限额时的一次打断复测曾失败，后续通过不能抹去该压力边界；相关旧日志保留供排查。

证据位于 .local/bot-commands-multiturn-final.json、bot-commands-recovery-final.json、bot-commands-voice-final-r2.json、bot-commands-interrupt-final-r2-a.json、bot-commands-interrupt-final-r2-b.json、bot-commands-ws-final-r2.json；最终语音日志为 .local/bot-commands-release-r2.log。

五轮内容检查随后发现原覆盖率接管可采用缺少末尾问题对象/数字的草稿，曾把猫的话题回答成反问对象，把 23+45 回答成 23+40=63。已在 Bot 内容层增加可选 can_adopt 策略，英语实现只接管相同规范化输入或有限安全尾缀；覆盖率、grace、并发、节流、打断门控均保留。该限制会增加部分请求的缓存 miss 和重新生成等待，不能用错误缓存的低延迟冒充优化。同时明确宿主可朗读回复，避免机器人声称自己只能打字。

加内容校验后的 r3 实测：五轮真实 WAV/ASR/LLM/TTS 流程通过，猫话题给出猫的事实，23+45 回答 68，慢速请求使用短句；问候和帮助请求仍命中预生成，补全关键内容的请求走 fresh。新增语义断言可阻止上述错误作为“语音通过”发布。六至九年级 40 轮真实文本聊天、状态恢复和请求幂等也已重新通过。自动化套件 179 passed、3 deselected，前端播放时钟五场景通过。

r3 内容与恢复证据：.local/bot-commands-voice-five-turn-r3.json、bot-commands-multiturn-r3.json、bot-commands-recovery-r3.json；语音日志为 .local/bot-commands-release-r3.log。受控后台任务与取消测试在 tests/test_bot_commands.py，CPU 限额及慢启动/单次停止回归在 tests/test_atomic.py。

r3 最终打断与命令复测亦通过，报告为 .local/bot-commands-interrupt-r3.json、bot-commands-ws-r3.json；最后检查 r3 全量日志没有 QUEUE_OVERFLOW、40010005、asr.failed 或 asr.reset_failed。待发布包 .local/rtv-20261021-bot-commands-r1.tar.gz 已验证 33 个文件与工作区一致，SHA256 为 1d9759858cf903a483fc8c12901969952b1bb960ed4af869723f15f9921aeb5b；该包仅含应用源码、资料和 Web 文件，不含本地凭证、学生录音、运行数据库或测试报告。

发布状态：已于 2026-10-21 网络恢复后部署到香港 ECS，版本 rtv-20261021-bot-commands-r1，应用启动时间 05:12:58 UTC。此前网络阻塞期间没有重启实例、修改安全组或变更域名；本次仅按发布流程重启应用服务。

### 命令版线上验收

发布前两次检查无活跃通话，核对原线上差异并保留既有预生成 grace/遥测改动；应用、运行配置和数据库备份位于 /var/backups/real-time-voice/rtv-20261021-bot-commands-r1。发布后 33 个应用文件与已测试发布包 SHA256 一致，运行配置仅 GIT_COMMIT 变化，供应商凭证、模型选型及原有双工参数未变；数据库仍为 catvoice 持有、目录 0700/文件 0600。

通过公网 HTTPS/WSS 与原试用认证完成：五轮真实 WAV→NLS→Bot→Piper 语音，猫话题和 23+45=68 语义断言通过；两次语音打断（声明播放 0/550ms），旧音频泄漏 0；接通、time_out、重复命令、告别音频、旧回执隔离及当前播放完成后挂断；七年级 10 轮文本聊天；请求完整重放、游标重放、ID 冲突、重复回执、未播放草稿恢复、部分播放后续接。长任务异步推送仍以此前受控任务测试为依据，不冒称已有真实业务工具的线上验收。

本次发布后的应用日志未发现 QUEUE_OVERFLOW、40010005、asr.failed、asr.reset_failed 或 reply.failed。原认证保留：匿名页面访问返回 401，认证后的首页、文字页、脚本与 health 均返回 200；测试结束健康检查正常、资源占用归零。播放完成回执仍由测试客户端模拟，不代表物理麦克风/扬声器或真实回声环境已验收。

线上证据位于 .local/bot-commands-voice-cloud.json、bot-commands-interrupt-cloud.json、bot-commands-ws-cloud.json、bot-commands-multiturn-cloud.json、bot-commands-recovery-cloud.json；文件核对结果为 .local/bot-commands-remote-after.json。入口仍为 https://aimaoren.com/ 和 https://aimaoren.com/text.html，原试用账号口令不变。

## 后端故障边界修复与渐进重构（2026-10-21）

先用 tests/test_failure_boundaries.py 重现六项问题，修复前六项均失败：发送锁等待后仍发送失效 PCM、ASR 单轮失败无法恢复、日志保存失败使订阅悬挂、播放完成回执缺失卡住回复、跨会话全局锁串行等待、订阅首次读取前断开遗留生产任务。修复后上述六项及清理超时、响应头前断开、键锁取消、计时取消、存储失败健康检查，共 11 项故障回归通过。

在不改外部协议的前提下，将 atomic.py 从约 1500 行拆为 57 行兼容工厂和 providers 子模块，另拆出媒体发送栅栏、播放确认期限、订阅、会话锁和纯历史投影模块。没有调整 PCM 帧、VAD/打断门控、预生成覆盖率/grace/并发或资料策略；没有数据库结构迁移。新增播放确认故障期限默认 15 秒，仅在全部 PCM 已发送而客户端仍不确认时生效，不用超时推断已听内容或执行挂断动作。

完整自动化 190 passed、3 deselected，前端播放时钟 5 场景与 JS 语法检查通过。真实 DeepSeek 六至九年级 40 轮文本聊天，以及完整/游标重放、ID 冲突、重复事实、未播放与部分播放后续接通过，报告为 .local/refactor-multiturn-local.json、refactor-recovery-local.json。

本地首次真实语音回归失败，出现 QUEUE_OVERFLOW；该结果不计为通过。测试机同时观察到负载约 157、约 20GB 交换空间使用，以及多帧 VAD 延迟超过 100ms，原始日志保留于 .local/refactor-voice.log。没有扩大队列、放宽打断参数或终止用户的其他进程。后续使用香港 ECS 的回环地址 8890 隔离验证实例，独立测试数据库，不更换公网服务，也不开放端口；真实语音及打断通过之前不激活候选包。

候选包 .local/rtv-20261021-backend-r1.tar.gz，46 个应用文件，SHA256=a6072927f7ce8fbb133d952a2d0db3bf34b973c138f8e180050e1b591debd355。发布前已确认远端 33 个原应用文件与上次发布留存哈希完全一致；原认证、域名、供应商和双工参数均保留。隔离验证与线上复测结果随后记录于本节。

### 香港隔离实例验收

同一候选包在 ECS 回环地址运行，使用生产环境已有供应商配置、独立测试数据库及原生线程限额。五轮真实 WAV→NLS→Bot→Piper→WebSocket 全部通过，含猫话题、23+45=68 和慢速请求语义检查；两次真实语音打断分别声明播放 0/550ms，旧音频泄漏 0；接通、time_out、命令重试、告别、旧 ACK 不挂断和当前播放确认后挂断通过；真实 LLM 的幂等、回执重试和打断后恢复再次通过。

隔离实例日志中 QUEUE_OVERFLOW、40010005、asr.failed、reply.failed 均为 0；正式服务测试期间未切换且资源空闲。候选文件 46 项与包内逐一核对一致，测试结束已停止隔离实例，避免与正式发布争抢推理资源。报告为 .local/refactor-voice-staging.json、refactor-interrupt-staging.json、refactor-commands-staging.json、refactor-recovery-staging.json。

本地负载导致的失败记录继续保留，隔离验证通过不代表高负载下能保证零溢出；没有因此取消队列保护或放宽双工参数。播放回执由客户端模拟，本次仍不包含物理麦克风/扬声器、回声和多人环境的统计验收。

### 发布与公网复测

已发布 rtv-20261021-backend-r1，启动时间 2026-10-21 05:49:33.883486 UTC。激活前两次确认没有活跃试用会话；应用、配置和 SQLite 备份位于 /var/backups/real-time-voice/rtv-20261021-backend-r1。发布后 46 项文件与候选包逐项一致，核对报告为 .local/refactor-remote-after.json。运行配置与备份比较只有 GIT_COMMIT 变化；没有改供应商、认证、模型、域名、安全组或原双工参数。数据库目录 0700、文件 0600，仍由 catvoice 持有。

经公网 HTTPS/WSS 使用原试用认证再次通过：五轮真实语音与语义断言、两次真实语音打断（声明播放 0/550ms、旧音频泄漏 0）、命令重试与播完才挂断、完整/游标重放、ID 冲突、重复回执、未播放/部分播放后恢复，以及七年级 10 轮真实文本聊天。匿名首页 401，认证后的首页、文字页、脚本和 health 均为 200。发布后日志中的 QUEUE_OVERFLOW、40010005、asr.failed、asr.reset_failed、reply.failed 均为 0。

公网报告：.local/refactor-voice-cloud.json、refactor-interrupt-cloud.json、refactor-commands-cloud.json、refactor-recovery-cloud.json、refactor-multiturn-cloud.json。最终自动化报告 .local/refactor-regression-final.log 为 190 passed、3 deselected；3 项独立 real_model/real_cloud pytest 测试未运行，真实服务验证以上述独立脚本为证，不把 deselected 算作通过。前端播放时钟 5 场景、JS 语法检查及 git diff --check 通过。

全部复测结束后 health.ok=true、resources.inUse=0，隔离验证服务已停止，本地自建的 8890/8891 验证实例也已停止；既有用户预览实例未动。可继续使用 https://aimaoren.com/ 试用，原账号口令不变。

### 空闲策略纠正（本地已修正，尚未发布）

用户明确要求：空闲可以主动询问，但不能因连续未回应而主动挂断。已删除英语 Bot 在第三次 time_out 后告别并 action(hang_up) 的分支；任何超时次数均只询问。明确 hang_up 命令与用户主动告别的正常结束流程保留，双工计时、播放确认与打断机制不变。

本地定向回归 tests/test_bot_commands.py、tests/test_failure_boundaries.py 共 19 项通过，包括 8 次已交付提醒及请求重放均不产生 action、5 次语音模拟播放确认后仍保持通话、随后显式 hang_up 仍可正常结束。此次测试使用轻量测试适配器，不加载本地语音模型或调用真实供应商；本地 8765 服务保持停止。尚未部署此次策略变更，因此前述 rtv-20261021-backend-r1 的线上记录不代表已包含此修正。

## 延迟优化与空闲策略合并发布（2026-10-21）

### 实现与回归

先修客户端停止回执：按目标 replyId 采样位置并停止对应音源/排队帧，未开始为 0，重复停止及完成去重，迟到的旧打断不误停新回复。修复 real_e2e_client 在收尾时批量确认被打断回复的问题，测试回执不再把未播放内容升级成已听。Bot 保留原 150ms 回执宽限，已有最终事实立即继续；增加 deliveryWaitMs、snapshotMs、projectionMs 实际分段耗时，不再把 historyReadyMs 累计值直接视为文件库耗时。

预生成增加内容等价判断、明显片段的准备过滤、原始/合格覆盖率及策略拒绝原因。对象、数字、否定、条件变化不直接复用旧答案。云 ASR 收尾期间可读取最新 partial，用原 prepare 通道准备；没有新增提前播放或提交用户定稿的通道。相同候选中优先已就绪且过门槛的安全文本，记录 adoptedReady/adoptionWaitMs，避免最新的空草稿压过已完成草稿。原覆盖率、grace、次数、并发、节流、VAD 与打断门控参数保持不变。

Piper 改为容量 2 的原生块桥接，保留模型级单线程隔离；取消不再交付旧 PCM，但不声称能立即中断 ONNX。长的已确认首句可按词边界缩短 TTS 子段，文字拼接逐字保留，数字表达保持整段；Bot 仍提交完整句，不恢复半句截断。空闲提醒任意次数均不 action(hang_up)，明确挂断命令及用户主动告别仍可正常结束。

最终轻量回归 210 passed、3 deselected，前端播放时钟 9 场景、JS 语法和差异检查通过。包含：危险尾缀、原始覆盖率与拒绝理由、可读草稿优先、晚到 ASR 文本预生成及取消、停止回执免宽限但保留未听历史、线程首块提前交付、背压断开、异常传播与原生取消不重叠。全程未启动本地语音服务，也未在本地加载真实语音模型。

### 远端隔离验收与性能边界

最终候选在香港 ECS 的回环地址 8890、独立数据库验证：六轮真实 WAV→NLS→Bot→Piper、猫对象/23+45=68/慢速语义检查；两次真实语音打断，旧音频泄漏 0；连续五次 time_out 与重试不挂断，明确告别只在当前播放确认后挂断；六至九年级 40 轮真实文本；幂等、游标、冲突、重复回执、未播放及部分播放后的恢复。修正回执的两轮端到端客户端，以及另一个带 800ms 噪声注入的真实语音会话也通过正确性检查，噪声误打断和抢话均为 0。样本很小，不代表实际环境误打断率为零。

最终六轮语音中 5 次接管（约 83%），仅 1 次为接管时已就绪，另 4 次仍需等待模型；不能把 83% 命中解释为 83% 零等待。TTS 首 PCM 中位数为 272.15ms，但回复文本与旧测试不同，不能据此承诺固定收益。两轮端到端停句到实收音频为 1153/1759ms，ASR 定稿到实收音频中位数 705ms；噪声会话停句到首音 1854ms。800ms 停句到首音目标仍未达成，性能验收不得记为 SLA 达标。及时收到停止回执的场景不再空等 150ms，ASR 收尾与模型等待仍是后续瓶颈。

最终隔离实例日志中 QUEUE_OVERFLOW、40010005、asr.failed、asr.reset_failed、reply.failed 均为 0。证据为 .local/latency-opt-{voice,interrupt,commands,recovery,multiturn,e2e,noise}-staging-final.json。播放完成及进度由测试客户端模拟，本次不是物理扬声器、回声或人耳实际听见的验收。

### 发布范围

同期另一路任务修改并发布了录音前端；本次没有覆盖其功能，也没有打包其尚未发布的调试 getter。已验证线上 finishPlayback/reportPlaybackStopped/resetPlayback 与本次停止回执实现一致，目标音源标记和打断入口也已具备。为避免并行前端发布互相覆盖，最终发布包只含 40 个 backend 文件；activate_release 的 backend-only 模式仅备份/更新/回滚后端与运行配置，不改 Web 目录。当前工作区中的录音改动原样保留。

发布包 .local/rtv-20261021-latency-r1.tar.gz，SHA256=fc10e475dd1a1a9a9293268e13be24a007769e163639f80536149276a590810a，版本 rtv-20261021-latency-r1。激活前已确认正式服务无活跃通话，最终候选 40 项后端文件与包逐项一致，隔离服务已停止；后续线上复测另记于本节。

### 发布完成与公网复测

版本 rtv-20261021-latency-r1 已于北京时间 2026-10-21 16:00:18 启动。备份位于 /var/backups/real-time-voice/rtv-20261021-latency-r1，含后端、原运行配置与 SQLite；40 项已部署后端哈希与候选一致，运行配置仅 GIT_COMMIT 改变，未调整模型、ASR、TTS、双工门控或原试用认证。Web 未被本次安装器覆盖，保留另一路已上线的录音功能；核对记录为 .local/latency-opt-remote-after.json。

公网 HTTPS/WSS 使用原认证完成六轮真实语音、猫对象与算式等语义断言、两次打断（旧音频泄漏 0）、连续五次空闲提醒不挂断、重复命令、显式告别播放确认后挂断、状态恢复/幂等和七年级十轮真实文本。六轮中 5 次接管，但仅 1 次接管时已就绪，另 4 次仍等待模型；TTS 首 PCM 中位数 304.1ms。该样本不支持“整体延迟已稳定低于 800ms”或“命中即无等待”的结论。

发布后的 QUEUE_OVERFLOW、40010005、asr.failed、asr.reset_failed、reply.failed 均为 0；复测结束 health 正常、资源占用归零，本地 8765/8890/8891 无监听，远端隔离实例已停止。证据为 .local/latency-opt-{voice,interrupt,commands,recovery,multiturn}-cloud.json、latency-opt-cloud-timings.json；最终轻量回归见 latency-opt-regression.log。此前“空闲策略仅本地、未发布”的章节为历史状态，本次已将此修正正式发布。

## VAD 收尾预生成独立模块与开关对照（2026-10-21，未发布生产）

边听边想已迁至双工侧 `backend/vad_prefetch.py`，默认工厂返回普通 EnglishChatBot。
安静窗口成立 120ms 后提前生成；后续 ASR 更新立即重新请求，不重新等待稳定计时器。
每轮最多 3 次，只保留最新候选；定稿立即进入正式请求，不等候选启动或 grace。
文本、会话、轮次、年级、完整已投影消息和交付上下文逐字一致才接管，不做语义复用。
没有明确无副作用生成适配器的 Bot 不提前调用，正常流式接口与持久化状态逻辑不变。

`DUPLEX_PREFETCH_ENABLED=0/1` 是新开关；未设置时兼容旧 LLM_SPECULATIVE，
两者均未设置默认关闭，`.env.example` 也默认 0。未修改用户现有秘密配置或生产配置。
effectiveLeadMs、remainingWaitMs、adoptionWaitMs、adoptedReady、exactMatch、prefetches
同时进入 duplex.prefetch 日志和可注入生成路径的 bot.latency。未采用的有效提前量记 0，
提前已有文本的剩余等待记 0；未观测的供应商耗时不能当作 0。

### 对照结果与解释

脚本为 `python3.11 -m scripts.check_vad_prefetch_ab`。默认只跑合成机制验证，不加载语音模型、
不启动监听服务；`--real-voice` 仅在远端隔离目录运行实际 NLS/FSMN-VAD、DeepSeek、Piper。
每个 fixture 交错开关顺序，各重复两次，使用独立会话和内存 BotStore，不写生产账本。
真实实验测量从音频中最后非静音样本到服务端第一帧 PCM，不含浏览器网络、声卡或实际扬声器；
播放回执均为模拟。它是小样本试验，不是生产 SLA 或统计显著结论。

| 实验 | 结果 |
| --- | --- |
| 合成 Bot=1s、可重叠窗口=500ms，两组配对 | 关闭约 1005–1017ms，开启约 505ms；配对节省中位约 506ms，证明进行中命中也能省等待 |
| 第一版远端 6 组配对 | 0/6 精确接管，配对差值中位为慢 103.66ms；发现逐次文本去抖会丢失收尾窗口，已修复，未把此轮计为通过收益验证 |
| 修正后远端 6 组配对（12 轮） | 6/6 精确接管，全部未提前就绪；每组 ASR 定稿一致，错误为 0，未在 VAD 定稿前发送音频 |
| 真实有效提前量 | 中位 32.73ms；不是假设中的 500ms |
| 真实剩余等待 | 中位 712.93ms；说明命中仍在等模型 |
| 停说到首 PCM | 关闭组中位 1944.45ms，开启组中位 1747.17ms；配对差值中位快 152.96ms，范围为慢 155.43ms 至快 523.11ms |
| 额外工作量 | 开启侧 6 轮启动 14 个候选生成任务；这不是精确计费请求数或 Token 费用 |

不能把表观 153ms 改善全部归因于预生成：有效提前量只有约 33ms，模型和 TTS 输出/时延存在波动，
6 组中有 3 组反而变慢。当前完整且完全一致的请求能启动时，距 VAD 定稿已很近；
尚未证明稳定收益足以抵消额外调用。新配置默认关闭，保留可控实验能力。
后续应先测完整 ASR 文本可用时间，而非放宽匹配或延长 VAD 断句来人为制造命中。

证据：`.local/vad-prefetch-ab-synthetic-final.json`、`vad-prefetch-ab-real-initial.json`、
`vad-prefetch-ab-real-final.json`；真实报告另归档于 `.local/vad-prefetch-evidence/ab-real-final.json`。

### 正确性、双工与资源验证

本地仅跑轻量回归：240 passed、3 deselected；新增独立模块测试 15 项，
覆盖未定稿不提交、进行中与已就绪接管、逐字不一致拒绝、播放事实改变上下文后拒绝、
关闭开关、未知有状态 Bot 回退、失败候选回退、安静门控、晚到更新不重等、
打断后同轮重新准备与配置优先级。组件测试另验证挂断在等待候选清理前先使 replyId/epoch 失效。
前端播放时钟 9 项通过，未改前端或原有打断门控阈值。

远端独立 loopback 实例真实测试通过：六轮语音含猫对象、算式、语速与句中停顿；
两次打断（模拟已播 0/550ms），lateOldAudio=0；开场白、连续五次空闲提醒不挂断、
重复命令与告别 ACK 后挂断；真实 Bot 全量/游标重放、请求冲突、重复回执和未听/部分听后的状态恢复。
报告为 `.local/vad-prefetch-evidence/{voice,interrupt,commands,recovery}-final.json`。
未执行真人麦克风/扬声器回声验收，不将文件送入 ASR 当作实听通过。

以上测试只使用隔离目录与独立账本，不是生产发布；验证实例测试后停止。
本地语音服务全程未启动，8765/8890/8891 无监听。架构图与标准接口说明同步至 v0.7；
Mermaid 图做了源语义检查，本机未安装图渲染器，未声称完成渲染视觉验收。

### 用户授权正式部署（2026-10-21）

版本 `rtv-20261021-prefetch-r1` 已于北京时间 2026-10-21 18:08:07 启动，
ASR/TTS 预热完成、health 正常。仅部署 backend，44 项文件 SHA256 全部一致，
web 文件与发布前一致，保留录音功能。运行配置仅改变 GIT_COMMIT，
并新增显式 DUPLEX_PREFETCH_ENABLED=0；原 LLM_SPECULATIVE、供应商凭证、
模型、语音参数、认证和其他配置均未改变。

部署安装器不再强制启用旧预生成或重写 Agent/语音开关；健康检查按新开关优先级报告
textBotPreparation，不以预生成开启作为服务正常的条件。新增部署配置回归后，
轻量测试为 241 passed、3 deselected。本地语音服务始终未启动。

备份为 `/var/backups/real-time-voice/rtv-20261021-prefetch-r1`，包含原后端、运行配置和 SQLite 快照。
发布包 `.local/rtv-20261021-prefetch-r1.tar.gz`，
SHA256 `64a25e491ba56f5d578220aac6a24dfc68d581161b7217a6670ee3535356e7ff`。
核对记录 `.local/prefetch-deploy-after.json`。

部署后通过现有公网 WSS 和原认证完成开场白及两轮真实 NLS/LLM/Piper 语音文件测试，
错误为 0，两个正式轮次均 cache=disabled、prefetches=0，确认新开关在实际语音路径生效。
该复测的回执为模拟收包确认，不代表真人扬声器验收；报告 `.local/prefetch-deploy-voice.json`。
此前本节“未发布生产”指实验时状态；本小节为其后经用户授权的正式发布记录。
