# PRD 实现评审报告

| 项目 | 内容 |
| --- | --- |
| 评审对象 | Real-time Voice Agent Console |
| PRD 版本 | `docs/00-product-requirements.md` v1.0 |
| 评审日期 | 2026-09-07 |
| 评审方式 | 代码静态检查、Python 3.11 自动化测试、真实模型门禁、WebSocket 冒烟、浏览器页面检查 |
| 当前结论 | 可演示 MVP；尚不能认定 P0 全部验收通过 |

## 1. 总体结论

项目已经完成实时语音作品集的主要形态：浏览器采音、WebSocket 双工传输、ASR、LLM、TTS、浏览器播放、打断控制、会话时间线、运行指标和 Stub/Real 双模式均已具备。

当前可以用于内部演示，但仍有若干影响 P0 完成定义的问题，主要集中在真实模型取消、失败恢复、短语音事件关联、上行背压、指标准确性和稳定性证据。综合判断：

| 维度 | 评估 |
| --- | --- |
| 核心功能完成度 | 约 80% |
| 自动化测试基础 | 良好 |
| 浏览器端可观测性 | 基本完成 |
| 实时链路正确性 | 仍有高优先级边界问题 |
| PRD 验收证据可信度 | 约 65% |
| 对外作品集状态 | 修复 P1 项后再正式发布 |

## 2. 已验证结果

### 2.1 自动化与运行结果

- Python 3.11 Stub 回归：`23 passed, 2 skipped`。
- 真实模型门禁：`2 passed`，约 24 秒。
- 5 秒真实 WebSocket 冒烟：116 个会话、0 错误、0 残留任务。
- `web/app.js` 与 `web/audio-worklet.js` JavaScript 语法检查通过。
- 浏览器初始页面正常渲染，服务状态、通话区、诊断区和时间线可见，控制台无错误。
- `git diff --check` 通过。
- 模型权重、虚拟环境和 `third_party` 未被 Git 跟踪。

### 2.2 已实现的关键能力

- 控制事件、媒体事件和时间线事件已分离为独立队列。
- `replyId + generationEpoch` 栅栏已覆盖主要异步回复链路。
- 浏览器支持 ASR partial/final、助手增量文本和被打断消息标记。
- 浏览器尾包通过 `reply_complete` 主动 flush，不再依赖下一包到达。
- 后端队列已设置容量，时间线满时不会反向阻塞实时链路。
- Stub 支持 ASR、LLM、TTS 延迟及错误注入。
- 真实模型测试与默认回归已经隔离。

## 3. 必须修复的问题

### REV-P1-01 模型异常后 Reply 状态无法收敛

- 位置：`backend/duplex.py::_reply_worker`
- 现象：LLM、TTS 或播放异常时只发送错误事件，没有清理当前 Reply。
- 影响：`activeReplyId` 和机器人状态可能永久停留在 `Thinking` 或 `Synthesizing`；用户不继续讲话时，会话无法自行恢复。
- 建议：新增统一的 `fail_reply()` 路径，负责使 epoch 失效、取消任务、清理媒体、恢复机器人状态，并产生 `reply_failed` 事件。
- 必要测试：分别注入 LLM/TTS 异常，确认错误后能继续发起下一轮并正常回复。

### REV-P1-02 Piper 取消不等于真实推理停止

- 位置：`backend/atomic.py::PiperTts.stream_pcm`
- 现象：取消等待 `run_in_executor` 的 Task 后，底层线程仍继续执行；协程锁却会提前释放。
- 影响：新一轮可能与旧线程并发调用同一个 Piper voice，导致 CPU 堆积、资源竞争或线程安全问题。
- 建议：使用单独的串行执行器或可取消的 TTS 工作进程；在旧合成真正退出前不得对同一 voice 发起并发合成。Epoch 栅栏继续负责隔离旧输出。
- 必要测试：使用不可立即取消的同步 TTS Stub，连续打断并确认最大并发合成数为 1、后台任务最终归零。

### REV-P1-03 极短语音事件缺少 Turn 关联

- 位置：`backend/duplex.py::_decode_audio`
- 现象：VAD 在同一音频包返回开始和结束时，Duplex 可能直接收到 `status=3`，此前没有创建 `_active_user_turn_id`。
- 影响：`speech_ended`、`asr_final` 事件没有 `turnId`，但后续用户历史和 Reply 使用新生成的 Turn，时间线断裂。
- 建议：处理任何 `status in {1,2,3}` 的语音事件前保证当前 Turn 存在；`status=3` 完成全部写入后再清空。
- 必要测试：构造第一包即 `status=3` 的 ASR 结果，断言 ASR、用户历史、Reply 使用同一个 Turn。

### REV-P1-04 浏览器上行缺少发送背压

- 位置：`web/app.js::sendAudio`
- 现象：浏览器每 100 ms 直接调用 `WebSocket.send()`，不检查 `socket.bufferedAmount`。
- 影响：后端或网络变慢时，过期音频可能在浏览器持续积压；后端有界队列无法约束浏览器内存和传输延迟。
- 建议：设置约 1 秒上行阈值，超过阈值时丢弃当前或最旧音频，并产生 `uplink_backpressure`/`queue_overflow` 事件。
- 必要测试：模拟高 `bufferedAmount`，确认停止发送过期包且控制消息仍可发送。

### REV-P1-05 30 分钟门禁不是连续会话测试

- 位置：`scripts/stability_test.py`
- 现象：脚本在 30 分钟内快速创建和关闭大量短会话，每个会话只发送三个音频包。
- 影响：该结果证明了连接 churn 和清理能力，但不能证明单会话连续 30 分钟无队列增长、无串轮或无资源泄漏。
- 建议：保留当前脚本作为 `session_churn_test`，另加 `long_session_test`：单一 WebSocket 持续发送音频、周期性制造多轮回复与打断，并定期采样队列、任务数和 RSS。
- 文档调整：在新门禁通过前，不应把 38,708 个短会话描述为“满足连续运行 30 分钟”。

## 4. 建议修复的问题

### REV-P2-01 合法的 0 时间戳会被覆盖

**2026-09-08 状态：已关闭。** 用户和机器人历史均改为显式判断
`is not None`，并增加 `start_time=0` 回归测试。

- 位置：`backend/duplex.py::_accept_final_text`、`_finalize_reply`
- 现象：通过 `start_time or current_time` 选择时间；合法值 `0` 会被当作空值。
- 影响：首轮历史可能出现错误的起止时间，甚至 `startTime > endTime`。
- 建议：使用 `value if value is not None else fallback`。

### REV-P2-02 默认输入队列最多积压 6.4 秒

- 位置：`backend/duplex.py::DuplexDialogue.__init__`
- 现象：默认 64 个 100 ms 包，相当于 6.4 秒音频。
- 影响：慢 ASR 下 VAD、字幕和打断都会明显滞后，不符合实时优先和既有 1 秒设计目标。
- 建议：默认限制为约 10 包；溢出时只丢弃音频事件，不能误丢 `pick_up` 或开发文本事件。

### REV-P2-03 TTS 首包指标会被后续分段覆盖

- 位置：`web/app.js::rememberMetric`
- 现象：同一 Turn 的事件使用 `Map.set(eventType, timestamp)`，后续 `tts_segment_started` 会覆盖首段起点，而 `tts_first_audio` 仍是第一次时间。
- 影响：TTS 首包耗时可能显示为 `0 ms`，掩盖真实延迟。
- 建议：首包指标保存第一次事件；分段指标使用 `segmentIndex` 建立独立键。

### REV-P2-04 ASR 定稿指标缺乏真实测量意义

- 位置：`backend/duplex.py::_decode_audio`
- 现象：`speech_ended` 与 `asr_final` 都是在组合 ASR/VAD 调用完全返回之后连续生成。
- 影响：页面显示的句尾识别延迟通常接近 0，并未包含模型 flush 或端点处理耗时。
- 建议：拆分 VAD 结束和 ASR final 时刻，或使用媒体时间与单调时钟建立明确的 endpoint latency 口径。

### REV-P2-05 真实模型 ASR 门禁没有识别真实语音

- 位置：`tests/test_real_models.py::test_real_asr_and_tts_models_load_and_run`
- 现象：真实 FunASR 测试只输入静音并断言 `status=0`。
- 影响：只能证明模型可以加载和执行，不能证明真实语音会产生 `speech_started/partial/final` 和非空文本。
- 建议：加入短小、许可清晰的 16 kHz PCM/WAV fixture，验证完整 VAD/ASR 生命周期及非空结果。

### REV-P2-06 缺少浏览器播放与打断的可重复自动化

- 位置：`docs/06-prd-acceptance-matrix.md`
- 现象：浏览器尾包、打断停止和五段指标主要依赖人工实测。
- 影响：前端调度回归后，Python 测试仍会全部通过。
- 建议：增加 Playwright/浏览器测试，注入可控音频和 Stub WebSocket 事件，验证预缓冲、尾包 flush、旧 epoch 丢弃、打断计时器清理和指标计算。

## 5. PRD 验收状态修订建议

| 验收项 | 当前建议状态 | 原因 |
| --- | --- | --- |
| AC-01 正常五轮对话 | 部分通过 | Python 层五轮通过；真实浏览器/真实 ASR 五轮缺少可重复证据。 |
| AC-02 播放期间插话 | 部分通过 | 控制通道和 Epoch 已实现；真实浏览器仅人工样本，缺少自动门禁。 |
| AC-03 生成期间插话 | 部分通过 | Async Stub 通过；Piper executor 的底层工作无法真正取消。 |
| AC-04 尾包与短句 | 基本通过 | 服务端补帧和浏览器强制 drain 已实现；应增加浏览器自动化。 |
| AC-05 慢消费者与队列保护 | 未完全通过 | 浏览器无 `bufferedAmount` 限制，后端默认可积压 6.4 秒。 |
| AC-06 断连与恢复 | 基本通过 | 后端任务清理测试通过；真实 ASR executor 正在运行时的关闭竞态尚未覆盖。 |
| AC-07 可重复测试 | 基本通过 | Python 3.11 Stub 回归稳定；干净环境依赖和浏览器测试仍需补齐。 |

## 6. 推荐修复顺序

1. 修复异常后的 Reply 状态收敛。
2. 解决 Piper 同步线程取消与并发问题。
3. 修复短语音 Turn 关联及 0 时间戳问题。
4. 增加浏览器发送背压，并把后端上行队列缩短到约 1 秒。
5. 修正 TTS、ASR 指标口径。
6. 新增单会话 30 分钟门禁。
7. 新增真实语音 ASR fixture 和浏览器播放/打断自动化。
8. 复验后更新 `docs/06-prd-acceptance-matrix.md`，再将 P0 标记为全部通过。

## 7. 最终复验门禁

完成修复后至少执行：

```bash
python3.11 -m pytest -q
RUN_REAL_MODELS=1 python3.11 -m pytest -q -m real_model
python3.11 scripts/stability_test.py --duration 300
python3.11 scripts/long_session_test.py --duration 1800
```

并补充以下证据：

- LLM/TTS 故障后继续下一轮成功。
- 同包开始/结束的短语音拥有完整 Turn 关联。
- 浏览器上行积压超过阈值时执行明确的丢弃策略。
- 真实 Piper 连续打断时没有并发合成和后台任务残留。
- 单会话 30 分钟的队列、任务数和 RSS 趋势。
- 真实语音 ASR 的 partial/final 结果。
- 浏览器尾包和打断 P95 样本数量、原始数据及计算方式。

全部门禁通过后，项目可从“可演示 MVP”提升为“可公开展示的实时语音工程作品集”。
