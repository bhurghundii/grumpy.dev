"""grumpy — FastAPI application, phase 4.

/healthz (unauthenticated), POST /sessions + GET /verdict (bearer auth),
GET /s/{token} + POST /s/{token}/answer (unauthenticated — the token is
the credential). Grading is FakeGrader (FAKE_GRADER=true) or RealGrader,
a two-call Anthropic Messages API grader (FAKE_GRADER=false).
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.auth import require_bearer_token
from app.commit_status import GitHubStatusPublisher, NullStatusPublisher
from app.config import Settings, get_settings
from app.db import create_pool
from app.grading import FakeGrader, RealGrader
from app.logging_config import configure_logging, redact_session_token
from app.middleware import MaxBodySizeMiddleware
from app.migrations import run_migrations
from app.questions import FixedQuestionGenerator
from app.schemas import CreateSessionRequest
from app.sessions import build_session_url, create_or_get_session
from app.verdict import fetch_verdict
from app.web import router as web_router

request_logger = logging.getLogger("grumpy.request")
startup_logger = logging.getLogger("grumpy.startup")


def _require_allowed_repo(settings: Settings, repo: str) -> None:
    """Shared by POST /sessions and GET /verdict — both must enforce the
    (optional) repo allow-list identically, so a disallowed repo can't be
    probed via /verdict even without permission to create a session for it.
    """
    if not settings.is_repo_allowed(repo):
        raise HTTPException(
            status_code=403,
            detail=f"repo '{repo}' is not permitted on this deployment",
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    app.state.settings = settings

    startup_logger.info("running migrations")
    applied = await run_migrations(settings.database_url, settings.migrations_dir)
    startup_logger.info(
        "migrations complete", extra={"outcome": "ok", "path": ",".join(applied) or "none pending"}
    )

    pool = await create_pool(
        settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    app.state.pool = pool
    app.state.question_generator = FixedQuestionGenerator()

    if settings.fake_grader:
        app.state.grader = FakeGrader()
    else:
        app.state.grader = RealGrader(
            api_key=settings.model_api_key, meaniemode=settings.meaniemode
        )

    if settings.github_status_token:
        app.state.status_publisher = GitHubStatusPublisher(
            settings.github_status_token, api_url=settings.github_api_url
        )
    else:
        app.state.status_publisher = NullStatusPublisher()

    startup_logger.info("startup complete", extra={"outcome": "ok"})

    try:
        yield
    finally:
        await pool.close()


# docs_url/redoc_url/openapi_url disabled: FastAPI serves these
# unauthenticated by default, on the same public GRUMPY_BASE_URL handed to
# Actions and PR authors. They don't leak secrets, but they hand any
# visitor a browsable, executable reference to every request/response
# shape — not something a public v1 should expose by default.
app = FastAPI(title="grumpy", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(web_router)
# Holds one file, app/static/nopaste.js (the paste guard on the answer and
# explain-back textareas). Served same-origin so the CSP below can allow it
# with `script-src 'self'` and nothing looser.
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")

# Must be added before the @app.middleware("http") functions below are
# declared. Starlette's middleware stack nests in registration order: the
# first add_middleware() call ends up innermost (right next to the
# router), each later one wraps further out. MaxBodySizeMiddleware raises
# internally and resolves that into a response itself — it must sit
# innermost so that exception never has to cross a BaseHTTPMiddleware
# boundary (log_requests/add_security_headers below are both
# BaseHTTPMiddleware-based via the decorator, which is unreliable about
# exceptions raised from receive()). With this order, both of those still
# see and act on the resulting response normally, including a 413.
app.add_middleware(MaxBodySizeMiddleware)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    outcome = "ok"
    try:
        response: Response = await call_next(request)
        if response.status_code >= 500:
            outcome = "error"
        return response
    except Exception:
        outcome = "error"
        raise
    finally:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        head_sha = getattr(request.state, "head_sha", None)
        request_logger.info(
            "request completed",
            extra={
                "repo": getattr(request.state, "repo", None),
                "pr_number": getattr(request.state, "pr_number", None),
                "head_sha": head_sha[:7] if head_sha else None,
                "outcome": outcome,
                "duration_ms": duration_ms,
                "method": request.method,
                # Never request.url.path raw: for /s/{token} routes that
                # segment is the credential. See redact_session_token.
                "path": redact_session_token(request.url.path),
            },
        )


# Cheap defense-in-depth, concretely relevant here (not just generic
# hardening): /s/{token}'s path segment *is* the credential — the classic
# bearer-token-in-a-URL pattern sensitive to leaking via Referer.
#
# `script-src 'self'` exists for exactly one file, app/static/nopaste.js.
# Same-origin files only, never 'unsafe-inline': an inline <script> or on*
# handler smuggled into rendered markdown still doesn't run (see
# app/rendering.py). `nosniff` keeps 'self' from stretching to grumpy's
# HTML or JSON responses — browsers won't execute those as script.
#
# No `img-src`: it existed only to permit templates/result.html's hotlinked
# third-party reward image, which is now an inline SVG. With that gone,
# `default-src 'none'` covers images too (and `connect-src`, so the paste
# guard can't phone home either), so the pages cannot make any outbound
# request at all — nothing to leak a session URL to. `style-src` stays for
# the <style> block each template carries inline.
_SECURITY_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'",
}


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


@app.get("/healthz")
async def healthz() -> JSONResponse:
    pool = app.state.pool
    try:
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")
    except Exception:
        request_logger.exception("healthz db check failed")
        return JSONResponse(status_code=503, content={"status": "error"})
    return JSONResponse(status_code=200, content={"status": "ok"})


@app.post("/sessions", dependencies=[Depends(require_bearer_token)])
async def create_session(payload: CreateSessionRequest, request: Request) -> JSONResponse:
    request.state.repo = payload.repo
    request.state.pr_number = payload.pr_number
    request.state.head_sha = payload.head_sha

    settings = app.state.settings
    _require_allowed_repo(settings, payload.repo)

    diff_bytes = len(payload.diff.encode("utf-8"))
    if diff_bytes > settings.max_diff_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"diff exceeds maximum size of {settings.max_diff_bytes} bytes",
        )

    question = await app.state.question_generator.generate(payload.diff)
    token = secrets.token_urlsafe(32)

    row, created = await create_or_get_session(
        app.state.pool,
        repo=payload.repo,
        pr_number=payload.pr_number,
        head_sha=payload.head_sha,
        base_sha=payload.base_sha,
        diff=payload.diff,
        question=question,
        token=token,
        ttl_days=settings.session_ttl_days,
    )

    session_url = build_session_url(settings.grumpy_base_url, row["token"])

    # On every call, not only the one that created the session: re-running
    # the workflow is how a status lost to a GitHub hiccup gets re-posted,
    # and on a session that's already decided it re-posts that verdict.
    await app.state.status_publisher.publish(
        repo=payload.repo,
        head_sha=payload.head_sha,
        status=row["status"],
        target_url=session_url,
    )
    body = {
        "session_url": session_url,
        "status": row["status"],
        "question": row["question"],
    }
    return JSONResponse(status_code=201 if created else 200, content=body)


@app.get("/verdict", dependencies=[Depends(require_bearer_token)])
async def get_verdict(
    request: Request,
    repo: str = Query(...),
    pr_number: int = Query(..., gt=0),
    head_sha: str = Query(...),
) -> dict:
    request.state.repo = repo
    request.state.pr_number = pr_number
    request.state.head_sha = head_sha

    settings = app.state.settings
    _require_allowed_repo(settings, repo)

    status_value = await fetch_verdict(
        app.state.pool, repo=repo, pr_number=pr_number, head_sha=head_sha
    )
    return {"status": status_value}
