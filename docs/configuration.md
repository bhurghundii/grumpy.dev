# Configuration

All config is environment variables, validated at startup — grumpy refuses to boot with a missing required var or an insecure `GRUMPY_TOKEN`, rather than failing confusingly later. See [`.env.example`](https://github.com/bhurghundii/grumpy.dev/blob/main/.env.example) for the full annotated list; the ones you'll actually touch:

| Variable | Required | Default | What it does |
|---|---|---|---|
| `DATABASE_URL` | yes | — | Postgres connection string |
| `GRUMPY_BASE_URL` | yes | — | Public URL used to build session links. Never derived from request headers, so it works correctly behind a proxy |
| `GRUMPY_TOKEN` | yes | — | Shared bearer secret gating `POST /sessions` and `GET /verdict`. Rejected at startup if it's the placeholder or under 20 characters |
| `FAKE_GRADER` | yes | — | `true` = free/instant grading via a `looks-good` marker string (good for trying grumpy out); `false` = real grading via Claude |
| `MODEL_API_KEY` | only if `FAKE_GRADER=false` | — | Anthropic API key used by the real grader |
| `GITHUB_STATUS_TOKEN` | no | unset | GitHub token with *Commit statuses: Read and write* on the gated repos (see [Blocking PRs](blocking-prs.md#1-create-the-status-token)). grumpy uses it to post the `grumpy/verdict` status; unset, it posts nothing and your workflow has to gate on `GET /verdict` itself. A 401 from GitHub in the logs means the token is wrong or expired |
| `GRUMPY_ALLOWED_REPOS` | no | unset (any repo) | Comma-separated `owner/name` allow-list. Defense-in-depth if `GRUMPY_TOKEN` ever leaks |
| `EXAM_QUESTION_COUNT` | no | `5` | Maximum questions in the walkthrough, including the fixed high-level first one. The model writes the rest, each anchored to a part of the diff and shown on its own screen. Small changes get fewer: one scoped question per 15 changed lines, so a one-line PR gets only the high-level question. `PASSINGMARKS` is clamped to the sheet length |
| `PASSINGMARKS` | no | `3` | How many screens must be answered correctly to pass. The session fails as soon as that is out of reach. Must be between `1` and `EXAM_QUESTION_COUNT` |
| `MAX_QUESTION_ATTEMPTS` | no | `3` | Tries per question before grumpy reveals the answer and moves on. A revealed question counts for nothing. `1` = one try, no retries |
| `ALLOW_RESTART` | no | `false` | Show a **Start over** button on failed sessions, which resets them to pending with a fresh set of questions on the same commit and link. Unlimited, so turning it on weakens the gate; off by default. See [Blocking PRs](blocking-prs.md#failing-and-trying-again) |
| `MEANIEMODE` | no | `false` | Failure notes become sarcastic and merciless instead of professional. Doesn't change which answers passed, only tone |
| `EVALUATOR` | no | unset (defaults below) | JSON config for checks that reject a PR before any question is asked: `POST /sessions` returns `422` with the reason, and the `grill` job comments it on the PR and goes red. Today there is one check, `size`: `{"size": {"max_changed_lines": 1000, "exclude": ["*.lock", "package-lock.json", "pnpm-lock.yaml", "go.sum"]}}` (those are the defaults). Changed lines are added + removed lines, not context; files matching an `exclude` glob (path or basename) don't count. `max_changed_lines: 0` turns the check off. Omitted keys keep their defaults; unknown keys or bad values fail startup |
| `MAX_DIFF_BYTES` | no | `400000` | Larger diffs are rejected like an `EVALUATOR` check (`422`, red `grill` job). Bounded by the model's context window, not by Postgres: at ~3–4 bytes per token, 400 KB is ~100k–130k tokens. Raise it much further and you accept diffs that can never be graded |
| `SESSION_TTL_DAYS` | no | `7` | How long a question link stays answerable. After that it shows "This session expired" and the `grumpy/verdict` status stays pending; pushing a new commit creates a fresh session |
