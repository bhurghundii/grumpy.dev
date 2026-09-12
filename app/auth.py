"""Bearer-token auth for /sessions and /verdict. Not applied to /healthz —
container health checks and load balancers must reach it unauthenticated.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Header, HTTPException, Request

_UNAUTHORIZED = HTTPException(status_code=401, detail="unauthorized")


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


async def require_bearer_token(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Missing header, malformed header, and wrong token all raise the same
    401 — the caller must not be able to distinguish which one happened.
    """
    settings = request.app.state.settings
    provided = _extract_bearer_token(authorization)
    if provided is None or not secrets.compare_digest(provided, settings.grumpy_token):
        raise _UNAUTHORIZED
