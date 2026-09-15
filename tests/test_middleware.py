"""Public-v1 hardening: docs disabled, security response headers, and the
ASGI-level request body size cap (app/middleware.py).
"""

from __future__ import annotations

import secrets

from fastapi.testclient import TestClient

from app.main import app

_VALID_SHA = "1" * 40


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_docs_endpoints_are_disabled(grumpy_env) -> None:
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_security_headers_present_on_response(grumpy_env) -> None:
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]


def test_oversized_sessions_body_rejected_with_413(grumpy_env, monkeypatch) -> None:
    # Small enough to trip well before Pydantic ever sees valid JSON —
    # proves the raw body is rejected before it's parsed, not after.
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "500")
    monkeypatch.setenv("MAX_DIFF_BYTES", "200")
    monkeypatch.setenv("MAX_ANSWER_BYTES", "200")

    with TestClient(app) as client:
        response = client.post(
            "/sessions",
            content=b"x" * 2000,
            headers={**_headers(grumpy_env.token), "Content-Type": "application/json"},
        )

    assert response.status_code == 413
    assert "exceeds maximum size" in response.json()["detail"]


def test_oversized_answer_form_body_rejected_with_413(grumpy_env, monkeypatch) -> None:
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "500")
    monkeypatch.setenv("MAX_DIFF_BYTES", "200")
    monkeypatch.setenv("MAX_ANSWER_BYTES", "200")

    with TestClient(app) as client:
        response = client.post(
            "/s/" + secrets.token_urlsafe(32) + "/answer",
            data={"answer": "x" * 2000},
        )

    assert response.status_code == 413


def test_normal_request_within_limits_still_succeeds(grumpy_env) -> None:
    # Regression check: the middleware must not interfere with an
    # ordinary, well-under-the-cap request.
    repo = f"octo/mw-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        response = client.post(
            "/sessions",
            json={
                "repo": repo,
                "pr_number": 1,
                "head_sha": _VALID_SHA,
                "base_sha": "2" * 40,
                "diff": "diff --git a/x b/x\n+hello\n",
            },
            headers=_headers(grumpy_env.token),
        )

    assert response.status_code == 201


def test_csp_forbids_all_outbound_subresource_requests(grumpy_env) -> None:
    """`default-src 'none'` with no `img-src` or `connect-src` means a
    rendered page cannot fetch anything off-origin at all. That matters
    here specifically: /s/{token} URLs are credentials, and an outbound
    request from one of those pages is a way for that URL to reach a third
    party. The exceptions are `style-src`, for the <style> block each
    template carries inline, and `script-src 'self'`, for the same-origin
    paste guard (app/static/nopaste.js) — which must never grow
    'unsafe-inline', or injected markup could run script.

    img-src previously allowed `https:` solely to permit a hotlinked
    third-party reward image on the pass page, now an inline SVG.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        csp = client.get("/healthz").headers["content-security-policy"]

    assert csp == "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'"
    assert "img-src" not in csp
    assert "connect-src" not in csp
    script_src = next(d for d in csp.split(";") if d.strip().startswith("script-src"))
    assert "'unsafe-inline'" not in script_src
