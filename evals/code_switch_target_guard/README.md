# 最终默认版本的独立语音验证

5 个完整任务、3 个原话真的缺词的问题、3 个噪声控制，各版本一次实时测试。原 ASR 均保持不变，LOCAL_ASR_TARGET_VERIFY=0。最终改动仅在空目标问法时澄清，不自动补词。缺词澄清 1/3→3/3，完整任务均 3/5；window 仍漏识，房子被重复而未解释，均计失败。

小样本只验证保护与延迟，不代表总体 ASR 识别率。所有源段完整保留并核验 SHA，源码冻结在第一次推理前。详见 summary.json、review.json、frozen.json 与 ../../docs/31-code-switch-target-verification.md。
