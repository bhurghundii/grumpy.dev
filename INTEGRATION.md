# GitHub Actions Integration

This app is designed to be called from a GitHub Actions workflow as a self-hosted API service. It does not register a GitHub App and does not listen for repository webhooks.

## How it works

The app exposes two Action-facing endpoints:

- `POST /sessions` — create or reuse a review session for a PR
- `GET /verdict` — poll whether the review passed or failed

Both endpoints require a bearer token:

```http
Authorization: Bearer <GRUMPY_TOKEN>
```

The request body for `POST /sessions` is:

```json
{
  "repo": "owner/name",
  "pr_number": 123,
  "head_sha": "<40-char-sha>",
  "base_sha": "<40-char-sha>",
  "diff": "<git diff output>"
}
```

The response looks like:

```json
{
  "session_url": "https://grumpy.example.com/s/<token>",
  "status": "pending",
  "question": "What does this change do, and what breaks if it's wrong?"
}
```

Then the workflow polls:

```http
GET /verdict?repo=owner/name&pr_number=123&head_sha=<head-sha>
```

Status values are:

- `UNKNOWN` — no session exists yet
- `PENDING` — review is waiting for an answer
- `PASSED` — developer answer passed
- `FAILED` — developer answer failed

## Required GitHub secrets

In GitHub repository or organization secrets, set:

- `GRUMPY_BASE_URL` — public base URL of the deployed grumpy app, such as `https://grumpy.example.com`
- `GRUMPY_TOKEN` — the same value configured as `GRUMPY_TOKEN` on the server

## Example GitHub Actions workflow

The canonical, maintained copy of this workflow is
[.github/workflows/grumpy.yml](.github/workflows/grumpy.yml) — grumpy runs it
on its own PRs. Copy that file rather than the abridged version below, which
exists to show the shape at a glance. (An earlier revision of this document
carried its own full copy, which drifted: it named a `GRUMPY_URL` secret the
workflow never read.)

```yaml
name: grumpy-review-gate

on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  contents: read
  pull-requests: write

jobs:
  grumpy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Ask grumpy
        id: grumpy
        env:
          GRUMPY_BASE_URL: ${{ secrets.GRUMPY_BASE_URL }}
          GRUMPY_TOKEN: ${{ secrets.GRUMPY_TOKEN }}
          REPO: ${{ github.repository }}
          PR_NUMBER: ${{ github.event.pull_request.number }}
          HEAD_SHA: ${{ github.event.pull_request.head.sha }}
          BASE_SHA: ${{ github.event.pull_request.base.sha }}
        run: |
          set -euo pipefail

          verdict=$(curl -sf -H "Authorization: Bearer $GRUMPY_TOKEN" \
            --get --data-urlencode "repo=$REPO" \
            --data-urlencode "pr_number=$PR_NUMBER" \
            --data-urlencode "head_sha=$HEAD_SHA" \
            "$GRUMPY_BASE_URL/verdict" | jq -r .status)

          if [ "$verdict" = "UNKNOWN" ]; then
            git diff "$BASE_SHA...$HEAD_SHA" > /tmp/grumpy.diff
            # ... POST /sessions with that diff, then comment the session URL
            verdict="PENDING"
          fi

          echo "verdict=$verdict" >> "$GITHUB_OUTPUT"

      - name: Gate
        run: |
          [ "${{ steps.grumpy.outputs.verdict }}" = "PASSED" ] || exit 1
```

### Polling for the verdict

The shipped workflow polls `GET /verdict` while the status is `PENDING`, for
up to ten minutes (30 attempts, 20 seconds apart), then gates on whatever it
last saw.

This is a deliberate trade-off, and worth understanding before you change it.
Without the loop the verdict is captured once at job start, so a developer
who answers correctly while the job is running still sees a red check until
somebody notices the PR comment and manually re-runs it. Polling lets the
check resolve itself, which is the point of a merge gate.

The cost is runner time: every second spent waiting on a human is billed as
Actions minutes, and the job holds a runner for the duration. If that matters
more to you than self-resolving checks, drop the loop and gate on a single
reading — verdicts are durable once written, so a re-run picks them up.

Either way, do not raise the budget much past ten minutes. A developer may
take hours to answer, and no polling budget survives that.

### Fork pull requests

GitHub does not share repository secrets with workflow runs triggered by a
PR from a fork, and gives those runs a read-only token. Without a guard, the
bearer token interpolates to an empty string, the API returns 401, `curl -sf`
exits non-zero, and an outside contributor sees their PR blocked by an error
they cannot fix.

The shipped workflow detects this and skips the gate with an explanatory
notice. Do not reach for `pull_request_target` to "fix" it: that pairs your
secrets with a checkout of untrusted head code. If you want fork PRs gated,
run the gate after merging to a trusted branch.

### The diff range must use three dots

`git diff "$BASE_SHA...$HEAD_SHA"`, not `git diff "$BASE_SHA" "$HEAD_SHA"`.

`github.event.pull_request.base.sha` is the tip of the base branch when the
event fired, not the merge base. A two-dot diff between them therefore also
contains the *reverse* of every commit that landed on the base branch after
the PR was opened — code the author never touched, shown to them as their
own change and sent to the grader as a claim about this diff. Three dots
diffs from the merge base, which is what "the whole PR against its base"
means.

## Important configuration details

### Server env vars

The server must be configured with values matching the app requirements in [.env.example](.env.example):

```bash
DATABASE_URL=postgresql://...
GRUMPY_BASE_URL=https://grumpy.example.com
GRUMPY_TOKEN=<strong-secret>
FAKE_GRADER=false
MODEL_API_KEY=<anthropic-key>
```

### Public URL requirement

`GRUMPY_BASE_URL` must be public and reachable by whoever clicks the session URL. It must not be derived from the incoming request Host header.

### Repo matching

If `GRUMPY_ALLOWED_REPOS` is set, only repos in that allow-list can call the API.

### SHA requirement

The app requires full 40-character SHAs for both `head_sha` and `base_sha`.

## Deployment model

This app is meant to be run as a normal web service, usually behind HTTPS and a reverse proxy or container orchestration layer. The GitHub workflow simply invokes the API and blocks on the verdict.

grumpy has no built-in rate limiting — your reverse proxy or CDN must provide it, along with request body-size limits, before `GRUMPY_BASE_URL` is reachable from the public internet. See [README.md's "Self-hosting: before you make it public"](README.md#self-hosting-before-you-make-it-public) for the full pre-launch checklist (rate limiting, proxy access-log redaction for `/s/{token}`, Anthropic spend limits, and the accepted-risk items).

## Typical usage pattern

1. Run the server
2. Set GitHub secrets
3. Add the workflow above to the repo
4. PRs automatically create review sessions
5. Reviewers click the URL, answer the question, and the workflow gate resolves

## Reference

- README: [README.md](README.md)
- Example environment: [.env.example](.env.example)
