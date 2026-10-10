"""Auth: missing token, malformed header, wrong token, correct token — all
three failure modes must return 401 with an identical body.

Exercised against GET /verdict rather than POST /sessions: it takes no
request body, so these tests only ever vary the Authorization header,
without any ambiguity about body-validation-vs-auth-dependency ordering.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _query() -> dict:
    return {"repo": "octo/repo", "pr_number": 1, "head_sha": "a" * 40}


def test_missing_token_returns_401(grumpy_env) -> None:
    with TestClient(app) as client:
        response = client.get("/verdict", params=_query())
    assert response.status_code == 401


def test_malformed_header_returns_401(grumpy_env) -> None:
    with TestClient(app) as client:
        response = client.get(
            "/verdict", params=_query(), headers={"Authorization": "Basic abc123"}
        )
    assert response.status_code == 401


def test_wrong_token_returns_401(grumpy_env) -> None:
    with TestClient(app) as client:
        response = client.get(
            "/verdict", params=_query(), headers={"Authorization": "Bearer wrong-token"}
        )
    assert response.status_code == 401


def test_correct_token_is_accepted(grumpy_env) -> None:
    with TestClient(app) as client:
        response = client.get(
            "/verdict",
            params=_query(),
            headers={"Authorization": f"Bearer {grumpy_env.token}"},
        )
    assert response.status_code == 200
    assert response.json() == {"status": "UNKNOWN"}


def test_all_401_responses_have_the_same_body(grumpy_env) -> None:
    with TestClient(app) as client:
        missing = client.get("/verdict", params=_query())
        malformed = client.get(
            "/verdict", params=_query(), headers={"Authorization": "Token abc"}
        )
        wrong = client.get(
            "/verdict", params=_query(), headers={"Authorization": "Bearer nope"}
        )

    assert missing.status_code == malformed.status_code == wrong.status_code == 401
    assert missing.json() == malformed.json() == wrong.json()
