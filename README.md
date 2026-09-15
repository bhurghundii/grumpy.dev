# grumpy - the AI that grills your devs

**A merge gate that checks the developer understands the change they are putting in.**

grumpy sits in your PR pipeline as a GitHub Action. Before a PR can merge, grumpy asks whoever opened it one question about their own diff — *"What does this change do, and what breaks if it's wrong?"* — and blocks the merge until they answer it well enough to convince an LLM grader. 

## Why should you care?

More and more PRs are AI-generated or AI-assisted, and "LGTM, CI is green" is no longer good enough evidence that the person clicking merge knows what they're shipping. For business critical software, this is unacceptable.

- **YAGNI the SaaS** grumpy is self-hosted only - one Docker Compose command and it's running.
- **No GitHub App, no OAuth, no webhooks.** It's a small FastAPI service that a GitHub Action talks to over a bearer token. Nothing to install on the GitHub side beyond a workflow file. Just keep the token a secret.
- **Real grading, not a keyword check.** Answers are graded by Claude against a blind interpretation of the diff, so it can't be gamed by echoing the question back.
- **Tutorials to help you understand the code** - Submitted a 3,000 line monstrosity? Grumpy will break it down to explain how it works. (Dev note: I learned about Firebase Security Rules changes this way that I would've totally missed so really needed this)

## Need a demo? 

Check out the PRs and see it in action 

### Isn't it ironic the solution to cognitive debt caused by vibe coding is more vibe slop? 

Yeah but I got a business to run so I am trying my best here. 

## How it works

1. A PR is opened or updated. Your workflow runs `git diff base...head` — three dots, so the diff is the PR's own changes measured from the merge base, not a two-dot comparison that would also include the reverse of anything landed on the base branch since — and `POST`s it to your grumpy instance.
2. grumpy generates a session, stores the diff, and returns a URL with a one-time token — `https://your-grumpy/s/<token>`.
3. The PR author opens the link, reads their own diff, and answers the question in a plain textarea. No login required — the token in the URL is the credential.
4. grumpy grades the answer with Claude: one call to interpret the diff blind (no answer shown), one call to compare that interpretation against what the developer wrote. Contradicting the diff fails; being terse or incomplete-but-correct passes.
5. Your workflow polls `GET /verdict` and exits `0` (pass) or `1` (fail), the same as any other required check.

A wrong answer doesn't end the session — the developer sees why they were wrong and gets another attempt, up to a configurable limit. See [Configuration](#configuration) for retries, an optional guided-tutorial mode, and a "meanie mode" that makes the failure reasoning much less polite.

## Quickstart (self-host it)

Requires Docker and Docker Compose.

```sh
git clone https://github.com/<your-fork>/grumpy.git
cd grumpy
cp .env.example .env
# edit .env: set GRUMPY_TOKEN to a real secret (see below) and GRUMPY_BASE_URL
docker compose up --build
curl localhost:8000/healthz
# {"status": "ok"}
```

Generate a real token rather than hand-typing one:

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

By default grumpy runs with `FAKE_GRADER=true`, which passes any answer containing the literal string `looks-good` — enough to see the whole flow end to end with zero API cost. To grade for real, set `FAKE_GRADER=false` and `MODEL_API_KEY=<your Anthropic API key>` in `.env`.

Try the answer page without wiring up a GitHub Action at all:

```sh
make seed   # inserts a realistic session directly into Postgres, prints its URL
```

Open the printed URL, answer the question, and watch the verdict resolve.

## Deploying to Railway

The reference deployment of grumpy runs on [Railway](https://railway.com). Nothing in grumpy depends on Railway — any host that can run the Dockerfile next to a Postgres 16 database works - I just personally use Railway cause it was easy:

1. **Create the project from your fork.** In Railway, *New Project → Deploy from GitHub repo* and pick your fork. Railway detects the `Dockerfile` and builds from it; no `railway.toml` is needed.
2. **Add Postgres.** In the same project, *+ New → Database → PostgreSQL*. Migrations apply automatically when the app starts, so there's nothing to run by hand.
3. **Set the app service's variables** (*Variables* tab):

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` (a reference to the Postgres service; adjust the name if you renamed it) |
   | `GRUMPY_TOKEN` | output of `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
   | `FAKE_GRADER` | `true` to try it out, `false` for real grading |
   | `MODEL_API_KEY` | your Anthropic API key (only needed when `FAKE_GRADER=false`) |
   | `PORT` | `8000` — the container always listens on 8000, so tell Railway to route there |
   | `GRUMPY_BASE_URL` | filled in after the next step |

4. **Give it a public URL.** *Settings → Networking → Generate Domain*, with target port `8000`. Then set `GRUMPY_BASE_URL` to that domain including the scheme (e.g. `https://your-app.up.railway.app`) and let it redeploy. grumpy won't boot without it.
5. **Point Railway's healthcheck at `/healthz`** (*Settings → Deploy → Healthcheck Path*), so a deploy that can't reach Postgres or fails config validation never takes traffic.
6. **Check it's up:** `curl https://your-app.up.railway.app/healthz` should return `{"status": "ok"}`.
7. **Wire up the repo** as below, using that same URL for the `GRUMPY_BASE_URL` secret and the same token for `GRUMPY_TOKEN`.

The `Dockerfile` intentionally skips a BuildKit cache mount because Railway's builder requires one scoped to its own internal service ID (see the comment in the file). A Railway domain is still on the public internet, so the [before you make it public](#self-hosting-before-you-make-it-public) checklist applies here too.

## Configuration

All config is environment variables, validated at startup — grumpy refuses to boot with a missing required var or an insecure `GRUMPY_TOKEN`, rather than failing confusingly later. See [`.env.example`](.env.example) for the full annotated list; the ones you'll actually touch:

| Variable | Required | Default | What it does |
|---|---|---|---|
| `DATABASE_URL` | yes | — | Postgres connection string |
| `GRUMPY_BASE_URL` | yes | — | Public URL used to build session links. Never derived from request headers, so it works correctly behind a proxy |
| `GRUMPY_TOKEN` | yes | — | Shared bearer secret gating `POST /sessions` and `GET /verdict`. Rejected at startup if it's the placeholder or under 20 characters |
| `FAKE_GRADER` | yes | — | `true` = free/instant grading via a `looks-good` marker string (good for trying grumpy out); `false` = real grading via Claude |
| `MODEL_API_KEY` | only if `FAKE_GRADER=false` | — | Anthropic API key used by the real grader |
| `GRUMPY_ALLOWED_REPOS` | no | unset (any repo) | Comma-separated `owner/name` allow-list. Defense-in-depth if `GRUMPY_TOKEN` ever leaks |
| `MAX_SESSION_ATTEMPTS` | no | `3` | Graded answers + tutorial requests allowed per session before it locks in as failed. `0` = unlimited retries |
| `ENABLE_TUTORIAL` | no | `false` | Offers a step-by-step, non-graded walkthrough of the diff, with a light comprehension check. Draws on the same `MAX_SESSION_ATTEMPTS` budget as an answer, so the last remaining attempt is reserved for answering and the offer is withdrawn at that point |
| `MEANIEMODE` | no | `false` | Failure explanations become sarcastic and merciless instead of professional. Doesn't change pass/fail, only tone |
| `MAX_DIFF_BYTES` | no | `400000` | Larger diffs are rejected with `413`. Bounded by the model's context window, not by Postgres: at ~3–4 bytes per token, 400 KB is ~100k–130k tokens. Raise it much further and you accept diffs that can never be graded |

## Self-hosting: before you make it public

grumpy's `/sessions` and `/verdict` endpoints are bearer-gated, but the answer page (`/s/{token}`) is intentionally open — the token in the URL is the only credential, so anyone with the link can answer it. Before pointing a real `GRUMPY_BASE_URL` at the public internet:

- **Put a reverse proxy or CDN in front of it that rate-limits and caps request body size.** grumpy has no built-in rate limiting — an in-process limiter would be false security the moment you run more than one replica.
- **Redact `/s/{token}` from your proxy's access logs.** That path *is* a bearer-equivalent secret. grumpy keeps it out of its own structured logs and disables uvicorn's access log for this reason, but a default nginx/Caddy line in front of it will happily write the token to disk.
- **Set spend limits on your Anthropic API key.** Real grading is two synchronous Claude calls per submitted answer, with no built-in per-deployment budget.
- **Treat `GRUMPY_TOKEN` as a real secret**, and set `GRUMPY_ALLOWED_REPOS` if you want a leaked token to not be usable against arbitrary repos.
- **Diffs are stored in Postgres in plaintext** — scope database access like it holds source code, because it does.

grumpy does ship a few defaults out of the box: interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are disabled; `Referrer-Policy`, `X-Content-Type-Options`, `X-Frame-Options` and a `Content-Security-Policy` of `default-src 'none'; style-src 'unsafe-inline'; script-src 'self'` are sent on every response (the only script allowed is grumpy's own same-origin paste guard — no inline script, no `img-src`, no `connect-src` — so the pages make no outbound requests at all, and there is nothing a session URL can leak to); request bodies are capped against bytes actually received rather than a client-supplied `Content-Length`; transient model-API failures are retried with backoff instead of surfacing to the developer; and session tokens are kept out of the logs.

## Wire it into a repo

(This is the part where you just shove the instructions into an LLM) 

Add a workflow that calls grumpy on every PR and blocks merge on the result. Minimal shape:

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened]

jobs:
  grumpy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Ask grumpy
        env:
          GRUMPY_BASE_URL: ${{ secrets.GRUMPY_BASE_URL }}
          GRUMPY_TOKEN: ${{ secrets.GRUMPY_TOKEN }}
        run: |
          # POST /sessions with the diff, then poll GET /verdict until
          # PASSED or FAILED. Full script with all the edge cases handled:
          # see INTEGRATION.md.
```

The full working workflow (session creation, polling loop, timeouts) is in [INTEGRATION.md](INTEGRATION.md), and the exact version this repo uses on itself is in [`.github/workflows/grumpy.yml`](.github/workflows/grumpy.yml). You'll need two repo/org secrets: `GRUMPY_BASE_URL` (your deployment's public URL) and `GRUMPY_TOKEN` (the same value the server is configured with).

## Local development

```sh
make dev    # starts Postgres via compose, runs the app locally with uv --reload
make test   # uv run pytest (spins up its own Postgres via Testcontainers)
make seed   # inserts a realistic session directly into Postgres, prints its URL
make eval   # runs evals/cases/ against the real grader (needs MODEL_API_KEY the first time)
make down   # docker compose down
```

> Postgres runs on host port **5433**, not 5432, to avoid colliding with a local Postgres you might already have running. Inside Docker Compose's own network, the app still talks to `db` on the normal 5432 — this only affects connecting from the host (e.g. `psql localhost:5433`).

Migrations (`migrations/*.sql`) apply automatically on startup via a small built-in runner — no Alembic, no manual step.

## Tech stack

Python, FastAPI, `uv`, psycopg3 (async, raw SQL, no ORM), Postgres 16, server-rendered Jinja2 templates. The only client-side JavaScript is a small paste guard on the answer box ([`app/static/nopaste.js`](app/static/nopaste.js)); every page works without it. Grading calls the Anthropic Messages API directly over async httpx. See [`pyproject.toml`](pyproject.toml) for exact versions.

## Limitations

The question grumpy asks is currently fixed — it doesn't generate a new question per diff. There's no author-identity check (the session URL alone is the credential), no confidence scores or partial credit, no multi-turn follow-up, and no queue — grading happens synchronously inside the answer submission. Grumpy is also, in principle, prompt-injection-attackable: both the diff and the developer's answer are attacker-influenceable text fed to an LLM. The blind-interpretation grading design (see `app/grading.py`) blunts the obvious cases but isn't a formal defense.

The answer box blocks pasting, but that's friction, not enforcement: anyone can retype text, turn JavaScript off, or POST the form directly. A submission made without the guard running is still graded normally — it's recorded (`answers.js_active` / `tutorials.explanation_js_active` = `false`) and logged with `outcome: no_js`, not failed.

## License

MIT — see [LICENSE](LICENSE). Security policy: [SECURITY.md](SECURITY.md). Release notes: [CHANGELOG.md](CHANGELOG.md).
