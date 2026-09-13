# Changelog

## Unreleased — public release preparation

Findings from a pre-release audit, fixed.

**Fixed**

- The Action sent the wrong diff. `git diff BASE HEAD` is a two-dot,
  tree-to-tree comparison, and `pull_request.base.sha` is the base branch
  tip at event time rather than the merge base — so once anything landed on
  the base branch, the diff also contained the reverse of those commits.
  Authors were shown a phantom revert of a colleague's work and graded on
  it. Now `git diff BASE...HEAD`.
- Fork pull requests failed closed. GitHub withholds secrets from
  fork-triggered runs, so the token was empty, the API returned 401, and the
  gate blocked an outside contributor with an error they could not fix. The
  gate now detects an unconfigured run and skips with an explanation.
- Session tokens were written to grumpy's own logs. The `/s/{token}` path
  segment is the credential; `log_requests` logged the raw path and
  uvicorn's access log repeated it. Both are redacted or disabled now, and
  `httpx`'s request logging is quieted for the same reason.
- Requesting a tutorial breakdown on the last remaining attempt charged an
  Anthropic call, locked the session as `FAILED`, and never displayed the
  breakdown. The final attempt is now reserved for answering.
- `INTEGRATION.md` did not work as written: it named a `GRUMPY_URL` secret
  the workflow never read, and documented a `"PENDING"` response value the
  API returns as `"pending"`.
- Transient model-API failures (429, 529, 5xx, timeouts) surfaced to the
  developer as "grading failed, please try again". They are now retried with
  exponential backoff, honouring `Retry-After`.
- `INTEGRATION.md` documented a verdict-polling loop that no longer matched
  the shipped workflow. It now describes the workflow's actual ten-minute
  budget and states the trade-off (runner minutes versus a check that
  resolves itself) rather than contradicting it.
- `MAX_DIFF_BYTES` defaulted to 1 MB, above the model's context window, so
  oversized diffs failed at grading time with no usable message instead of
  at session creation. Lowered to 400 KB.
- The pass page hotlinked a copyrighted image from a third-party CDN. It is
  an inline SVG now, which also let the CSP drop `img-src` entirely.

**Added**

- MIT `LICENSE` — the repository previously granted no rights at all.
- `SECURITY.md`, with the threat model and the known, accepted trade-offs.
- A `ci` workflow running lint, the test suite, `uv lock --check`, and a
  Docker build. Nothing ran the tests on a PR before.
- Ruff configuration.
- A paste guard on the answer and explain-back textareas, suggested by
  csgrant. A small same-origin script (`app/static/nopaste.js`) blocks
  pasting and dropping text in. That's friction, not enforcement, so whether
  it was running is recorded per submission (`answers.js_active`,
  `tutorials.explanation_js_active`, migration V6) and logged as
  `outcome: no_js` when it wasn't — never used to fail an answer. The CSP
  gains `script-src 'self'`; inline script is still refused.

**Changed**

- Python pinned to 3.14 via `.python-version`, matching the Dockerfile, so
  local development and CI stop running a different interpreter from the
  one the image ships. Suite verified green on 3.14.
- README corrected in place rather than rewritten: the three-dot diff range,
  the reserved final attempt, `MAX_DIFF_BYTES`, the exact CSP, and the fact
  that grumpy now redacts session tokens from its own logs.
- `INTEGRATION.md` linked to `README.md#before-you-deploy-publicly`, an
  anchor that no longer exists — the heading is now "Self-hosting: before
  you make it public".

## Development phases

grumpy was built in numbered phases; the README used to narrate them.

### Phase 4 — real grading

Replaced `FakeGrader` with `RealGrader`: two sequential calls to the
Anthropic Messages API over async httpx, structured outputs plus defensive
parsing. Interpretation runs blind (diff only, no answer) so the comparison
call cannot rationalise toward the answer. Added `model`/`prompt_version`
recording, the persisted and displayed `reasoning`, the eval set in
`evals/cases/`, `MEANIEMODE`, markdown rendering with `nh3` sanitization,
answer retries under `MAX_SESSION_ATTEMPTS`, and the tutorial-breakdown
flow with its step-through walkthrough.

### Phase 3 — the developer-facing loop

`GET /s/{token}` and `POST /s/{token}/answer`, unauthenticated by design
with the token as the credential. Server-rendered Jinja2, no JavaScript.
Post/redirect/get so a refresh cannot resubmit. `FakeGrader` stood in for a
real model so the loop could be proved end to end for free.

### Phase 2 — the Action-facing API

`POST /sessions` and `GET /verdict` behind bearer auth. Idempotent session
creation on `(repo, pr_number, head_sha)` via `INSERT … ON CONFLICT DO
NOTHING RETURNING *`, because two concurrent Action runs on one SHA are the
normal case. The four-state verdict, keeping `UNKNOWN` distinct from
`PENDING` so a force-push creates a fresh session instead of blocking on a
stale one.

### Phase 1 — the skeleton

FastAPI, uvicorn, `uv`, psycopg3 with an async pool and raw SQL, Postgres
16. Startup config validation, structured JSON logging, the advisory-locked
migration runner, and `/healthz`.
