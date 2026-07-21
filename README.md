# Real-time Voice Avatar

本仓库当前实现第一版可运行龙骨：本地 WebSocket、会话状态、对话历史、双工事件队列、100 ms PCM 帧、前端拨通/挂断与猫开闭口渲染。

默认 ASR 已接入 FunASR：`fsmn-vad` 接收每个 100 ms 音频包，
`paraformer-zh-streaming` 在内部按 600 ms chunk 推理。VAD 检测到一句话结束时，
后端会 flush Paraformer 缓存，并将完整识别文本写入该包的 `status=3` 结果；这会自动触发当前的 Agent → TTS → 猫动画下行链路。

```bash
python3.11 -m pip install -e '.[asr,tts,observability,dev]'
python3.11 -m backend.app
```

打开 `http://127.0.0.1:8765`。首次收到音频时，FunASR 会按其默认模型源下载模型；本机建议使用 `ASR_DEVICE=cpu`。若只验证 WebSocket/媒体协议、不加载模型，可显式使用占位 VAD：

```bash
ASR_BACKEND=stub python3.11 -m backend.app
```

真实识别模式是默认值，也可明确指定：

```bash
ASR_BACKEND=funasr ASR_DEVICE=cpu python3.11 -m backend.app
```

## 本地 LLM（固定 0.5B）

为保证 16GB Intel Mac 上 ASR、TTS 和 LLM 可以共存，真实对话仅使用
Ollama 的量化模型 `qwen2.5:0.5b`，不使用 3B/4B 默认模型：

```bash
ollama pull qwen2.5:0.5b
AGENT_BACKEND=ollama OLLAMA_MODEL=qwen2.5:0.5b python3.11 -m backend.app
```

未设置 `AGENT_BACKEND=ollama` 时，龙骨仍使用 `DemoAgent`，以便在尚未安装
Ollama 时验证 WebSocket、ASR 和媒体链路。

## 本地 TTS（Piper 中文轻量模型）

默认 TTS 是 Piper 的 `zh_CN-huayan-medium`（约 63MB ONNX 模型），会将生成的
22.05kHz 单声道 PCM 自动转换为协议要求的 16kHz，再由双工模块切分为 100ms 包。若
只验证协议，可显式使用 `TTS_BACKEND=demo`。

建立会话成功后，服务端会向文本 Bot 发送 `pick_up` 信号；文本 Bot 固定输出开场白
“您好，我是猫猫，您有什需要帮助的吗”，并通过同一条 TTS/动画下行链路播放。

## 日志与 Trace

默认输出 JSON 结构化日志，包含连接 `traceId`、会话、上/下行包计数、VAD 状态、直接指令和错误。每 10 个音频包采样一条日志，可通过 `AUDIO_LOG_EVERY` 调整。

安装 OpenTelemetry 开发依赖并启用控制台导出，可查看每个 WebSocket、音频解码和下行播放 span：

```bash
python3.11 -m pip install -e '.[observability]'
OTEL_CONSOLE_EXPORTER=1 python3.11 -m backend.app
```
