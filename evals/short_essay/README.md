# Short model essay evaluation

`cases.json` contains original writing tasks anchored to eight verified local textbook units. Each prompt lists concrete content requirements for review. The target is 30–65 words. The same eight prompts run in natural and textbook modes; four also run in strict mode. Treat comparisons by mode as paired results, since modes have different acceptance criteria.

Run with both Ollama tags installed and no other local inference in progress:

```sh
.venv/bin/python -m scripts.evaluate_text_and_writing short_essay --output evals/short_essay/results.json
```

The runner uses the current composition service with web search off, the real cumulative vocabulary scope, and writing thinking disabled. Each model is warmed before its block. Results include model digests, the input SHA-256 and vocabulary hashes. Time includes all model calls and server checks. Drafts and final essays are saved for inspection. `rewrites` counts completed drafts after the first; separate glossary calls contribute to total time but are not rewrites. Accepted textbook/strict responses require one editorial rewrite by design. A third draft means one additional attempt. A truncated response is an end-to-end failure even if earlier drafts exist; the first run reports rewrite count as unavailable for unexpected provider errors.

Quality: score final delivered essays against the case's content rubric, age-appropriate grammar and naturalness, continuity, and contextual glossary accuracy. Report delivery and reviewed quality separately; a server pass establishes measurable constraints, not prose quality. Failed requests count as no delivered essay. This set does not evaluate essay grading.
