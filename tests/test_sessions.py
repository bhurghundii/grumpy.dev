"""POST /sessions: creation shape, non-concurrent repeat behaviour,
one rejection test per validated field, and the oversized-diff 413.

(Genuine concurrent-request idempotency is tested separately in
test_idempotency.py — a sequential test here proves nothing about the race.)
"""

from __future__ import annotations

import secrets

from fastapi.testclient import TestClient

from app.main import app

_VALID_BODY = {
    "repo": "octo/repo",
    "pr_number": 10,
    "head_sha": "1" * 40,
    "base_sha": "2" * 40,
    "diff": "diff --git a/x b/x\n+hello\n",
}


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _unique_repo(label: str) -> str:
    return f"octo/{label}-{secrets.token_hex(4)}"


def test_create_session_returns_201_with_url_and_question(grumpy_env) -> None:
    body = {**_VALID_BODY, "repo": _unique_repo("create")}
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(grumpy_env.token))

    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "pending"
    assert data["question"]
    assert data["session_url"].startswith(f"{grumpy_env.base_url}/s/")


def test_repeated_create_returns_200_with_same_url(grumpy_env) -> None:
    body = {**_VALID_BODY, "repo": _unique_repo("repeat")}
    with TestClient(app) as client:
        first = client.post("/sessions", json=body, headers=_headers(grumpy_env.token))
        second = client.post("/sessions", json=body, headers=_headers(grumpy_env.token))

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["session_url"] == second.json()["session_url"]


def test_rejects_invalid_repo(grumpy_env) -> None:
    body = {**_VALID_BODY, "repo": "not-a-valid-repo", "head_sha": "3" * 40, "base_sha": "4" * 40}
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(grumpy_env.token))

    assert response.status_code == 422
    fields = {err["loc"][-1] for err in response.json()["detail"]}
    assert "repo" in fields


def test_rejects_non_positive_pr_number(grumpy_env) -> None:
    body = {
        **_VALID_BODY,
        "repo": _unique_repo("badpr"),
        "pr_number": 0,
        "head_sha": "5" * 40,
        "base_sha": "6" * 40,
    }
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(grumpy_env.token))

    assert response.status_code == 422
    fields = {err["loc"][-1] for err in response.json()["detail"]}
    assert "pr_number" in fields


def test_rejects_invalid_head_sha(grumpy_env) -> None:
    body = {**_VALID_BODY, "repo": _unique_repo("badhead"), "head_sha": "not-hex", "base_sha": "7" * 40}
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(grumpy_env.token))

    assert response.status_code == 422
    fields = {err["loc"][-1] for err in response.json()["detail"]}
    assert "head_sha" in fields


def test_rejects_invalid_base_sha(grumpy_env) -> None:
    body = {**_VALID_BODY, "repo": _unique_repo("badbase"), "head_sha": "8" * 40, "base_sha": "short"}
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(grumpy_env.token))

    assert response.status_code == 422
    fields = {err["loc"][-1] for err in response.json()["detail"]}
    assert "base_sha" in fields


def test_rejects_empty_diff(grumpy_env) -> None:
    body = {
        **_VALID_BODY,
        "repo": _unique_repo("emptydiff"),
        "head_sha": "9" * 40,
        "base_sha": "a1" * 20,
        "diff": "   ",
    }
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(grumpy_env.token))

    assert response.status_code == 422
    fields = {err["loc"][-1] for err in response.json()["detail"]}
    assert "diff" in fields


def test_rejects_oversized_diff_with_413(grumpy_env) -> None:
    body = {
        **_VALID_BODY,
        "repo": _unique_repo("bigdiff"),
        "head_sha": "b2" * 20,
        "base_sha": "c3" * 20,
        "diff": "x" * 1_000_001,  # default max_diff_bytes is 1_000_000
    }
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(grumpy_env.token))

    assert response.status_code == 413


def test_repo_allowlist_unset_permits_any_repo(grumpy_env) -> None:
    # GRUMPY_ALLOWED_REPOS is never set by the grumpy_env fixture — this is
    # an explicit regression test for that default (unrestricted) behavior,
    # not just an incidental side effect of the other tests in this file.
    body = {**_VALID_BODY, "repo": _unique_repo("unrestricted")}
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(grumpy_env.token))

    assert response.status_code == 201


def test_create_session_rejects_repo_not_in_allowlist(grumpy_env, monkeypatch) -> None:
    # Must be set before `with TestClient(app)`: get_settings() runs once,
    # inside the lifespan, at TestClient.__enter__().
    monkeypatch.setenv("GRUMPY_ALLOWED_REPOS", "octo/only-allowed")
    body = {**_VALID_BODY, "repo": _unique_repo("disallowed")}
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(grumpy_env.token))

    assert response.status_code == 403


def test_create_session_allows_repo_in_allowlist(grumpy_env, monkeypatch) -> None:
    monkeypatch.setenv("GRUMPY_ALLOWED_REPOS", "octo/only-allowed")
    body = {**_VALID_BODY, "repo": "octo/only-allowed"}
    with TestClient(app) as client:
        response = client.post("/sessions", json=body, headers=_headers(grumpy_env.token))

    assert response.status_code == 201
