# PRD P0 验收矩阵

本页把 PRD 的 P0 条目映射到实现与自动化证据。默认测试命令为 `python3.11 -m pytest -q`；真实模型门禁使用 `RUN_REAL_MODELS=1 python3.11 -m pytest -q -m real_model`。

| 需求 | 实现证据 | 验证证据 |
| --- | --- | --- |
| FR-001～003 服务检查、主动建会、完整清理 | `/health`、`WebSocketConnection`、浏览器 `checkEnvironment/startCall/endCall` | `test_disconnect_cleanup_cancels_all_connection_tasks`、浏览器拨通/挂断实测 |
| FR-004 Session/Turn 标识 | `Session`、`_active_user_turn_id`、统一事件模型 | `test_five_turn_conversation_keeps_unique_turns_and_complete_history` |
| FR-101 音频工作线程与 PCM | `audio-worklet.js`、16 kHz 重采样、固定 3224 字节帧 | `test_real_pcm_crosses_websocket_session_and_returns_audio`、稳定性脚本 |
| FR-102 序号连续性 | 前后端双向连续性检查与缺口重同步 | `test_sequence_validator_rejects_gaps_and_duplicates_then_resynchronizes`、`test_audio_sequence_gap_is_reported_and_connection_can_still_close` |
| FR-103～104 ASR partial/final 与 VAD | FunASR/FSMN 适配器、`asr_result`、`speech_started/ended` | `test_funasr_adapter_returns_final_text_on_vad_end`、真实模型门禁 |
| FR-105 有界上行 | 输入队列固定容量，满时 `drop_oldest` 并产生错误事件 | `test_input_queue_overflow_drops_media_without_blocking_control` |
| FR-201～203 流式 Agent/TTS | Turn/Reply 绑定、五字/标点切分、100 ms PCM Framer | `test_duplex_development_reply_becomes_playback_packets`、端到端 PCM 用例 |
| FR-204 Epoch/Reply 双重过滤 | 服务端三段栅栏、浏览器失效 `replyId` 集合、下行元数据 | 生成中/合成中/播放尾部三类打断测试 |
| FR-205 尾包 | 服务端不足 3200 字节补静音；浏览器 `reply_complete` 强制排空 | 单包真实 PCM 端到端用例、浏览器播放完成回执实测 |
| FR-206 预热 | aiohttp startup 并发预热 ASR/TTS | `test_startup_warmup_uses_injected_capabilities`、真实模型门禁 |
| FR-301 三阶段打断 | LLM、TTS、浏览器尾部播放均保持可取消 Reply | `test_interrupt_while_llm_is_generating...`、`test_interrupt_while_tts_is_synthesizing...`、`test_interrupt_during_browser_tail...` |
| FR-302 高优先级控制 | 控制、媒体、时间线三队列隔离 | `test_interrupt_control_event_is_not_blocked_by_full_old_media_queue` |
| FR-303～305 取消与历史隔离 | Epoch 先失效、清缓存、浏览器停播、完整回复才写 History | `test_llm_receives_no_internal_control_signals` 及三类打断测试 |
| FR-401～403 统一事件、时间线、指标 | `make_session_event`、筛选时间线、事件差值指标 | 浏览器实测可见完整事件链与五段指标 |
| FR-405 队列与异常 | `runtime_stats` 每 500 ms 推送，页面显示队列/缓冲/错误 | 队列测试、浏览器实测 |
| FR-501～502 Real/Stub 与故障注入 | FunASR/Ollama/Piper + Demo 三实现；延迟/错误环境变量 | 默认 23 项 Stub 回归、独立真实模型门禁 |
| FR-503 明确启动与检查 | `run_demo.sh`、`run_real.sh`、`check_environment.py` | Stub 与真实配置环境检查均通过 |

## AC 结果

| 验收场景 | 状态 | 证据 |
| --- | --- | --- |
| AC-01 正常五轮对话 | 通过 | 自动化五轮对话；浏览器开场白/用户轮/机器人轮实测 |
| AC-02 播放期间插话 | 通过 | 浏览器尾部回执前保持 Reply 活跃并可打断 |
| AC-03 生成期间插话 | 通过 | LLM 与 TTS 两阶段独立测试 |
| AC-04 尾包与短句 | 通过 | 单字符回复返回单个 100 ms 包并被强制播放 |
| AC-05 慢消费者与队列保护 | 通过 | 所有队列有界；控制绕过媒体；输入满时保留最新帧 |
| AC-06 断连与恢复 | 通过（P0） | 异常断连清理全部任务并允许重新拨通；自动重连属于 P1 |
| AC-07 可重复测试 | 通过 | 默认测试不加载模型，最终命令连续三次通过 |

## 稳定性结论

最终代码在 2026-09-07 的 30 分钟门禁中完成 38,708 个真实 WebSocket 会话，错误数为 0，清理前残留任务为 0，峰值 RSS 为 42,270,720 字节（约 40.3 MiB）。满足 P0“30 分钟无崩溃、无无界任务/队列增长”的门禁。
