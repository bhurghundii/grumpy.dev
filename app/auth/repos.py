from __future__ import annotations

from fastapi import HTTPException

from app.config import Settings


def require_allowed_repo(settings: Settings, repo: str) -> None:
    if not settings.is_repo_allowed(repo):
        raise HTTPException(
            status_code=403,
            detail=f"repo '{repo}' is not permitted on this deployment",
        )
