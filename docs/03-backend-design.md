# 实时语音虚拟人：后端原子能力设计

## 1. 当前范围

后端第一阶段不先实现完整的会话编排和并发调度，而是先实现、验证四项可独立运行的原子能力：中文 ASR、流式中文 TTS、流式 LLM、音频驱动的猫开闭口动画。它们都以 Python 异步接口暴露，后续再由 WebSocket 会话服务串联。

```mermaid
flowchart LR
  A[中文 ASR] -->|最终文本| L[流式 LLM]
  L -->|文本增量| T[流式中文 TTS]
  T -->|100 ms PCM| C[猫开闭口动画]
  C -->|音视频播放包| P[前端]
```

`ASR → LLM → TTS → 猫动画` 是未来会话的一条处理链；本阶段每个方块都可单独传入文件或模拟输入进行验证。WebSocket 控制协议见[整体架构与 WebSocket 协议](01-architecture-and-protocol.md)，此文档不重复定义网络消息。

## 2. 模型选择

| 原子能力 | 首选开源方案 | 选择原因 | 第一阶段约束 |
| --- | --- | --- |
| 中文 ASR | [Paraformer-zh-streaming（220M）+ fsmn-vad（0.4M）](https://github.com/modelscope/FunASR) | Paraformer-zh-streaming 面向实时流式识别；fsmn-vad 负责低成本检测用户说话起止。 | 固定该组合；输入 16 kHz 单声道 PCM，输出中间和最终中文识别结果。 |
| 中文 TTS | [Piper](https://github.com/OHF-Voice/piper1-gpl) + `zh_CN-huayan-medium` | 约 63MB ONNX 中文模型，在本机 CPU 上可流式输出且实测 RTF 约 0.38。 | 不追求定制音色；Piper 的音频流会转为 16kHz PCM 并按 100ms 下发。 |
| 对话 LLM | [Qwen2.5-0.5B](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct)，由 Ollama 本地量化部署 | 0.5B 参数适配本机 16GB Intel CPU 内存预算；Ollama 提供 OpenAI 兼容的流式接口。 | 固定使用 `qwen2.5:0.5b`，不启用 3B/4B 默认模型，优先首 token 延迟。 |
| 猫动画 | 自研 RMS 能量驱动器 | 首期只需开口/闭口，不需要人脸驱动或视频生成模型；无需 GPU，稳定且延迟极低。 | 两张对齐的猫图片：`mouth_closed`、`mouth_open`。 |

模型版本在落地时必须固定到具体 tag / commit / 模型 revision，并记录许可证。选择依据：FunASR 官方仓库列出 Paraformer-zh-streaming（220M）与 fsmn-vad（0.4M），并提供逐音频块送入 Paraformer 的流式示例；CosyVoice 官方仓库列明 bi-streaming 及低延迟音频输出；Qwen2.5-0.5B 模型卡标明 Apache-2.0 许可和适合本机的 0.5B 参数量。[FunASR](https://github.com/modelscope/FunASR) [CosyVoice](https://github.com/FunAudioLLM/CosyVoice) [Qwen2.5-0.5B](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct)

## 3. 原子能力接口

四项能力都不依赖 WebSocket，对外只接收和产出 Python 对象。音频统一为 16 kHz、单声道、16-bit `pcm_s16le`，帧长 100 ms（3200 字节）。

```python
class AsrEngine:
    async def feed(self, frame: "AsrVadInput") -> "AsrVadResult": ...
    async def reset(self) -> None: ...

class LlmEngine:
    async def stream_reply(self, conversation: "ConversationInput") -> "AsyncIterator[LlmStreamChunk]": ...

class TtsEngine:
    async def stream_pcm(self, request: "TtsInput") -> "AsyncIterator[TtsAudioChunk]": ...

class CatAnimationDriver:
    def drive(self, pcm_100ms: bytes) -> "CatFrame": ...
```

| 能力 | 输入 | 输出 | 单元测试方式 |
| --- | --- | --- | --- |
| ASR | 连续 100 ms 用户 PCM | `partial`、`final`、`speech_start/end` 事件 | 对固定中文音频断帧输入，检查最终文本与端点事件。 |
| LLM | 一段最终用户文本 | 按 token/文本增量产出的异步流 | 对固定提示词检查能先产出首段，再完整结束。 |
| TTS | 一段中文文本 | 持续产出的 PCM 字节流 | 输入“你好”，检查首段音频无需等待整段合成且格式正确。 |
| 猫动画 | 一个 100 ms 下行 PCM | 同序号的开/闭口图片状态 | 输入静音和有声测试帧，检查分别得到闭嘴和开嘴。 |

## 4. ASR + VAD 原子能力

ASR 与 VAD 合并为一个模块，固定采用 `Paraformer-zh-streaming + fsmn-vad`。模块从会话开始到会话结束**持续不断**地消费前端传来的 100 ms 音频包，不能因一句话结束而停止输入流。VAD 给出当前包的说话状态和精确起止位置，Paraformer 只在说话区间内维护当前这一次说话（utterance）的识别缓存。每输入一个音频包，模块必须返回一个对应的识别结果，包括静默包。

### 4.1 输入

```python
AsrVadInput = {
  "sequence": 4,                 # 从 0 递增；第五个音频包为 4
  "speechTime": 500,             # 当前包的结束时间，单位 ms，相对本次通话音频时间轴
  "pcm": b"..."                  # 100 ms、3200 bytes、16 kHz / 单声道 / pcm_s16le
}
```

`speechTime` 从通话音频流起点开始累计。第 1 个包覆盖 `[0, 100)`，第 5 个包覆盖 `[400, 500)`，因此其 `speechTime` 为 `500`。这与 WebSocket 二进制帧的 Unix `timestampMs` 不同：前者用于本次通话内的识别时间轴，后者用于传输诊断。

在 WebSocket 接入时不要求前端额外传递 `speechTime`；管理器根据上行 `sequence` 计算 `speechTime = (sequence + 1) × 100`，再构造本模块的 `AsrVadInput`。

### 4.2 输出

```python
AsrVadResult = {
  "speechTime": 500,             # 当前音频包的结束时间，单位 ms
  "startTime": 430,              # 当前这次说话的开始时间；仅 status=1/2/3 时存在
  "endTime": 500,                # 已确认的说话结束/当前说话进度；仅 status=1/2/3 时存在
  "status": 1,                   # 0=静音，1=开始说话，2=正在说话，3=结束说话
  "text": ""                     # 仅 status=1/2/3 时可选；结束说话时为本次说话的最终完整文本
}
```

`speechTime` 与 `status` 是**每个结果都必须存在**的字段。`startTime`、`endTime` 与 `text` 只在 `status=1/2/3` 时出现。`startTime` 与 `endTime` 均相对于通话音频时间轴，单位毫秒。VAD 应在 100 ms 包内给出边界位置，而不是只精确到整包。`text` 采用 UTF-8 字符串；没有可用文本时返回空字符串，不能返回上一次说话的旧文本。

| `status` | 含义 | `startTime` | `endTime` | `text` |
| ---: | --- | --- | --- | --- |
| `0` | 当前包为静音，且不属于结束包 | 不返回 | 不返回 | 不返回 |
| `1` | 当前包检测到一次新说话 | 这次说话的实际开始时间 | 当前包结束 `speechTime` | 可为空；首期允许为空 |
| `2` | 当前说话仍在继续 | 保持本次说话的开始时间 | 当前包结束 `speechTime` | 可为空或当前中间识别文本 |
| `3` | 当前包检测到本次说话结束 | 保持本次说话的开始时间 | 实际结束时间 | 本次说话的**最终完整文本** |

`status=3` 表示“结束说话”，不表示关闭整个 WebSocket 会话。只有该状态的最终文本才提交给 LLM；`status=1/2` 的中间文本仅用于前端字幕或调试，不能触发 LLM 回复。

### 4.3 示例：第五包开始说话、第十包结束

每包 100 ms。第 5 个包覆盖 `[400, 500)`，用户在该包内第 30 ms 开始说话，即时间轴 `430 ms`。此包的结果为：

```json
{
  "speechTime": 500,
  "startTime": 430,
  "endTime": 500,
  "status": 1,
  "text": ""
}
```

第 10 个包覆盖 `[900, 1000)`，用户在该包内第 50 ms 结束说话，即时间轴 `950 ms`。若本次完整识别为“您好”，此包的结果为：

```json
{
  "speechTime": 1000,
  "startTime": 430,
  "endTime": 950,
  "status": 3,
  "text": "您好"
}
```

该结束包之后，下一包若仍为静音，应返回：

```json
{"speechTime":1100,"status":0}
```

### 4.4 多轮间断说话

音频包流在整段通话期间持续输入；用户静默往往是正在听机器人说话，但这些静默包仍必须逐包返回 `status=0`。当用户再次开口时，模块从新的 `status=1` 开始一轮独立的 utterance，不能沿用上一次的 `startTime`、`endTime`、`text` 或 ASR 缓存。

例如第 30 个包覆盖 `[2900, 3000)`，用户在此包内再次开始说话，且开始点为 `2930 ms`：

```json
{
  "speechTime": 3000,
  "startTime": 2930,
  "endTime": 3000,
  "status": 1,
  "text": ""
}
```

后续语音包应依次返回 `status=2`，直到该轮说话结束的包返回 `status=3` 与这次新说话的最终文本；两轮说话之间的所有包都只返回 `{ "speechTime": N, "status": 0 }`。

### 4.5 实现要求

- VAD 对每个 100 ms 包立即返回状态；`Paraformer-zh-streaming` 的 ASR 内部将连续 100 ms 包累积为模型所需的推理块（首期按 600 ms），再产生中间文本。说话结束时必须立即 flush 剩余缓存并输出 `status=3` 的最终文本。
- 第一阶段优先在普通话场景验证识别准确度，再考虑方言、热词和说话人分离。
- `reset` 必须丢弃当前未完成说话及模型缓存，供用户打断或会话关闭使用。

## 5. ADK 聊天 Agent 与流式 LLM

LLM 原子能力由一个简单的 ADK 聊天 Agent 提供。模型运行在本地 Ollama（本文将用户所述的 “Ollm” 按 Ollama 实现），调用 `http://localhost:11434/v1/chat/completions` 的 OpenAI 兼容流式接口。ADK 是 Agent 层：负责系统提示词拼接、可选 RAG 检索和消息规整；它不保存也不生成对话历史。

上游模块是对话历史的唯一所有者。每次调用都将完整的、有序的历史传给 Agent，结构与 OpenAI Chat Completions 的 `messages` 一致；为贴近业务语义，输入允许 `machine`，Agent 在调用模型前将其规范化为 OpenAI 的 `assistant`。

### 5.1 输入

```json
{
  "messages": [
    {"role": "user", "content": "你好"},
    {"role": "machine", "content": "你好，请问有什么可以帮你？"},
    {"role": "user", "content": "今天天气怎么样？"}
  ]
}
```

输入约束：

- `messages` 至少有一条消息，最后一条必须为 `user`。
- `role` 只接受 `user`、`machine`；`machine` 在发往 Ollama 前转换为 `assistant`。
- 历史顺序由上游保证，Agent 不做持久化、不追加、不删除历史；超长历史的截断或摘要也由上游完成。

### 5.2 Agent 职责

```mermaid
flowchart LR
  H[上游对话历史] --> A[ADK 聊天 Agent]
  R[可选 RAG 检索] --> A
  A --> P[系统提示词 + RAG 上下文 + 标准 messages]
  P --> O[Ollama / OpenAI 兼容流式接口]
  O --> A
  A --> S[OpenAI 风格流式文本块]
```

1. 校验并规范化上游历史：`machine → assistant`。
2. 拼接固定系统提示词，例如要求角色以简短、自然的中文语音口吻回复。
3. 若配置 RAG，使用最后一条用户消息检索资料，将结果作为受长度限制的上下文放入系统消息；未配置或检索为空时直接跳过。
4. 调用 Ollama 的 OpenAI 兼容流式 Chat Completions 接口，模型固定为 `qwen2.5:0.5b`。
5. 只将回复文本增量输出给下游；工具调用、RAG 检索过程、思考内容和 ADK 内部事件不得送入 TTS。

发往 Ollama 的核心请求为：

```json
{
  "model": "qwen2.5:0.5b",
  "messages": ["规范化后的 system / assistant / user 消息"],
  "stream": true
}
```

### 5.3 输出：OpenAI 风格流式文本块

Agent 输出保持 Chat Completions 的流式结构。每收到一个有文本内容的模型块即原样转发；下游只读取 `choices[0].delta.content`。

```json
{
  "id": "chatcmpl_local_001",
  "object": "chat.completion.chunk",
  "created": 1760000000,
  "model": "qwen2.5:0.5b",
  "choices": [
    {
      "index": 0,
      "delta": {"content": "今天"},
      "finish_reason": null
    }
  ]
}
```

流结束块不含文本，且 `finish_reason` 不为 `null`：

```json
{
  "id": "chatcmpl_local_001",
  "object": "chat.completion.chunk",
  "created": 1760000001,
  "model": "qwen2.5:0.5b",
  "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
}
```

异常时，Agent 停止流并向会话编排层抛出结构化错误；不得把错误文本当成虚拟人回复交给 TTS。

### 5.4 5 字切分

`TextChunker` 位于 Agent 输出与 TTS 之间，不是模型：只拼接 `choices[0].delta.content` 的非空文本。累计约 5 个汉字后将文本交给 TTS；遇到 `，。！？；` 等标点可在 2–5 个汉字间提前提交；没有标点时最多累计 12 个汉字必须提交；收到 `finish_reason` 后立刻提交剩余字符。按 Unicode 字符处理，禁止按 UTF-8 字节截断。

```text
流式块：“你好，” → TTS: “你好，”
流式块：“很高兴认识你” → TTS: “很高兴认识”
流结束 → TTS: “你”
```

## 6. 流式中文 TTS

TTS 是独立原子能力：输入一段完整、可朗读的中文文本和基础语音配置，立即持续输出 PCM 音频流。它不负责 LLM 文本切分、不负责 100 ms 分帧，也不管理会话历史；`TextChunker` 将短文本片段逐个提交给它。

### 6.1 输入

```json
{
  "requestId": "reply_1_segment_0",
  "text": "你好，很高兴认识你。",
  "config": {
    "speed": 1.0,
    "volume": 1.0,
    "voice": "default"
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `requestId` | string | 调用方生成的唯一标识；用于关联该段文本和返回音频流。 |
| `text` | string | 要合成的中文文本；不能为空，建议由 `TextChunker` 控制为短句。 |
| `config.speed` | number | 语速倍率，首期范围 `0.8–1.2`，默认 `1.0`。 |
| `config.volume` | number | 线性音量倍率，首期范围 `0.0–2.0`，默认 `1.0`。`0` 产生静音但保持正确的时长。 |
| `config.voice` | string | 内置音色 ID，默认 `default`。服务端维护允许列表；不接受用户参考音频，不支持音色克隆。 |

配置缺失时使用默认值；范围非法或音色不在允许列表时，在开始音频流之前返回 `INVALID_TTS_CONFIG` 错误。第一阶段至少提供 `default`，其余内置音色可以后续按模型实际支持情况加入列表。

### 6.2 输出

TTS 输出是按生成顺序持续产生的 PCM 块，块大小不固定，不能假设一个输出块正好是 100 ms。统一输出格式为 16 kHz、单声道、16-bit `pcm_s16le`。

```python
TtsAudioChunk = {
  "requestId": "reply_1_segment_0",
  "chunkIndex": 0,               # 从 0 递增
  "sampleRate": 16000,
  "channels": 1,
  "sampleFormat": "pcm_s16le",
  "audio": b"...",              # 非空 PCM 字节；长度可变
  "isFinal": false
}
```

最后一个块可以携带剩余音频并设为 `isFinal: true`；若没有剩余音频，则发送一个 `audio: b""`、`isFinal: true` 的结束块。相同 `requestId` 的 `chunkIndex` 必须连续且严格递增。TTS 不得为了等待完整文本而延迟首个音频块。

### 6.3 与 100 ms 播放包的衔接

`PcmFramer` 位于 TTS 下游而非 TTS 内部。它按 `requestId` 顺序拼接所有 `TtsAudioChunk.audio`：累计满 3200 字节即输出一个 100 ms `PlaybackAudioFrame`；收到 `isFinal` 后，剩余不足 3200 字节的尾部补静音并输出最后一帧。每帧有会话级单调递增 `sequence`，供猫动画驱动和前端同步使用。

```mermaid
flowchart LR
  T[TextChunker 文本片段] --> I[TtsInput]
  I --> E[TTS]
  E -->|可变长度 TtsAudioChunk 流| F[PcmFramer]
  F -->|固定 100 ms / 3200 bytes PCM| A[猫动画驱动与前端播放包]
```

### 6.4 取消与异常

调用方可按 `requestId` 取消正在合成的文本段。TTS 收到取消后必须停止后续输出，且不会发送 `isFinal: true`；`PcmFramer` 丢弃该段尚未组成完整 100 ms 帧的尾部数据。模型异常、文本为空或配置非法时，返回结构化错误，不能把错误信息转成音频。

```python
PlaybackAudioFrame = {
  "reply_id": 1,
  "sequence": 42,
  "pcm_100ms": b"..."  # 恰好 3200 bytes
}
```

## 7. 音频驱动猫开闭口动画

猫动画是确定性音频驱动模块，不使用生成式视频、数字人或表情模型。它对每个 100 ms 下行音频包计算 RMS 音量，并产生一条同序号的猫图片状态。首期只有两张静态资源：`mouth_closed`（闭嘴待机）和 `mouth_open`（张嘴说话）；两张图片必须分辨率、背景、位置完全对齐。

### 7.1 输入

动画模块只接受已经由 `PcmFramer` 对齐好的 100 ms 音频帧：

```python
CatAnimationInput = {
  "replyId": 1,
  "sequence": 42,               # 会话下行音频帧序号
  "speechTime": 4200,            # 当前帧结束时间，单位 ms，相对本次下行播放时间轴
  "pcm": b"..."                  # 恰好 3200 bytes，16 kHz / 单声道 / pcm_s16le
}
```

输入 PCM 长度必须为 3200 字节；格式或长度错误直接返回 `INVALID_AUDIO_FRAME`，不生成图片状态。每次调用只处理一个音频帧，调用顺序必须按 `sequence` 递增。

### 7.2 RMS 与开闭口规则

先将 PCM 解析为有符号 16-bit 整数并归一化到 `[-1, 1)`，计算：

```text
RMS = sqrt(sum(sample²) / sample_count)
```

模块保存上一帧的口型和连续低能量帧数，按以下滞回规则决定当前状态：

| 条件 | 输出状态 | 说明 |
| --- | --- | --- |
| `RMS >= openThreshold` | `mouth_open` | 有明显语音能量，立即张嘴并将低能量计数清零。 |
| 上一状态为开口且 `RMS >= closeThreshold` | `mouth_open` | 位于两个阈值之间时保持张嘴，避免频繁切换。 |
| `RMS < closeThreshold`，连续不足 200 ms | 保持上一状态 | 100 ms 的短暂停顿不立即闭嘴。 |
| `RMS < closeThreshold`，连续达到 200 ms | `mouth_closed` | 连续两个低能量音频帧后闭嘴。 |
| 上一状态为闭口且介于两个阈值之间 | `mouth_closed` | 未达到张嘴阈值时保持闭嘴。 |

第一阶段配置默认值为 `openThreshold = 0.020`、`closeThreshold = 0.012`；二者均为归一化 RMS，且必须满足 `0 <= closeThreshold < openThreshold < 1`。实际部署时应使用预录 TTS 音频校准这两个值，但不得在运行时为每个会话随意改变。

### 7.3 输出

每个合法输入帧**恰好输出一条** `CatAnimationFrame`，即使输入是静音帧也必须输出闭嘴或保持后的状态：

```json
{
  "replyId": 1,
  "sequence": 42,
  "speechTime": 4200,
  "avatarState": "mouth_open",
  "assetId": "mouth_open",
  "durationMs": 100
}
```

| 字段 | 说明 |
| --- | --- |
| `replyId` | 对应的 LLM 回复标识，供播放中断时清理。 |
| `sequence` | 必须与输入音频帧、下行音频帧完全相同。 |
| `speechTime` | 必须与输入音频帧完全相同。 |
| `avatarState` | `mouth_open` 或 `mouth_closed`。 |
| `assetId` | 当前应展示的静态图片资源 ID；首期与 `avatarState` 相同。 |
| `durationMs` | 固定为 `100`，表示此状态覆盖的播放时长。 |

### 7.4 与播放包的衔接

会话层将 `PlaybackAudioFrame` 与同序号的 `CatAnimationFrame` 组成一个下行 100 ms 音视频播放包，按整体协议下发。前端开始播放该段音频时，以 `assetId` 切换猫图片；因此声音与开闭口共享 `sequence` 和 `speechTime`，无需额外的视频生成或编码。

```mermaid
sequenceDiagram
  participant F as PcmFramer
  participant C as 猫动画驱动器
  participant S as 会话层
  participant P as 前端

  F->>C: 100 ms PCM（sequence=42）
  C-->>S: CatAnimationFrame（sequence=42）
  F-->>S: PlaybackAudioFrame（sequence=42）
  S-->>P: 100 ms 音视频播放包
  P->>P: 播放 PCM，并切换 assetId 图片
```

### 7.5 验收

1. 输入任意合法的 100 ms PCM 帧都会产生一条同序号动画帧。
2. 静音连续 200 ms 后一定输出 `mouth_closed`。
3. 连续有声帧输出 `mouth_open`；阈值附近不应每 100 ms 来回闪烁。
4. 两张猫图片切换时不发生尺寸、背景或位置跳动。

## 8. 双工会话编排器概览（非规范性）

双工模块的完整、唯一规范已拆分至[双工对话模块设计](04-duplex-dialogue-design.md)。该文档中的 Session、ConversationHistory 与四层 DuplexDialogue 设计优先于本节的概览说明。

### 8.1 职责与边界

双工会话编排器（`DuplexSession`）是后端最核心的原子能力。一个活跃的 `sessionId` 对应一个 `DuplexSession`；它同时维持两条彼此独立的实时链路：

- **上行链路**：持续接收前端每 100 ms 的用户音频，交给 ASR+VAD；无论虚拟人是否正在说话，都不能停止接收或识别。
- **下行链路**：在收到一次用户说话的最终 ASR 文本后，运行 ADK Agent → TextChunker → TTS → PcmFramer → 猫动画，持续输出每 100 ms 的音视频播放包。

编排器不实现模型推理，也不直接读写 WebSocket；WebSocket 管理层只负责将上行包调用 `pushAudio`，并持续读取 `events()` 下发给前端。这样同一编排器也可用预录音频进行离线测试。

```mermaid
flowchart LR
  I[上行 100 ms 音频] --> D[DuplexSession]
  D --> A[ASR + VAD]
  A -->|status=3 final text| G[ADK Chat Agent]
  G --> X[TextChunker]
  X --> T[流式 TTS]
  T --> F[PcmFramer]
  F --> C[猫动画]
  C --> D
  D --> O[下行 100 ms 音视频播放包]
  D --> K[打断 / 关闭 / 背压]
```

### 8.2 输入与输出

```python
class DuplexSession:
    async def push_audio(self, frame: "UplinkAudioFrame") -> None: ...
    async def close(self, reason: str) -> None: ...
    async def events(self) -> "AsyncIterator[DuplexEvent]": ...
```

输入是来自 WebSocket 管理层的完整 100 ms 上行音频帧：

```python
UplinkAudioFrame = {
  "sessionId": "sess_01J...",
  "sequence": 42,
  "speechTime": 4200,
  "pcm": b"..."  # 3200 bytes, 16 kHz / 单声道 / pcm_s16le
}
```

输出是一个有序异步事件流：

| `DuplexEvent.type` | 触发时机 | 载荷 |
| --- | --- | --- |
| `asr_result` | 每消费一个上行音频包 | `AsrVadResult`；用于日志、字幕或上游观察，不必下发给前端。 |
| `playback_packet` | 生成一个完整下行 100 ms 包 | 同序号 `PlaybackAudioFrame + CatAnimationFrame`。 |
| `playback_interrupt` | 用户打断当前虚拟人回复 | 被中断的 `replyId` 与原因 `barge_in`。 |
| `close_session` | 用户告别、服务异常或外部关闭 | 关闭原因；由 WebSocket 管理层编码为协议消息。 |
| `error` | 不可恢复的模型或编排错误 | 稳定错误码与诊断信息，不包含模型堆栈。 |

`playback_packet` 的内部对象如下。`sequence` 必须按下行音频时间轴单调递增；`replyId` 用于识别属于哪一次虚拟人回复。

```python
PlaybackPacket = {
  "sessionId": "sess_01J...",
  "replyId": 3,
  "sequence": 108,
  "speechTime": 10800,
  "audio": b"...",              # 100 ms / 3200 bytes PCM
  "avatar": {
    "avatarState": "mouth_open",
    "assetId": "mouth_open",
    "durationMs": 100
  }
}
```

### 8.3 并发任务与队列

一个 `DuplexSession` 在同一 `asyncio` 事件循环中启动以下长期任务。每个任务只处理自己的队列，模型推理若为阻塞调用必须在专用线程/进程或本地模型服务中运行，不能阻塞音频接收。

| 任务 | 输入 | 输出 | 规则 |
| --- | --- | --- | --- |
| `ingress` | `pushAudio` 调用 | `uplinkQueue` | 校验序号和 3200 字节长度；连续异常时报告错误。 |
| `asrLoop` | `uplinkQueue` | `asr_result`、`turnQueue` | 每包调用 ASR+VAD，始终运行，不因下行回复而暂停。 |
| `replyLoop` | `turnQueue` | `ttsTextQueue` | 对 `status=3` 的最终文本创建一次回复；运行 ADK Agent 和 TextChunker。 |
| `ttsLoop` | `ttsTextQueue` | `playbackQueue` | 依次合成短文本、分帧、驱动猫动画，得到播放包。 |
| `egress` | `playbackQueue` | `playback_packet` | 按下行 `sequence` 有序产生事件。 |

队列限制按实时音频时长而非条数计算：`uplinkQueue` 最大 1 秒，`playbackQueue` 最大 2 秒。上行队列满时丢弃最旧音频包并记录指标，以控制用户说话延迟；下行播放队列满时必须暂停 TTS/动画生产并等待可用空间，不能丢弃已生成的机器人媒体包，否则会造成回复缺字。`turnQueue` 和关闭/错误等控制事件不得因媒体背压而丢失。

```mermaid
sequenceDiagram
  participant F as 前端/WS 管理层
  participant D as DuplexSession
  participant A as ASR+VAD
  participant R as 回复链路

  par 不间断上行
    loop 每 100 ms，整个 session 持续
      F->>D: pushAudio(frame)
      D->>A: feed(frame)
      A-->>D: AsrVadResult
    end
  and 下行回复
    A-->>D: status=3 + final text
    D->>R: 启动一轮回复
    loop LLM/TTS 持续产出
      R-->>D: PlaybackPacket（每 100 ms）
      D-->>F: playback_packet
    end
  end
```

### 8.4 回合、打断与回声

#### 回合创建

当且仅当 ASR+VAD 返回 `status=3` 且 `text` 非空时，`asrLoop` 生成一个新的用户回合。它先把该最终文本追加到**上游拥有的对话历史**，再调用 ADK Agent。Agent 回复开始时创建新的 `replyId`，该 ID 贯穿 TextChunker、TTS、播放包和可能的打断事件。

在调用 Agent 前执行轻量告别检测：若最终文本匹配“再见”“拜拜”等结束意图，编排器不启动 LLM/TTS，直接产生 `close_session(reason=user_goodbye)`。

#### 用户打断（barge-in）

当虚拟人仍有未播放/正在生成的 `replyId`，并且 ASR+VAD 首次返回新用户话语的 `status=1` 时，编排器立刻：

1. 取消该 `replyId` 的 Agent 与所有未完成 TTS 请求。
2. 从 `playbackQueue` 删除该 `replyId` 尚未播放的音视频包。
3. 产生 `playback_interrupt(replyId, reason=barge_in)`；WebSocket 管理层下发给前端，前端停止已调度但尚未开始的旧音频并让猫恢复闭嘴。
4. **继续**把所有上行包送给 ASR+VAD，直至这次新用户话语返回 `status=3`，再正常创建新回复。

浏览器麦克风必须启用回声消除（`echoCancellation: true`）。编排器仍以 VAD 的 `status=1` 为打断依据，但应记录打断频率；若播放声音被麦克风回采导致误打断，应优先校准前端回声消除，而不是在后端盲目丢弃用户音频。

### 8.5 关闭、异常与顺序保证

| 场景 | DuplexSession 动作 | 对外事件 |
| --- | --- | --- |
| 前端 `close_session` | 停止接收、取消全部任务、清空媒体队列并释放模型会话缓存。 | 无需额外媒体；WebSocket 管理层关闭连接。 |
| 用户告别 | 取消回复链路并清空播放队列。 | `close_session(reason=user_goodbye)`。 |
| 上游 WebSocket 断开 | 与前端主动挂断相同，但不再尝试发送事件。 | 无。 |
| ASR 单包异常 | 记录并产生 `asr_result` 诊断；下一包继续。 | 仅在连续异常达到阈值后 `error`。 |
| LLM/TTS 异常 | 取消当前回复，不影响上行 ASR。 | `error`；可保持会话以等待用户下一次说话。 |

顺序要求：上行 `sequence` 必须严格递增；下行播放包也必须严格递增。每个播放包的音频与猫动画必须使用同一 `sequence` 和 `speechTime`。被取消 `replyId` 的包不得在 `playback_interrupt` 之后继续从 `events()` 输出。

### 8.6 双工模块验收

1. 虚拟人下行播放期间，上行 ASR+VAD 仍连续处理每一个 100 ms 用户包，包括静默包。
2. 一次用户话语结束后，能在 LLM 尚未结束前输出首个 100 ms 播放包。
3. 用户在虚拟人说话时开口，旧回复被取消，前端收到 `playback_interrupt`，新用户话语仍能得到完整 ASR 结果。
4. 下行音频与猫动画的 `sequence`、`speechTime` 始终一致，且无乱序播放。
5. 用户/服务端关闭、断连和模型错误不会遗留运行中的任务、模型缓存或无界队列。

## 9. 交付顺序与验收

1. **猫动画驱动器**：准备两张对齐的猫图片；对静音与人声 PCM 生成正确开闭口状态。
2. **TTS**：固定输入中文文本，持续得到标准 PCM；分帧后每个包恰好 3200 字节。
3. **LLM + TextChunker**：对输入文字流式产生回复，验证约 5 字即可把首段交给 TTS。
4. **ASR**：输入已切分的中文音频，验证 VAD 起止事件和最终中文文本。
5. **链路冒烟测试**：用一条预录中文语音依次跑通 ASR → LLM → TTS → 猫动画，生成顺序正确的 100 ms 音视频播放包。
6. **DuplexSession**：接入连续上行音频，验证上行不间断、下行流式生成、用户打断、关闭和队列背压。

完成以上能力后，再将 `DuplexSession` 接入 WebSocket 管理层，并实现资源配额和多会话并发。
