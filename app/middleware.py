"""Pure-ASGI middleware. BaseHTTPMiddleware buffers the body before it sees it,
so size limits have to intercept `receive()` directly."""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class _BodyTooLarge(Exception):
    pass


class MaxBodySizeMiddleware:
    """Reject a body over `settings.max_request_body_bytes` with a 413 before it
        is buffered.

        The limit is read at request time because Settings is built in the
        lifespan handler. A Content-Length over the limit is rejected immediately;
        otherwise bytes are counted as they arrive, since Content-Length is only a
        claim. Must be the innermost middleware (see app/main.py).
        """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        max_bytes: int = scope["app"].state.settings.max_request_body_bytes

        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    break
                if declared > max_bytes:
                    await _reject(scope, receive, send, max_bytes)
                    return
                break

        total = 0

        async def limited_receive():
            nonlocal total
            message = await receive()
            total += len(message.get("body", b""))
            if total > max_bytes:
                raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await _reject(scope, receive, send, max_bytes)


async def _reject(scope: Scope, receive: Receive, send: Send, max_bytes: int) -> None:
    response = JSONResponse(
        status_code=413,
        content={"detail": f"request body exceeds maximum size of {max_bytes} bytes"},
    )
    await response(scope, receive, send)
