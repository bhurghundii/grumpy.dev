# Wire it into a repo

Add a workflow that opens a grumpy session on every PR. Its one job, `grill`, is non-blocking: it comments the question link and passes, and if grumpy is unreachable or rejects the request it warns instead of failing the PR. To make the answer gate merging, also require the `grumpy/verdict` status in branch protection; leave it out and grumpy is advisory. Minimal shape:

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened]

jobs:
  grill:
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
          # POST /sessions with the diff and comment the link on the PR.
          # On any error, emit a ::warning and exit 0 so the job never
          # blocks. grumpy posts the grumpy/verdict status itself. Full
          # script with all the edge cases handled: see the workflow
          # linked below.
```

The exact working workflow this repo uses on itself is [`.github/workflows/grumpy.yml`](https://github.com/bhurghundii/grumpy.dev/blob/main/.github/workflows/grumpy.yml). You'll need two repo/org secrets: `GRUMPY_BASE_URL` (your deployment's public URL) and `GRUMPY_TOKEN` (the same value the server is configured with), plus `GITHUB_STATUS_TOKEN` set on the server.
