# grumpy evals

This directory holds the eval set that validates the grader: real diffs
paired with a question, a candidate answer, and the verdict a correct
grader should reach.

## Running evals

```sh
make eval
```

Runs every case in `cases/` against the real two-call grader
(`app/grading.py`'s `RealGrader` — not `FakeGrader`) and prints pass/fail
per case. Model calls are recorded to `cassettes/<hash>.json`, keyed on a
hash of `(diff, question, answer)`, and replayed on later runs — so once
recorded, `make eval` is fast and free. Delete a case's cassette file to
force a re-record against the real API for that case.

Recording (the first run, or after deleting a cassette) needs
`MODEL_API_KEY` set to a real Anthropic API key:

```sh
MODEL_API_KEY=sk-ant-... make eval
```

Replaying an already-recorded cassette needs no key.

## Case schema

Each case is one JSON object:

```json
{
  "diff": "diff --git a/foo.py b/foo.py\n...",
  "question": "Why does this change move the retry loop inside the lock?",
  "answer": "Because without the lock two callers could both see the retry\ncounter as zero and both back off for the same duration...",
  "expected_passed": true,
  "note": "Correct answer that explains the race, not just what the diff does."
}
```

| Field             | Type      | Meaning                                                                 |
|-------------------|-----------|--------------------------------------------------------------------------|
| `diff`            | string    | The unified diff the question was generated from.                       |
| `question`        | string    | The question grumpy asked about that diff.                              |
| `answer`          | string    | The candidate answer being graded.                                      |
| `expected_passed` | boolean   | What the grader *should* decide for this answer.                        |
| `note`            | string    | Why this case exists — e.g. "vague answer", "diff pasted back", "correct but terse", "correct with unrelated extra detail". |

Cases should span both directions: answers that ought to pass (real
understanding, in the answerer's own words) and answers that ought to fail
(vague hand-waving, or the diff itself pasted back as an "answer").

## The five required cases

`cases/` holds the minimum set the phase 4 spec calls for, all grading the
same small diff (a TTL added to a cache) so the cases are easy to compare:

1. **Correct and complete** — pass.
2. **Correct but terse**, covering about half the change, omitting
   something minor — pass.
3. **Correct content, poor English** — pass. Catches a grader that has
   quietly learned to reward fluency.
4. **Paraphrased directly from the diff**, no understanding shown — fail.
5. **Plausible but wrong about a side effect** — fail. Catches a grader
   that only checks topic overlap.
