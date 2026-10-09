"""Reports a session's verdict to GitHub as a `grumpy/verdict` commit status.

Unlike an Action job, which can only end green or red, a commit status can
stay pending for as long as the human takes to answer. Posted as `pending` on
POST /sessions and `success`/`failure` when an answer decides the session.
Optional: with GITHUB_STATUS_TOKEN unset, NullStatusPublisher posts nothing.

Never raises: the verdict is already in Postgres, so a GitHub failure is
logged rather than turned into a 500. Re-running the workflow reposts the
current status.
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
    """`client_factory` lets tests swap in an httpx.MockTransport."""

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
            # 404 usually means the token cannot see the repo; 403 that it lacks
            # "Commit statuses: write".
            logger.warning(
                "GitHub rejected the commit status",
                extra={**log_extra, "outcome": "error", "status_code": exc.response.status_code},
            )
        except Exception:  # timeouts, connection errors, anything unexpected
            logger.warning(
                "posting the commit status failed", extra={**log_extra, "outcome": "error"}, exc_info=True
            )
