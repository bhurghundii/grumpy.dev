"""Pure-ASGI middleware for the public v1 release.

Not `@app.middleware("http")`/`BaseHTTPMiddleware` — Starlette buffers the
body before that style ever sees it, which is exactly the gap this closes
(see audit.md's "unbounded request bodies" finding). This has to intercept
`receive()` itself, at the raw ASGI level, before Starlette/Pydantic ever
turns the body into a Python object.
"""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class _BodyTooLarge(Exception):
    pass


class MaxBodySizeMiddleware:
    """Rejects a request body over `settings.max_request_body_bytes` with a
    413, before the body is buffered into memory downstream.

    The limit is read from `scope["app"].state.settings` at request time,
    not at construction time: `Settings` is deliberately built lazily
    inside the FastAPI lifespan handler (see app/config.py), not at import
    time, and this middleware is wired in before that ever runs.

    Two layers, since a client can't be trusted to be honest about
    Content-Length:
      1. If Content-Length is present and already over the limit, reject
         immediately without reading anything off the wire.
      2. Otherwise (or if the client lies and sends more than it declared),
         count bytes as they actually arrive via `receive()` and abort as
         soon as the running total crosses the limit — this is what
         actually bounds memory, since Content-Length is a client-supplied
         claim, not a guarantee.

    Must be the innermost middleware in the stack (wired in via
    `app.add_middleware()` before any `@app.middleware("http")` handlers
    are declared) — see the comment in app/main.py next to where this is
    added for why.
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
