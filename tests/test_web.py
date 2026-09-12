"""Phase 3: the full developer-facing loop — GET /s/{token}, POST
/s/{token}/answer — plus its error states.

FakeGrader passes only when the answer contains the marker string
"looks-good"; everything else here builds on that to prove the wiring.
"""

from __future__ import annotations

import secrets

import psycopg
from fastapi.testclient import TestClient

from app.main import app

_VALID_SHA = "1" * 40


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_session(client: TestClient, token: str, *, repo: str, diff: str = "diff --git a/x b/x\n+hello\n") -> dict:
    response = client.post(
        "/sessions",
        json={
            "repo": repo,
            "pr_number": 1,
            "head_sha": _VALID_SHA,
            "base_sha": "2" * 40,
            "diff": diff,
        },
        headers=_headers(token),
    )
    assert response.status_code == 201
    return response.json()


def _token_from_url(session_url: str) -> str:
    return session_url.rsplit("/", 1)[-1]


def _expire(database_url: str, token: str) -> None:
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "UPDATE sessions SET expires_at = now() - interval '1 day' WHERE token = %s",
            (token,),
        )
        conn.commit()


def _answer_count(database_url: str, session_token: str) -> int:
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            """
            SELECT count(*) FROM answers a
            JOIN sessions s ON s.id = a.session_id
            WHERE s.token = %s
            """,
            (session_token,),
        ).fetchone()
    return row[0]


def test_full_path_pass(grumpy_env) -> None:
    repo = f"octo/pass-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        page = client.get(f"/s/{token}")
        assert page.status_code == 200
        # The fixed question contains an apostrophe, which Jinja2 correctly
        # HTML-escapes (it's -> it&#39;s) — check a substring without one
        # rather than the raw question text.
        assert "What does this change do" in page.text

        submit = client.post(
            f"/s/{token}/answer",
            data={"answer": "looks-good, this makes sense to me"},
            follow_redirects=False,
        )
        assert submit.status_code == 303
        assert submit.headers["location"] == f"/s/{token}"

        result = client.get(f"/s/{token}")
        assert result.status_code == 200
        assert "PASSED" in result.text

        verdict = client.get(
            "/verdict",
            params={"repo": repo, "pr_number": 1, "head_sha": _VALID_SHA},
            headers=_headers(grumpy_env.token),
        )
        assert verdict.json() == {"status": "PASSED"}


def test_full_path_fail(grumpy_env) -> None:
    repo = f"octo/fail-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        submit = client.post(
            f"/s/{token}/answer",
            data={"answer": "I have no idea what this does"},
            follow_redirects=False,
        )
        assert submit.status_code == 303

        result = client.get(f"/s/{token}")
        assert "FAILED" in result.text

        verdict = client.get(
            "/verdict",
            params={"repo": repo, "pr_number": 1, "head_sha": _VALID_SHA},
            headers=_headers(grumpy_env.token),
        )
        assert verdict.json() == {"status": "FAILED"}


def test_expired_session_returns_410_on_get_and_rejects_post(grumpy_env, database_url: str) -> None:
    repo = f"octo/expired-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        _expire(database_url, token)

        get_response = client.get(f"/s/{token}")
        assert get_response.status_code == 410

        post_response = client.post(
            f"/s/{token}/answer", data={"answer": "looks-good"}, follow_redirects=False
        )
        assert post_response.status_code == 410

    assert _answer_count(database_url, token) == 0


def test_unknown_token_returns_generic_404(grumpy_env) -> None:
    unknown_token = secrets.token_urlsafe(32)
    with TestClient(app) as client:
        response = client.get(f"/s/{unknown_token}")

    assert response.status_code == 404
    # Generic — must not claim to know whether this token ever existed.
    assert "expired" not in response.text.lower()
    assert unknown_token not in response.text


def test_double_submission_returns_409_and_writes_no_second_row(grumpy_env, database_url: str) -> None:
    repo = f"octo/double-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        first = client.post(
            f"/s/{token}/answer", data={"answer": "looks-good"}, follow_redirects=False
        )
        assert first.status_code == 303

        second = client.post(
            f"/s/{token}/answer", data={"answer": "looks-good again"}, follow_redirects=False
        )
        assert second.status_code == 409

    assert _answer_count(database_url, token) == 1


def test_empty_answer_rerenders_form_with_error_and_writes_no_row(grumpy_env, database_url: str) -> None:
    repo = f"octo/empty-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        response = client.post(
            f"/s/{token}/answer", data={"answer": "   "}, follow_redirects=False
        )

    assert response.status_code == 422
    assert "before submitting" in response.text
    assert _answer_count(database_url, token) == 0


def test_answer_over_max_length_rerenders_form_with_error_and_writes_no_row(
    grumpy_env, database_url: str, monkeypatch
) -> None:
    monkeypatch.setenv("MAX_ANSWER_BYTES", "10")
    repo = f"octo/toolong-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        response = client.post(
            f"/s/{token}/answer",
            data={"answer": "this answer is way over the ten byte limit"},
            follow_redirects=False,
        )

    assert response.status_code == 422
    assert "too long" in response.text
    assert _answer_count(database_url, token) == 0


def test_diff_with_script_tag_is_escaped(grumpy_env) -> None:
    repo = f"octo/escape-{secrets.token_hex(4)}"
    malicious_diff = "diff --git a/x b/x\n+<script>alert(1)</script>\n"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo, diff=malicious_diff)
        token = _token_from_url(created["session_url"])

        page = client.get(f"/s/{token}")

    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
