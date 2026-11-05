# Text chat evaluation

`cases.json` contains 24 original conversations covering preference strength and corrections, assistant identity versus student/fictional age, verified Chinese meanings and examples, and everyday chat. Run on the two real text bot branches with local Ollama:

```sh
.venv/bin/python -m scripts.evaluate_text_and_writing text_chat --output evals/text_chat/results.json
```

Both model variants use the same updated text grounding code. Compare grounded and model-generated turns separately: bypass replies are application behavior, not evidence of either model's knowledge. Curriculum tests use a real scope and omit retrieval and the durable dialogue store to isolate behavior after history projection. `firstMs` measures the first emitted text event at the bot, not browser or audio time. Review replies against the case rubric and check for unsupported claims or missing tasks. Prompts are synthetic and contain no private logs.
