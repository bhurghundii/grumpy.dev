# Security policy

## Reporting a vulnerability

Please report security issues privately via GitHub's [private vulnerability
reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository (Security → Report a vulnerability), rather than opening a
public issue.

Please include what you were able to do, the deployment shape you tested
against (self-hosted, behind what proxy), and anything needed to reproduce.

## Scope and threat model

grumpy is designed to be self-hosted. Each deployment is isolated only by the
two values it controls — `GRUMPY_TOKEN` and `GRUMPY_BASE_URL`. There is no
GitHub App, OAuth, webhook, or tenancy model, and no shared credential is
baked into the codebase, so two deployments cannot cross-talk unless a token
is reused or leaked.

Note that anyone holding a deployment's `GRUMPY_TOKEN` can create sessions
and read verdicts for *any* `owner/name` string, not just the repos that
deployment is meant to serve. `GRUMPY_ALLOWED_REPOS` narrows that. With
`GITHUB_STATUS_TOKEN` set, the same holder can also get a `grumpy/verdict`
status onto a commit that has no session yet, in any repo that GitHub token
can write to, by creating a session for it and answering it. Scope the
GitHub token to the gated repos only.

### In scope

- Bypassing bearer auth on `POST /sessions` or `GET /verdict`
- Reading or altering another session's data without its token
- Injection (SQL, template, HTML/XSS) via a diff, an answer, or model output
- Bypassing `GRUMPY_ALLOWED_REPOS` when it is configured
- Causing unbounded Anthropic API spend against a configured
  `MAX_SESSION_ATTEMPTS`

### Known and accepted, by design

These are documented trade-offs rather than vulnerabilities. Reports about
them are welcome as design discussion, but they are already known:

- **Anyone with a session URL can answer it.** The token in `/s/{token}` is
  the only credential; there is no check that the answerer is the PR author.
- **The grader is prompt-injection-attackable in principle.** Both the diff
  and the answer are attacker-influenceable text fed to a model. The two-call
  blind-interpretation design blunts the obvious cases; it does not solve the
  general problem.
- **Diffs are stored in Postgres in plaintext.** Treat the database as
  containing source code.
- **Anyone with write access to a repo can set `grumpy/verdict` by hand.**
  A commit status isn't tied to who posts it, so a repo writer can post
  `success` under that context themselves. Under the old job-based gate, the
  same writer could edit the workflow in their own PR to skip it. The gate
  holds people to answering; it doesn't hold off people who own the repo.
- **grumpy does not rate-limit itself.** A reverse proxy or CDN in front of it
  must, before `GRUMPY_BASE_URL` is publicly reachable. See
  [README.md](README.md#self-hosting-before-you-make-it-public).

## Supported versions

grumpy is pre-1.0. Fixes land on `main`; there are no backported release
branches yet.
