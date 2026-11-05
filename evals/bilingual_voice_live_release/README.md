# 最终实时独立语音评测

6 个新完整任务，另有静音与低噪声控制。冻结 cases.json 的 WAV 哈希及 frozen.json 的候选与基线源码后，逐帧真实时钟送入原来的 VAD/ASR/Bot/TTS，修改前后各两遍。两次重复不是额外独立题。TTS 缓存关闭，计时不包含模型加载、浏览器播放或麦克风输送。

123 个相关测试通过，1 个跳过。summary.json 保留完成率、全部回应与成功任务的分位数，所有缺词澄清在完整任务中计失败。review.json 为逐题审阅。实际 WAV 转写与补充的中英文固定语言转写同时保留。

全部产品源码在正式计时后保持不变，summary.json 验证 SHA-256。前面诊断集的失败保留，未拿来充当新样本。中文整句与英语请求可用；本轮两条中英混说仍漏目标词，因此整体完成率 8/12，而非 100%。

运行：

```sh
.venv/bin/python -m scripts.evaluate_bilingual_voice_live_release --variant candidate
.local/asr-venv/bin/python -m scripts.transcribe_bilingual_voice_eval --pack evals/bilingual_voice_live_release
```

输入生成脚本会改变冻结文件，不要对这份结果重新生成 WAV。基线快照位于 .local/bilingual-voice-live-release/baseline，代码差异在 baseline-to-candidate.patch。详细报告为 docs/29-3b-bilingual-voice-evaluation.md。
