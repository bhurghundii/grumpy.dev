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
  "status": "PENDING",
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

- `GRUMPY_URL` — public base URL of the deployed grumpy app, such as `https://grumpy.example.com`
- `GRUMPY_TOKEN` — the same value configured as `GRUMPY_TOKEN` on the server

## Example GitHub Actions workflow

```yaml
name: grumpy-review-gate

on:
  pull_request:
    types: [opened, synchronize, reopened, edited]

jobs:
  grumpy:
    runs-on: ubuntu-latest
    permissions:
      contents: read

    steps:
      - name: Check out code
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Create grumpy review session
        id: session
        env:
          GRUMPY_URL: ${{ secrets.GRUMPY_URL }}
          GRUMPY_TOKEN: ${{ secrets.GRUMPY_TOKEN }}
        run: |
          set -euo pipefail

          BASE_SHA="${{ github.event.pull_request.base.sha }}"
          HEAD_SHA="${{ github.event.pull_request.head.sha }}"
          REPO="${{ github.repository }}"
          PR_NUMBER="${{ github.event.pull_request.number }}"

          DIFF="$(git diff --binary "$BASE_SHA" "$HEAD_SHA")"

          PAYLOAD="$(jq -nc \
            --arg repo "$REPO" \
            --argjson pr_number "$PR_NUMBER" \
            --arg head_sha "$HEAD_SHA" \
            --arg base_sha "$BASE_SHA" \
            --arg diff "$DIFF" \
            '{repo:$repo, pr_number:$pr_number, head_sha:$head_sha, base_sha:$base_sha, diff:$diff}')"

          RESPONSE="$(curl -sS -X POST "$GRUMPY_URL/sessions" \
            -H "Authorization: Bearer $GRUMPY_TOKEN" \
            -H "Content-Type: application/json" \
            --data "$PAYLOAD")"

          echo "$RESPONSE"

          echo "SESSION_URL=$(echo "$RESPONSE" | jq -r '.session_url')" >> "$GITHUB_OUTPUT"
          echo "QUESTION=$(echo "$RESPONSE" | jq -r '.question')" >> "$GITHUB_OUTPUT"

      - name: Show review URL
        run: |
          echo "Grumpy review URL: ${{ steps.session.outputs.SESSION_URL }}"
          echo "Question: ${{ steps.session.outputs.QUESTION }}"

      - name: Wait for verdict
        env:
          GRUMPY_URL: ${{ secrets.GRUMPY_URL }}
          GRUMPY_TOKEN: ${{ secrets.GRUMPY_TOKEN }}
        run: |
          set -euo pipefail

          REPO="${{ github.repository }}"
          PR_NUMBER="${{ github.event.pull_request.number }}"
          HEAD_SHA="${{ github.event.pull_request.head.sha }}"

          for i in $(seq 1 60); do
            STATUS="$(curl -sS -G "$GRUMPY_URL/verdict" \
              --data-urlencode "repo=$REPO" \
              --data-urlencode "pr_number=$PR_NUMBER" \
              --data-urlencode "head_sha=$HEAD_SHA" \
              -H "Authorization: Bearer $GRUMPY_TOKEN" \
              | jq -r '.status')"

            echo "Current grumpy status: $STATUS"

            case "$STATUS" in
              PASSED)
                echo "Review passed."
                exit 0
                ;;
              FAILED)
                echo "Review failed."
                exit 1
                ;;
              UNKNOWN|PENDING)
                ;;
              *)
                echo "Unexpected status: $STATUS" >&2
                exit 1
                ;;
            esac

            sleep 10
          done

          echo "Timed out waiting for grumpy verdict" >&2
          exit 1
```

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

grumpy has no built-in rate limiting — your reverse proxy or CDN must provide it, along with request body-size limits, before `GRUMPY_BASE_URL` is reachable from the public internet. See [README.md's "Before you deploy publicly"](README.md#before-you-deploy-publicly) for the full pre-launch checklist (rate limiting, proxy access-log redaction for `/s/{token}`, Anthropic spend limits, and the accepted-risk items).

## Typical usage pattern

1. Run the server
2. Set GitHub secrets
3. Add the workflow above to the repo
4. PRs automatically create review sessions
5. Reviewers click the URL, answer the question, and the workflow gate resolves

## Reference

- README: [README.md](README.md)
- Example environment: [.env.example](.env.example)
