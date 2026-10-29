# 中文 ASR 测试音频

`zh_help_16k.wav` 是本项目使用本地 Piper `zh_CN-huayan-medium` 声音模型合成的测试数据，不包含真人声音或个人数据。内容为：

> 你好，请问有什么可以帮助我的？

音频格式为 16 kHz、单声道、16-bit PCM；首尾静音用于稳定触发 FSMN-VAD 边界。可使用以下命令重新生成：

```bash
python3.11 scripts/generate_asr_fixture.py
```

该 fixture 用于真实 NLS/FunASR 集成验证，不用于评估不同说话人、口音或噪声环境下的识别准确率。
