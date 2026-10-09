# Limitations

The pass threshold is a flat count (`PASSINGMARKS` of `EXAM_QUESTION_COUNT`) — no per-question weighting, no partial credit within a question. There's no author-identity check (the session URL alone is the credential), no multi-turn follow-up, and no queue — grading happens synchronously inside the submission. Grumpy is also, in principle, prompt-injection-attackable: both the diff and the developer's answers are attacker-influenceable text fed to an LLM. The blind-interpretation grading design (see `app/ai/grading.py`) blunts the obvious cases but isn't a formal defense.

The page blocks pasting into the answer box and blocks copying/cutting anywhere on it, but that's friction, not enforcement: anyone can retype text, turn JavaScript off, or POST the form directly. A submission made without the guard running is still graded normally — it's recorded (`js_active` = `false` on that screen's attempt) and logged with `outcome: no_js`, not failed.
