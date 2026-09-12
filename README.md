# grumpy

A merge gate that checks the developer understands the change, not just that
the tests pass. Phase 1 built the skeleton. Phase 2 added the Action-facing
API (`POST /sessions`, `GET /verdict`, bearer auth). Phase 3 closed the loop
end to end with `FakeGrader` standing in for a real model. Phase 4 replaces
it: a two-call grader against the real Anthropic API. The question is still
fixed — no question generation yet.

## Stack

Python, FastAPI, uvicorn, `uv`, psycopg3 (async pool, raw SQL, no ORM),
Postgres 16. See `pyproject.toml` for exact versions.

## Running it

### Docker (the way self-hosters will run it)

```sh
cp .env.example .env
docker compose up --build
curl localhost:8000/healthz
# {"status": "ok"}
```

### Local dev

```sh
make dev    # starts Postgres via compose, runs the app locally with uv --reload
make test   # uv run pytest (spins up its own Postgres via Testcontainers)
make seed   # inserts a realistic session directly into Postgres, prints its URL
make eval   # runs evals/cases/ against the real grader (needs MODEL_API_KEY the first time)
make down   # docker compose down
```

`make seed` is how you develop the answer-page UI — no Action, no GitHub,
no webhook. It works against a bare `docker compose up`; run it, open the
printed URL, answer the question, watch the verdict.

## ⚠️ Postgres port: 5433, not 5432

`docker-compose.yml` publishes Postgres on host port **5433**. Port 5432
was already bound by another Postgres instance on the reference dev
machine, so 5433 was picked to avoid a silent collision. This only affects
the *host*-side mapping — inside the compose network, the `app` service
still talks to `db` on its normal internal port 5432. If you're connecting
from the host with `psql` or a GUI client, use `localhost:5433`.

## Config

All config is via environment variables (see `.env.example`), validated at
startup with `pydantic-settings`. `DATABASE_URL`, `GRUMPY_BASE_URL`,
`GRUMPY_TOKEN`, and `FAKE_GRADER` are required; missing any of them fails
startup immediately with a clear error naming the variable(s), rather than
failing somewhere deeper. `MODEL_API_KEY` is required only when
`FAKE_GRADER=false`, and is what `RealGrader` uses to call the Anthropic
API — see the Grading section below. `GRUMPY_TOKEN` is rejected at startup
if it's the `.env.example` placeholder or shorter than 20 characters — see
"Self-host isolation model" below for why, and for the optional
`GRUMPY_ALLOWED_REPOS`.

### Self-host isolation model

grumpy has no GitHub App, no OAuth, no webhooks, and no users/orgs/tenants
table. Each self-hosted deployment is isolated purely by two things it
controls: `GRUMPY_TOKEN` (the shared bearer secret gating `POST /sessions`
and `GET /verdict`) and `GRUMPY_BASE_URL` (used to build session URLs,
never derived from the request's Host header). Because there's no shared
credential baked into the codebase, one self-hosted deployment can't
cross-talk with another's by construction — the only way two deployments
could ever collide is if a self-hoster reused or leaked a token. Anyone
holding your `GRUMPY_TOKEN` can create sessions and read verdicts for
*any* `owner/name` repo string, not just your own — there's no repo
scoping baked into the API by default.

Two things follow from this:

- Treat `GRUMPY_TOKEN` as a real secret. Generate it with
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`, not by
  hand — grumpy refuses to start with the `.env.example` placeholder or
  anything shorter than 20 characters, but that only catches the laziest
  mistakes, not a token that leaks after being generated properly.
- Set `GRUMPY_ALLOWED_REPOS` (comma-separated `owner/name` list) if you
  want defense-in-depth against a leaked or guessed token: a disallowed
  repo gets `403` from both `POST /sessions` and `GET /verdict`, before
  any other work happens. Leaving it unset permits any repo — the
  default, backward-compatible behavior.

## API (phase 2)

Both endpoints below require `Authorization: Bearer <GRUMPY_TOKEN>`.
`/healthz` does not. If `GRUMPY_ALLOWED_REPOS` is configured, both also
return `403` for a `repo` outside that allow-list — see "Self-host
isolation model" above.

**`POST /sessions`** — `{repo, pr_number, head_sha, base_sha, diff}` →
`{session_url, status, question}`. Idempotent on `(repo, pr_number,
head_sha)`: a second call for the same triple returns the existing session
with `200` instead of creating a new one with `201` — this is the normal
case for two concurrent Action runs on the same head SHA, not an edge case.
`head_sha`/`base_sha` must be full 40-character hex SHAs (what GitHub
Actions actually sends); short SHAs are rejected with `422`, since the
unique index and verdict lookups are exact-string matches, not prefix
resolution. Diffs over `max_diff_bytes` (default 1 MB) get `413`.

The raw request body itself is also capped, independently of the diff
check above: `max_request_body_bytes` (default 8 MB — generous headroom
over `max_diff_bytes`, since JSON-encoding a diff can expand its byte
count) is enforced by an ASGI-level middleware against actual bytes
received off the wire, before Starlette or Pydantic ever buffers the body
into memory. Neither setting has a `.env.example` entry (same as
`max_diff_bytes`); override via the `MAX_REQUEST_BODY_BYTES` /
`MAX_DIFF_BYTES` env vars if the defaults don't fit your use case.

**The diff is cumulative, not per-commit.** The Action computes it as
`git diff BASE_SHA HEAD_SHA` — the whole PR against its base — every time
it creates a session, not the delta since the last push. An answer that's
true about your latest commit but not about the PR as a whole (e.g. "just
reverted the previous commit") can legitimately fail, because that's not
what the grader was shown. Answer the question in front of you, not your
git log.

**`GET /verdict?repo=...&pr_number=...&head_sha=...`** → `{"status": ...}`,
one of four values the Action treats differently: `UNKNOWN` (no session for
this SHA — create one), `PENDING` (exists, unanswered), `PASSED` (exit
zero), `FAILED` (blocked). `UNKNOWN` and `PENDING` are deliberately distinct
— a session that exists for a *different* SHA on the same PR (e.g. after a
force-push) returns `UNKNOWN`, not `PENDING`, so the Action creates a fresh
session instead of blocking forever on one that no longer applies.

Question generation is a `FixedQuestionGenerator` behind a `QuestionGenerator`
protocol (`app/questions.py`) — same string every call, no model, no HTTP
client. Phase 4 kept it fixed on purpose ("no question generation in this
phase") but changed the actual text to `"What does this change do, and
what breaks if it's wrong?"`, since that's now the question the real
grader's prompts are written against and the evals compare against.

## Answer page (phase 3)

**`GET /s/{token}`** and **`POST /s/{token}/answer`** — unauthenticated;
the token in the URL is the credential. Server-rendered Jinja2, no
JavaScript. Shows the repo/PR, the diff (escaped, added/removed lines
coloured, nothing fancier), the question, and a textarea. Submitting
grades synchronously, records the answer, and redirects back to the GET
(post/redirect/get, so a refresh can't resubmit). An unknown token is a
generic `404`; an expired-but-still-pending session is `410`; answering an
already-decided session is `409`; an empty/whitespace answer, or one over
`max_answer_bytes` (default 20,000 bytes — override via `MAX_ANSWER_BYTES`),
re-renders the form with an inline error and writes nothing.

Two things this phase deliberately leaves open rather than deciding:
- **Retries**: right now one answer is final — a failed session can't be
  retried. Whether that should change (and how many attempts, if so) is a
  genuinely open product decision.
- **Authorship**: the token alone is the credential — anyone with the URL
  can answer, not just the PR author. No OAuth or identity check exists.

## Grading (phase 4)

Grading is a `Grader` protocol (`app/grading.py`) with two implementations:

- **`FakeGrader`** (`FAKE_GRADER=true`) — passes only if the answer
  contains the literal string `looks-good`. Fast and free; this is how
  phase 3's tests stay that way. Still selectable and still used by
  `uv run pytest` — nothing here removed it.
- **`RealGrader`** (`FAKE_GRADER=false`, `MODEL_API_KEY` set) — two
  sequential calls to the Anthropic Messages API (`claude-opus-5`) over
  async httpx, never the `anthropic` SDK or a sync client:
  1. **Interpretation** — the diff only, no answer. Output is a short list
     of discrete claims about what the change does.
  2. **Comparison** — the interpretation plus the developer's answer.
     Output is `{"passed": bool, "reasoning": str}`, requested via
     Structured Outputs (`output_config.format`) and *also* defensively
     parsed (fences stripped, first `{`…last `}` extracted, required
     fields/types checked) — belt-and-suspenders, since the spec requires
     defensive parsing regardless of how reliable structured outputs is.

  The split matters: a single call that sees the answer alongside the diff
  rationalises toward the answer and produces confident false passes.
  Generating the interpretation blind is what keeps the comparison honest.

  Grading rule: contradictions outweigh missing coverage. An answer that
  states something the diff doesn't do fails. Correct-but-incomplete
  passes unless the omission is the point of the change. Terse, poorly
  written, or non-native-English phrasing is fine — wrong content is not.

  A malformed or refused model response raises `GradingError`, which the
  route turns into a `502` with an inline "please resubmit" error on the
  answer form — no answers row is written, the session stays `pending`.
  It must never silently pass or fail.

  Every verdict from `RealGrader` records `model` and `prompt_version` on
  the `answers` row (`migrations/V2__answers_grading_metadata.sql`) —
  without that, there's no way to tell later whether a grading change
  helped. `FakeGrader`-produced rows leave both `NULL`.

  `reasoning` is likewise recorded (`migrations/V3__answers_reasoning.sql`)
  and shown on the result page under the verdict — both graders have
  always produced it on `GradeResult`, but nothing persisted or displayed
  it before this, so a `FAILED` developer had no way to see why.

  **Latency**: two sequential real model calls run synchronously inside
  the `POST /s/{token}/answer` request — no queue, per the spec. `claude-opus-5`
  runs adaptive thinking on by default, and this doesn't override that, so
  a real submission can take a real amount of wall-clock time (plausibly
  10s of seconds) before the redirect. Worth watching in production; not
  addressed in this phase since the spec's given scope doesn't ask for a
  UI loading state or a different model/effort tradeoff.

### Evals

`evals/cases/` holds five required cases (correct+complete, correct+terse,
correct+poor-English, diff-paraphrase, plausible-but-wrong) run via
`make eval` against the real grader — see `evals/README.md`. Model calls
are cached to `evals/cassettes/` keyed on a hash of `(diff, question,
answer)` and replayed on later runs; delete a cassette to re-record.

## Migrations

`migrations/*.sql` files are applied on startup by a small home-rolled
runner (`app/migrations.py`), tracked in a `schema_migrations` table — no
Alembic. The whole run is wrapped in a fixed Postgres advisory lock, so
that multiple replicas (or just two overlapping `docker compose up` runs)
booting at the same time serialize instead of racing to apply the same
migration twice.

## Tests

All integration tests share one Testcontainers-backed Postgres 16 (see
`tests/conftest.py`). `test_health.py` covers `/healthz`; `test_auth.py`,
`test_sessions.py`, `test_verdict.py`, and `test_idempotency.py` cover
phase 2; `test_web.py` covers phase 3 (full pass/fail loop, expiry, unknown
token, double submission, empty answer, and HTML-escaping of the diff);
`test_grading.py` covers `RealGrader`'s defensive JSON parsing and error
handling against a mocked HTTP transport — no real API key, no network,
no cost. It proves the *code* is correct; it does not prove the *prompts*
grade well — that's what `make eval`'s five real cases are for.
The idempotency test fires two `POST /sessions` calls through
`asyncio.gather` against a directly-driven app lifespan (not
`TestClient`, which is sync/thread-backed and can't prove two requests
genuinely overlapped) — sequentially firing them would pass even against a
racy select-then-insert implementation and prove nothing.

Two Docker Desktop quirks on macOS are worked around in `tests/conftest.py`
(gated to macOS, or an explicit env override — never unconditional, so CI
doesn't leak containers):
- the Ryuk reaper container is disabled, since Docker Desktop's non-standard
  per-user socket path breaks its connection back to the daemon;
- `DOCKER_HOST` is set explicitly to that socket path if not already set in
  the environment, since the same non-standard path can also break
  Testcontainers' initial connection, not just Ryuk's.

## Known cost: the diff is stored in Postgres

`sessions.diff` persists the full unified diff. This service has no GitHub
credentials in this architecture (the GitHub Action ships the diff to it
directly), and the future answer UI has to render the diff from somewhere —
so there's no clean way to avoid storing it. Self-hosters should treat this
table as containing source code and scope access accordingly.

## Before you deploy publicly

grumpy is bearer-gated on `/sessions`/`/verdict`, but the answer routes
(`/s/{token}`, `/s/{token}/answer`) are fully unauthenticated by design —
the token in the URL is the only gate. A checklist for a first-time
self-hoster, pulling together everything that matters once
`GRUMPY_BASE_URL` is genuinely public, rather than leaving it scattered
through the sections above:

- **Front grumpy with a reverse proxy or CDN that does rate limiting and
  request body-size limits** (nginx `limit_req`/`client_max_body_size`,
  Caddy, Cloudflare, etc.). Nothing in grumpy itself rate-limits any
  endpoint — an in-process limiter would be a false sense of security the
  moment you run more than one replica, so this isn't optional once the
  base URL is public.
- **Redact or disable full-path logging for `/s/{token}` in your proxy's
  access logs.** The token in that path *is* the credential — a default
  nginx/Caddy access-log line writes a bearer-equivalent secret to disk.
- **Set spend limits/alerts on your Anthropic API key.** `RealGrader` makes
  2 synchronous `claude-opus-5` calls per submitted answer with no
  per-deployment budget or alerting built in — a leaked `GRUMPY_TOKEN`, or
  just a burst of legitimate PR activity, translates directly into API
  spend.
- **Treat `GRUMPY_TOKEN` as a real secret and consider setting
  `GRUMPY_ALLOWED_REPOS`** — see "Self-host isolation model" above.
- **Anyone with a session URL can answer it.** There's no author-identity
  check tying an answer to the PR author — see "Answer page" above.
- **Diffs are stored in Postgres in plaintext** — see "Known cost" above.
  Scope database access accordingly.
- **The grader is prompt-injection-attackable, in principle.** Both the
  diff and the developer's answer are attacker-influenceable text fed to
  Claude. The two-call blind-interpretation design (see "Grading" above)
  blunts the obvious "ignore previous instructions" cases, but this is
  inherent to an LLM-graded merge gate, not something v1 claims to have
  fully solved.

grumpy itself, out of the box, now also: disables the interactive API docs
(`/docs`, `/redoc`, `/openapi.json` all `404`); sends `Referrer-Policy`,
`X-Content-Type-Options`, `X-Frame-Options`, and a `Content-Security-Policy`
on every response; and rejects request bodies over `max_request_body_bytes`
(checked against actual bytes received, not just a client-supplied
`Content-Length`) before they're buffered into memory, including a
dedicated length cap on the answer field itself — see "API" and "Answer
page" above for the specifics and env vars.

## Not in phase 4

No question generation (the question stays fixed), no repo context beyond
the diff, no multi-turn follow-up, no confidence scores or partial credit,
no diff chunking, no retry flow, no author-identity check, no queue, no
second process, no second datastore.

