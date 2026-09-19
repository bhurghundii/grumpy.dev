# GitHub Actions Integration

This app is designed to be called from a GitHub Actions workflow as a self-hosted API service. It does not register a GitHub App and does not listen for repository webhooks. Its one outbound call to GitHub is optional: with `GITHUB_STATUS_TOKEN` set, it posts the verdict as a commit status.

## How it works

The app exposes two Action-facing endpoints:

- `POST /sessions` — create or reuse a review session for a PR
- `GET /verdict` — read whether the review passed or failed

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

With `GITHUB_STATUS_TOKEN` set on the server, grumpy then reports the verdict
on the PR itself, as a `grumpy/verdict` commit status on `head_sha`:

| When | State |
|---|---|
| every `POST /sessions` for that head SHA | `pending` (or the verdict, if the session already has one) |
| a graded answer passes | `success` |
| a graded answer fails with no attempts left | `failure` |

Its details link is the session URL. **Require `grumpy/verdict` in branch
protection, not the workflow's job.** A wrong answer with attempts left
changes nothing: the session and the status both stay pending.

`GET /verdict` returns the same outcome, for a workflow that gates on it
itself (see [Without a status token](#without-a-status-token)):

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

And on the server, `GITHUB_STATUS_TOKEN`: a GitHub token that can write
commit statuses on every repo this deployment gates. A fine-grained personal
access token with **Commit statuses: Read and write** on those repos is
enough; nothing else needs granting. If the status never appears, grumpy's
log says why: a `404` from GitHub means the token can't see the repo, a
`403` that it lacks the permission.

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
  statuses: read

jobs:
  ask:
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
          git diff "$BASE_SHA...$HEAD_SHA" > /tmp/grumpy.diff
          # ... POST /sessions with that diff, on every run (it's
          # idempotent). On a 201, output the session URL for the next step.

      - name: Comment session link
        if: steps.grumpy.outputs.session_url
        # ... comment the session URL on the PR

      - name: Check grumpy posted its commit status
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          # ... fail if there's no grumpy/verdict status on HEAD_SHA,
          # i.e. the server isn't configured to post one
```

The job itself finishes in seconds and goes red only when something is
broken; it is not the gate. `grumpy/verdict` is.

### Why the gate is a commit status, not the job

An Actions job can only end green or red, and what it would be waiting on is
a human answering on their own schedule. This workflow used to poll
`GET /verdict` for ten minutes and then fail on whatever it last saw. That
held a runner for the full ten minutes and left nearly every PR red, since
authors rarely answer that fast. (Worse, it only commented the session link
*after* the loop, so they couldn't have.)

A commit status can stay `pending` for as long as the answer takes, and
grumpy is the one party that knows the moment it resolves. So the job opens
the session and posts the link, and grumpy flips the status when the answer
is graded.

`POST /sessions` re-posts the session's current status on every call. If a
post to GitHub fails (an outage, a revoked token), grumpy logs it and carries
on, since the verdict is safe in Postgres either way. Re-running the job
brings GitHub back in line.

### Without a status token

Without `GITHUB_STATUS_TOKEN`, nothing posts `grumpy/verdict`, and the
reference workflow's last step fails with a message saying so. To gate
anyway, replace that step with one that reads `GET /verdict` once and fails
unless it's `PASSED`, and require the job instead. Post the session link
*before* that step, and expect to re-run the job after answering. Don't
poll inside the job for the answer: see above.

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

A skipped fork PR also never gets a `grumpy/verdict` status. If that's a
required check, merging one takes a maintainer bypassing the requirement.

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
GITHUB_STATUS_TOKEN=<token with Commit statuses: write>
```

### Public URL requirement

`GRUMPY_BASE_URL` must be public and reachable by whoever clicks the session URL. It must not be derived from the incoming request Host header.

### Repo matching

If `GRUMPY_ALLOWED_REPOS` is set, only repos in that allow-list can call the API.

### SHA requirement

The app requires full 40-character SHAs for both `head_sha` and `base_sha`.

## Deployment model

This app is meant to be run as a normal web service, usually behind HTTPS and a reverse proxy or container orchestration layer. The GitHub workflow simply invokes the API; grumpy reports the verdict back to GitHub itself.

grumpy has no built-in rate limiting — your reverse proxy or CDN must provide it, along with request body-size limits, before `GRUMPY_BASE_URL` is reachable from the public internet. See [README.md's "Self-hosting: before you make it public"](README.md#self-hosting-before-you-make-it-public) for the full pre-launch checklist (rate limiting, proxy access-log redaction for `/s/{token}`, Anthropic spend limits, and the accepted-risk items).

## Typical usage pattern

1. Run the server, with `GITHUB_STATUS_TOKEN` set
2. Set GitHub secrets
3. Add the workflow above to the repo, and require `grumpy/verdict` in branch protection
4. PRs automatically create review sessions, and `grumpy/verdict` shows pending
5. The PR author clicks the link and answers, and `grumpy/verdict` turns green or red

## Reference

- README: [README.md](README.md)
- Example environment: [.env.example](.env.example)
