"""GET /verdict: all four states, asserted separately.

UNKNOWN and PENDING are deliberately not collapsed — a session on a
*different* head_sha for the same PR must return UNKNOWN, not PENDING,
since that's what tells the Action to create a fresh session after a
force-push rather than block forever on a session that no longer applies.
"""

from __future__ import annotations

import secrets

import psycopg
from fastapi.testclient import TestClient

from app.main import app


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_session(client: TestClient, token: str, *, repo: str, pr_number: int, head_sha: str):
    return client.post(
        "/sessions",
        json={
            "repo": repo,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "base_sha": "b" * 40,
            "diff": "diff --git a/x b/x\n+hello\n",
        },
        headers=_headers(token),
    )


def _set_status(database_url: str, *, repo: str, pr_number: int, head_sha: str, status: str) -> None:
    # Phase 2 has no grader yet — this simulates what phase 3's grader will
    # later do, so the verdict mapping can be tested independently of it.
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "UPDATE sessions SET status = %s WHERE repo = %s AND pr_number = %s AND head_sha = %s",
            (status, repo, pr_number, head_sha),
        )
        conn.commit()


def test_verdict_unknown_when_no_session_exists(grumpy_env) -> None:
    repo = f"octo/unknown-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        response = client.get(
            "/verdict",
            params={"repo": repo, "pr_number": 1, "head_sha": "a" * 40},
            headers=_headers(grumpy_env.token),
        )
    assert response.status_code == 200
    assert response.json() == {"status": "UNKNOWN"}


def test_verdict_pending_after_session_created(grumpy_env) -> None:
    repo = f"octo/pending-{secrets.token_hex(4)}"
    head_sha = "c" * 40
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo, pr_number=2, head_sha=head_sha)
        assert created.status_code == 201

        response = client.get(
            "/verdict",
            params={"repo": repo, "pr_number": 2, "head_sha": head_sha},
            headers=_headers(grumpy_env.token),
        )
    assert response.json() == {"status": "PENDING"}


def test_verdict_passed_after_grading(grumpy_env, database_url: str) -> None:
    repo = f"octo/passed-{secrets.token_hex(4)}"
    head_sha = "f" * 40
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo, pr_number=4, head_sha=head_sha)
        assert created.status_code == 201

    _set_status(database_url, repo=repo, pr_number=4, head_sha=head_sha, status="passed")

    with TestClient(app) as client:
        response = client.get(
            "/verdict",
            params={"repo": repo, "pr_number": 4, "head_sha": head_sha},
            headers=_headers(grumpy_env.token),
        )
    assert response.json() == {"status": "PASSED"}


def test_verdict_failed_after_grading(grumpy_env, database_url: str) -> None:
    repo = f"octo/failed-{secrets.token_hex(4)}"
    head_sha = "aa" * 20
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo, pr_number=5, head_sha=head_sha)
        assert created.status_code == 201

    _set_status(database_url, repo=repo, pr_number=5, head_sha=head_sha, status="failed")

    with TestClient(app) as client:
        response = client.get(
            "/verdict",
            params={"repo": repo, "pr_number": 5, "head_sha": head_sha},
            headers=_headers(grumpy_env.token),
        )
    assert response.json() == {"status": "FAILED"}


def test_verdict_unknown_for_different_sha_on_same_pr(grumpy_env) -> None:
    repo = f"octo/diffsha-{secrets.token_hex(4)}"
    head_sha_a = "d" * 40
    head_sha_b = "e" * 40
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo, pr_number=3, head_sha=head_sha_a)
        assert created.status_code == 201

        response = client.get(
            "/verdict",
            params={"repo": repo, "pr_number": 3, "head_sha": head_sha_b},
            headers=_headers(grumpy_env.token),
        )
    assert response.json() == {"status": "UNKNOWN"}


def test_verdict_rejects_repo_not_in_allowlist(grumpy_env, monkeypatch) -> None:
    # Must be set before `with TestClient(app)`: get_settings() runs once,
    # inside the lifespan, at TestClient.__enter__().
    monkeypatch.setenv("GRUMPY_ALLOWED_REPOS", "octo/only-allowed")
    with TestClient(app) as client:
        response = client.get(
            "/verdict",
            params={"repo": "octo/not-allowed", "pr_number": 1, "head_sha": "a" * 40},
            headers=_headers(grumpy_env.token),
        )
    assert response.status_code == 403


def test_verdict_allows_repo_in_allowlist(grumpy_env, monkeypatch) -> None:
    monkeypatch.setenv("GRUMPY_ALLOWED_REPOS", "octo/only-allowed")
    with TestClient(app) as client:
        response = client.get(
            "/verdict",
            params={"repo": "octo/only-allowed", "pr_number": 1, "head_sha": "a" * 40},
            headers=_headers(grumpy_env.token),
        )
    assert response.status_code == 200
    assert response.json() == {"status": "UNKNOWN"}
