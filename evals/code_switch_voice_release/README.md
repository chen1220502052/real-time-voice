# 独立完整波形语音集

16 主任务（12 混说、2 中文、2 英文）、2 中文词控制、1 真正缺词控制、3 噪声控制。全部源 PCM 保留并核验哈希。reference target 只用于评分，不送给识别器。

这里 candidate 是可选 Qwen3-ASR 0.6B 实验，未启用到本地服务。offline 两遍，live 主任务子集一遍；实时实验运行后，provider 又修复了非阻塞等待，仅离线 ASR/grounding 指标可用于相同最终源码。frontend-whisper/ 也为中间版本消融。最终默认（Whisper）结果另见 ../code_switch_pause/。

summary.json 与 review.json 保留全部失败及严格例句质量判断；input-integrity.json 记录源段校验。原始录音路径存于结果，完整解释见 ../../docs/30-code-switch-voice-evaluation.md。
