"""Reports a session's verdict to GitHub as a `grumpy/verdict` commit
status on the PR's head commit.

Why a commit status rather than the Action job's own result: a job can
only end green or red, and what it would be waiting on is a human
answering on their own schedule. The workflow used to poll GET /verdict
for ten minutes and then fail on whatever it last saw, which held a runner
for ten minutes and left nearly every PR red. A commit status can sit at
`pending` for as long as the answer takes, and grumpy is the one party
that knows the moment it resolves — so grumpy posts it:

  - `pending` on every POST /sessions (see app/main.py), and
  - `success` / `failure` as soon as a graded answer ends the session
    (app/web.py:submit_answer).

Optional. With GITHUB_STATUS_TOKEN unset, NullStatusPublisher posts
nothing and an Action has to gate on GET /verdict itself, as before.

Never raises. The verdict is already durable in Postgres by the time this
runs; a GitHub outage or a mis-scoped token must not turn a graded answer
into a 500 for the developer, or fail the Action's POST /sessions. The
failure is logged instead, and the next POST /sessions for the same head
SHA — i.e. re-running the workflow — posts the session's current status
again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

import httpx

logger = logging.getLogger("grumpy.commit_status")

CONTEXT = "grumpy/verdict"

# sessions.status -> (GitHub status state, description). GitHub truncates
# descriptions past 140 characters.
_STATES = {
    "pending": ("pending", "Waiting for the PR author to answer grumpy's question"),
    "passed": ("success", "The PR author answered grumpy's question"),
    "failed": ("failure", "Out of attempts without a passing answer"),
}


class StatusPublisher(Protocol):
    async def publish(self, *, repo: str, head_sha: str, status: str, target_url: str) -> None: ...


class NullStatusPublisher:
    """GITHUB_STATUS_TOKEN unset: grumpy reports nothing to GitHub."""

    async def publish(self, *, repo: str, head_sha: str, status: str, target_url: str) -> None:
        return None


class GitHubStatusPublisher:
    """`client_factory` exists for the same reason as RealGrader's: tests
    swap in an httpx.MockTransport instead of calling the real API."""

    def __init__(
        self,
        token: str,
        *,
        api_url: str = "https://api.github.com",
        timeout: float = 10.0,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=timeout))

    async def publish(self, *, repo: str, head_sha: str, status: str, target_url: str) -> None:
        state, description = _STATES[status]
        log_extra = {"repo": repo, "head_sha": head_sha[:7]}
        try:
            async with self._client_factory() as client:
                response = await client.post(
                    f"{self._api_url}/repos/{repo}/statuses/{head_sha}",
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                    json={
                        "state": state,
                        "target_url": target_url,
                        "description": description,
                        "context": CONTEXT,
                    },
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # 404 here almost always means the token can't see the repo
            # (GitHub hides repos a token has no access to), 403 that it
            # can see it but lacks "Commit statuses: write".
            logger.warning(
                "GitHub rejected the commit status",
                extra={**log_extra, "outcome": "error", "status_code": exc.response.status_code},
            )
        except Exception:  # timeouts, connection errors, anything unexpected
            logger.warning(
                "posting the commit status failed", extra={**log_extra, "outcome": "error"}, exc_info=True
            )
