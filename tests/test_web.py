"""Phase 3: the full developer-facing loop — GET /s/{token}, POST
/s/{token}/answer — plus its error states.

FakeGrader passes only when the answer contains the marker string
"looks-good"; everything else here builds on that to prove the wiring.
"""

from __future__ import annotations

import asyncio
import logging
import secrets

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from app.answers import fetch_session_by_token
from app.logging_config import JsonFormatter
from app.main import app
from app.tutorials import create_tutorial

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


def _tutorial_count(database_url: str, session_token: str) -> int:
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            """
            SELECT count(*) FROM tutorials t
            JOIN sessions s ON s.id = t.session_id
            WHERE s.token = %s
            """,
            (session_token,),
        ).fetchone()
    return row[0]


# The one <script> the answer and tutorial pages may carry
# (app/static/nopaste.js). Tests asserting that model output can't smuggle
# a script onto a page strip exactly this tag first, so they still catch
# any other.
_PASTE_GUARD_TAG = '<script src="/static/nopaste.js" defer></script>'


def _without_paste_guard(html: str) -> str:
    return html.replace(_PASTE_GUARD_TAG, "")


def _answer_js_active_flags(database_url: str, session_token: str) -> list[bool | None]:
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            """
            SELECT a.js_active FROM answers a
            JOIN sessions s ON s.id = a.session_id
            WHERE s.token = %s
            ORDER BY a.created_at
            """,
            (session_token,),
        ).fetchall()
    return [row[0] for row in rows]


def _explanation_js_active_flags(database_url: str, session_token: str) -> list[bool | None]:
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            """
            SELECT t.explanation_js_active FROM tutorials t
            JOIN sessions s ON s.id = t.session_id
            WHERE s.token = %s
            ORDER BY t.created_at
            """,
            (session_token,),
        ).fetchall()
    return [row[0] for row in rows]


# The next three helpers exist because FakeGrader's output is fixed
# (marker-based reasoning, a canned tutorial breakdown) — there's no way to
# get markdown- or script-bearing LLM content onto a page through the HTTP
# API alone, so the markdown-rendering tests below write it directly, the
# same way `_expire` reaches past the API to set up state a real grader
# would otherwise produce.
def _set_latest_answer_reasoning(database_url: str, token: str, reasoning: str) -> None:
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "UPDATE answers SET reasoning = %s WHERE session_id = (SELECT id FROM sessions WHERE token = %s)",
            (reasoning, token),
        )
        conn.commit()


def _set_tutorial_breakdown(database_url: str, token: str, breakdown: str) -> None:
    """Rewrites the row into the pre-V5 shape: prose breakdown, no steps.
    Clearing `steps` is what makes it one — it's the column
    templates/tutorial.html branches on to pick the fallback view."""
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "UPDATE tutorials SET breakdown = %s, steps = NULL"
            " WHERE session_id = (SELECT id FROM sessions WHERE token = %s)",
            (breakdown, token),
        )
        conn.commit()


def _set_tutorial_steps(database_url: str, token: str, steps: list[dict]) -> None:
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "UPDATE tutorials SET steps = %s WHERE session_id = (SELECT id FROM sessions WHERE token = %s)",
            (Jsonb(steps), token),
        )
        conn.commit()


def _set_tutorial_reasoning(database_url: str, token: str, reasoning: str) -> None:
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "UPDATE tutorials SET reasoning = %s WHERE session_id = (SELECT id FROM sessions WHERE token = %s)",
            (reasoning, token),
        )
        conn.commit()


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


def test_full_path_fail(grumpy_env, monkeypatch) -> None:
    # A single wrong answer is only immediately terminal at
    # MAX_SESSION_ATTEMPTS=1 — the default (3) instead offers a retry and
    # a tutorial breakdown, covered separately by
    # test_wrong_answer_within_cap_allows_retry_and_second_row_is_written.
    monkeypatch.setenv("MAX_SESSION_ATTEMPTS", "1")
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


def test_double_submission_after_pass_returns_409(grumpy_env, database_url: str) -> None:
    """A session that has already passed is terminal — this is unaffected
    by MAX_SESSION_ATTEMPTS, which only bounds wrong answers."""
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


def test_wrong_answer_within_cap_allows_retry_and_second_row_is_written(
    grumpy_env, database_url: str, monkeypatch
) -> None:
    """Default MAX_SESSION_ATTEMPTS (3): a single wrong answer is no
    longer terminal — it shows both options and stays retryable.
    ENABLE_TUTORIAL is opt-in (default off, see test_tutorial_button_
    hidden_by_default), so this test turns it on to see both options."""
    monkeypatch.setenv("ENABLE_TUTORIAL", "true")
    repo = f"octo/retry-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        first = client.post(
            f"/s/{token}/answer", data={"answer": "I have no idea"}, follow_redirects=False
        )
        assert first.status_code == 303

        page = client.get(f"/s/{token}")
        assert page.status_code == 200
        assert "Not quite" in page.text
        assert "Get a tutorial breakdown" in page.text
        assert "Submit" in page.text

        second = client.post(
            f"/s/{token}/answer", data={"answer": "looks-good"}, follow_redirects=False
        )
        assert second.status_code == 303

        result = client.get(f"/s/{token}")
        assert "PASSED" in result.text

    assert _answer_count(database_url, token) == 2


def test_wrong_answer_at_cap_boundary_becomes_terminal_and_blocks_further_posts(
    grumpy_env, database_url: str, monkeypatch
) -> None:
    monkeypatch.setenv("MAX_SESSION_ATTEMPTS", "1")
    repo = f"octo/capped-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        first = client.post(
            f"/s/{token}/answer", data={"answer": "I have no idea"}, follow_redirects=False
        )
        assert first.status_code == 303

        result = client.get(f"/s/{token}")
        assert "FAILED" in result.text

        second = client.post(
            f"/s/{token}/answer", data={"answer": "looks-good"}, follow_redirects=False
        )
        assert second.status_code == 409

        verdict = client.get(
            "/verdict",
            params={"repo": repo, "pr_number": 1, "head_sha": _VALID_SHA},
            headers=_headers(grumpy_env.token),
        )
        assert verdict.json() == {"status": "FAILED"}

    assert _answer_count(database_url, token) == 1


def test_unlimited_session_attempts_never_locks(grumpy_env, monkeypatch) -> None:
    monkeypatch.setenv("MAX_SESSION_ATTEMPTS", "0")
    repo = f"octo/unlimited-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        for _ in range(5):
            response = client.post(
                f"/s/{token}/answer", data={"answer": "still wrong"}, follow_redirects=False
            )
            assert response.status_code == 303

        verdict = client.get(
            "/verdict",
            params={"repo": repo, "pr_number": 1, "head_sha": _VALID_SHA},
            headers=_headers(grumpy_env.token),
        )
        assert verdict.json() == {"status": "PENDING"}


def test_tutorial_button_hidden_by_default(grumpy_env) -> None:
    repo = f"octo/tutorial-hidden-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        client.post(f"/s/{token}/answer", data={"answer": "I have no idea"}, follow_redirects=False)

        page = client.get(f"/s/{token}")

    assert "Not quite" in page.text
    assert "Get a tutorial breakdown" not in page.text


def test_tutorial_request_returns_404_when_disabled(grumpy_env) -> None:
    repo = f"octo/tutorial-disabled-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        response = client.post(f"/s/{token}/tutorial", follow_redirects=False)

    assert response.status_code == 404
    assert "not available" in response.text


def test_tutorial_full_path_updates_form_with_feedback_and_allows_retry(
    grumpy_env, database_url: str, monkeypatch
) -> None:
    monkeypatch.setenv("ENABLE_TUTORIAL", "true")
    repo = f"octo/tutorial-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        # Long enough a diff for FakeGrader to split it three ways — the
        # default one-line fixture would only yield two steps.
        created = _create_session(client, grumpy_env.token, repo=repo, diff=_WALKTHROUGH_DIFF)
        token = _token_from_url(created["session_url"])

        client.post(f"/s/{token}/answer", data={"answer": "I have no idea"}, follow_redirects=False)

        request_tutorial = client.post(f"/s/{token}/tutorial", follow_redirects=False)
        assert request_tutorial.status_code == 303

        # The walkthrough opens on step 1, which offers Next but not the
        # explain-back form — that lives on the final step.
        tutorial_page = client.get(f"/s/{token}")
        assert tutorial_page.status_code == 200
        assert "FAKE step-by-step breakdown" in tutorial_page.text
        assert "Step 1 of 3" in tutorial_page.text
        assert "?step=2" in tutorial_page.text
        assert "Explain it back" not in tutorial_page.text

        last_step = client.get(f"/s/{token}", params={"step": 3})
        assert last_step.status_code == 200
        assert "Step 3 of 3" in last_step.text
        assert "Explain it back" in last_step.text

        explain = client.post(
            f"/s/{token}/tutorial/explain",
            data={"explanation": "i-understand what this diff does now"},
            follow_redirects=False,
        )
        assert explain.status_code == 303

        back_to_answer = client.get(f"/s/{token}")
        assert back_to_answer.status_code == 200
        assert "Your explanation of the tutorial" in back_to_answer.text
        assert "Submit" in back_to_answer.text

        passed = client.post(
            f"/s/{token}/answer", data={"answer": "looks-good"}, follow_redirects=False
        )
        assert passed.status_code == 303

        result = client.get(f"/s/{token}")
        assert "PASSED" in result.text

    assert _tutorial_count(database_url, token) == 1
    assert _answer_count(database_url, token) == 2


# --- The step-through walkthrough (migrations/V5__tutorial_steps.sql).
# Steps are anchored to line ranges of the diff, and paging between them is
# pure navigation — a query param and a DB read, no writes and no model
# call, so a tutorial still costs exactly one attempt however far it's
# paged through.

_WALKTHROUGH_DIFF = (
    "diff --git a/app/pay.py b/app/pay.py\n"
    "--- a/app/pay.py\n"
    "+++ b/app/pay.py\n"
    "@@ -1,3 +1,4 @@\n"
    " def charge(order):\n"
    "-    process(order)\n"
    "+    with acquire_lock(order):\n"
    "+        process(order)\n"
)

_WALKTHROUGH_STEPS = [
    {"title": "Where it lives", "body": "The header names the file.", "start_line": 1, "end_line": 4},
    {"title": "The lock arrives", "body": "Now it takes a lock.", "start_line": 5, "end_line": 8},
]


def _seed_walkthrough(client: TestClient, database_url: str, env_token: str, repo: str) -> str:
    created = _create_session(client, env_token, repo=repo, diff=_WALKTHROUGH_DIFF)
    token = _token_from_url(created["session_url"])
    client.post(f"/s/{token}/answer", data={"answer": "I have no idea"}, follow_redirects=False)
    client.post(f"/s/{token}/tutorial", follow_redirects=False)
    _set_tutorial_steps(database_url, token, _WALKTHROUGH_STEPS)
    return token


def test_tutorial_step_shows_its_own_slice_and_navigation(
    grumpy_env, database_url: str, monkeypatch
) -> None:
    monkeypatch.setenv("ENABLE_TUTORIAL", "true")
    repo = f"octo/tutorial-steps-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _seed_walkthrough(client, database_url, grumpy_env.token, repo)

        first = client.get(f"/s/{token}")
        second = client.get(f"/s/{token}", params={"step": 2})

    assert "Step 1 of 2" in first.text
    assert "Where it lives" in first.text
    assert "The lock arrives" not in first.text
    assert 'href="?step=2"' in first.text
    assert 'href="?step=1"' not in first.text  # nothing to go back to

    assert "Step 2 of 2" in second.text
    assert "The lock arrives" in second.text
    assert "Where it lives" not in second.text
    assert 'href="?step=1"' in second.text
    assert 'href="?step=3"' not in second.text

    # The slice is captioned with the file the lines belong to, derived
    # from the diff headers rather than taken from the step.
    assert "app/pay.py" in second.text
    assert "lines 5–8" in second.text
    assert "acquire_lock" in second.text


def test_tutorial_step_out_of_range_or_malformed_still_renders(
    grumpy_env, database_url: str, monkeypatch
) -> None:
    """A hand-edited or truncated `?step=` must not break an otherwise
    valid session: out-of-range clamps to the nearest end, unparseable
    opens at the start."""
    monkeypatch.setenv("ENABLE_TUTORIAL", "true")
    repo = f"octo/tutorial-badstep-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _seed_walkthrough(client, database_url, grumpy_env.token, repo)
        pages = {
            raw: client.get(f"/s/{token}", params={"step": raw})
            for raw in ("999", "0", "-1", "abc", "", "2; DROP TABLE tutorials")
        }

    assert all(page.status_code == 200 for page in pages.values())
    assert "Step 2 of 2" in pages["999"].text  # clamped up
    for raw in ("0", "-1", "abc", "", "2; DROP TABLE tutorials"):
        assert "Step 1 of 2" in pages[raw].text


def test_tutorial_explain_error_returns_to_the_step_holding_the_form(
    grumpy_env, database_url: str, monkeypatch
) -> None:
    """The form only exists on the final step, so a validation error has to
    re-render there — dropping back to step 1 would hide both the error and
    what was typed."""
    monkeypatch.setenv("ENABLE_TUTORIAL", "true")
    repo = f"octo/tutorial-steperr-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _seed_walkthrough(client, database_url, grumpy_env.token, repo)
        response = client.post(
            f"/s/{token}/tutorial/explain", data={"explanation": "   "}, follow_redirects=False
        )

    assert response.status_code == 422
    assert "Step 2 of 2" in response.text
    assert "before submitting" in response.text
    assert "Explain it back" in response.text


def test_tutorial_step_body_markdown_is_rendered_and_sanitized(
    grumpy_env, database_url: str, monkeypatch
) -> None:
    """Step titles and bodies are model output derived from an
    attacker-controlled diff, on an unauthenticated page — same bar as the
    prose breakdown: bodies render as markdown, scripts do not survive."""
    monkeypatch.setenv("ENABLE_TUTORIAL", "true")
    repo = f"octo/tutorial-stepmd-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])
        client.post(f"/s/{token}/answer", data={"answer": "I have no idea"}, follow_redirects=False)
        client.post(f"/s/{token}/tutorial", follow_redirects=False)

        _set_tutorial_steps(
            database_url,
            token,
            [
                {
                    "title": "<script>alert('title')</script>",
                    "body": "**First step** does the thing <script>alert(1)</script>",
                    "start_line": 1,
                    "end_line": 2,
                }
            ],
        )
        page = client.get(f"/s/{token}")

    assert "<strong>First step</strong>" in page.text
    assert "**First step**" not in page.text
    assert "<script" not in _without_paste_guard(page.text)


def test_tutorial_explain_back_failing_still_unlocks_retry(grumpy_env, monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_TUTORIAL", "true")
    repo = f"octo/tutorial-fail-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        client.post(f"/s/{token}/answer", data={"answer": "I have no idea"}, follow_redirects=False)
        client.post(f"/s/{token}/tutorial", follow_redirects=False)

        explain = client.post(
            f"/s/{token}/tutorial/explain",
            data={"explanation": "still not sure honestly"},
            follow_redirects=False,
        )
        assert explain.status_code == 303

        back_to_answer = client.get(f"/s/{token}")
        assert back_to_answer.status_code == 200
        assert "Submit" in back_to_answer.text

        retry = client.post(
            f"/s/{token}/answer", data={"answer": "looks-good"}, follow_redirects=False
        )
        assert retry.status_code == 303


def test_requesting_tutorial_twice_without_explaining_is_idempotent(
    grumpy_env, database_url: str, monkeypatch
) -> None:
    monkeypatch.setenv("ENABLE_TUTORIAL", "true")
    repo = f"octo/tutorial-idem-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        client.post(f"/s/{token}/answer", data={"answer": "I have no idea"}, follow_redirects=False)

        first = client.post(f"/s/{token}/tutorial", follow_redirects=False)
        second = client.post(f"/s/{token}/tutorial", follow_redirects=False)
        assert first.status_code == 303
        assert second.status_code == 303

    assert _tutorial_count(database_url, token) == 1


def test_tutorial_is_offered_and_grantable_before_any_answer(
    grumpy_env, database_url: str, monkeypatch
) -> None:
    """The walkthrough isn't a consolation prize for answering wrong: the
    button sits next to Submit on the very first view, and the route
    grants one with no answer row in sight. It still costs an attempt
    (see test_tutorial_request_that_exhausts_shared_cap_locks_session),
    which is what bounds the spend now that the wrong-answer
    precondition is gone."""
    monkeypatch.setenv("ENABLE_TUTORIAL", "true")
    repo = f"octo/tutorial-early-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        first_view = client.get(f"/s/{token}")
        assert "Get a tutorial breakdown" in first_view.text
        assert "Not quite" not in first_view.text

        response = client.post(f"/s/{token}/tutorial", follow_redirects=False)
        assert response.status_code == 303

        walkthrough = client.get(f"/s/{token}")
        assert "FAKE step-by-step breakdown" in walkthrough.text

    assert _answer_count(database_url, token) == 0
    assert _tutorial_count(database_url, token) == 1


def test_tutorial_request_on_already_decided_session_returns_409(grumpy_env) -> None:
    repo = f"octo/tutorial-decided-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        client.post(f"/s/{token}/answer", data={"answer": "looks-good"}, follow_redirects=False)

        response = client.post(f"/s/{token}/tutorial", follow_redirects=False)

    assert response.status_code == 409


def test_tutorial_explain_without_in_progress_tutorial_returns_409(grumpy_env) -> None:
    repo = f"octo/tutorial-none-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        client.post(f"/s/{token}/answer", data={"answer": "I have no idea"}, follow_redirects=False)

        response = client.post(
            f"/s/{token}/tutorial/explain",
            data={"explanation": "i-understand"},
            follow_redirects=False,
        )

    assert response.status_code == 409


def test_tutorial_request_that_would_exhaust_shared_cap_is_refused(
    grumpy_env, monkeypatch
) -> None:
    """MAX_SESSION_ATTEMPTS is shared between answers and tutorials, so a
    tutorial request *could* be the action that exhausts the budget.

    It must not be. A tutorial can never pass a session, so exhausting the
    budget with one can only end it — and because view_session checks the
    passed/failed branch before the tutorial branch, the developer would be
    redirected to result.html and never shown the breakdown their last
    attempt just paid for. The route refuses instead, leaving the session
    pending and answerable. (This test previously asserted the old
    behaviour: 303, then FAILED.)"""
    monkeypatch.setenv("MAX_SESSION_ATTEMPTS", "2")
    monkeypatch.setenv("ENABLE_TUTORIAL", "true")
    repo = f"octo/tutorial-exhausts-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        client.post(f"/s/{token}/answer", data={"answer": "I have no idea"}, follow_redirects=False)
        request_tutorial = client.post(f"/s/{token}/tutorial", follow_redirects=False)
        assert request_tutorial.status_code == 409

        result = client.get(f"/s/{token}")
        assert "FAILED" not in result.text


def test_create_tutorial_still_locks_a_session_that_exhausts_the_cap(
    grumpy_env, database_url: str
) -> None:
    """The DB-layer backstop is unchanged and still enforced, even though
    request_tutorial no longer lets a caller reach it: create_tutorial()
    flips a session to terminal 'failed' when its row exhausts the shared
    budget. Exercised directly, since the route now refuses first.

    Driven through the real lifespan (the app.router.lifespan_context
    pattern from test_idempotency.py) so create_tutorial gets a pool bound
    to the same event loop it's awaited on.
    """
    repo = f"octo/backstop-{secrets.token_hex(4)}"
    body = {
        "repo": repo,
        "pr_number": 1,
        "head_sha": _VALID_SHA,
        "base_sha": "2" * 40,
        "diff": "diff --git a/x b/x\n+hello\n",
    }

    async def _run() -> str:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                created = await client.post(
                    "/sessions", json=body, headers={"Authorization": f"Bearer {grumpy_env.token}"}
                )
            token = _token_from_url(created.json()["session_url"])

            session = await fetch_session_by_token(app.state.pool, token)
            _, status = await create_tutorial(
                app.state.pool,
                session_id=session["id"],
                breakdown="a breakdown",
                max_session_attempts=1,  # this row alone exhausts the budget
            )
            return status

    assert asyncio.run(_run()) == "failed"

    with psycopg.connect(database_url) as conn:
        status = conn.execute(
            "SELECT status FROM sessions WHERE repo = %s", (repo,)
        ).fetchone()[0]
    assert status == "failed"


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


# Markdown rendering (app/rendering.py): the tutorial breakdown, grading
# reasoning, and the developer's own answer text are all rendered as
# markdown rather than shown as preformatted plain text. Unlike the diff
# above (which is escaped, never rendered as HTML), this content is
# genuinely turned into live HTML — so unlike
# test_diff_with_script_tag_is_escaped, the bar here is that dangerous
# content is *stripped*, not merely escaped. tests/test_rendering.py
# covers render_markdown itself in isolation; these confirm the filter is
# actually wired into each template.


def test_answer_body_markdown_is_rendered_on_result_page(grumpy_env) -> None:
    repo = f"octo/answer-md-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        client.post(
            f"/s/{token}/answer",
            data={"answer": "looks-good, and **this part** is bold"},
            follow_redirects=False,
        )

        result = client.get(f"/s/{token}")

    assert "<strong>this part</strong>" in result.text
    assert "**this part**" not in result.text


def test_answer_body_script_tag_is_stripped_not_just_escaped_on_result_page(
    grumpy_env, monkeypatch
) -> None:
    monkeypatch.setenv("MAX_SESSION_ATTEMPTS", "1")
    repo = f"octo/answer-xss-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        client.post(
            f"/s/{token}/answer",
            data={"answer": "no idea <script>alert(1)</script>"},
            follow_redirects=False,
        )

        result = client.get(f"/s/{token}")

    assert "<script" not in result.text


def test_tutorial_breakdown_markdown_is_rendered_and_sanitized(
    grumpy_env, database_url: str, monkeypatch
) -> None:
    """Doubles as the pre-V5 fallback test: _set_tutorial_breakdown clears
    `steps`, so this is a tutorial row from before the walkthrough existed
    — it must still render, as one block of prose with no step navigation.
    """
    monkeypatch.setenv("ENABLE_TUTORIAL", "true")
    repo = f"octo/tutorial-md-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        client.post(f"/s/{token}/answer", data={"answer": "I have no idea"}, follow_redirects=False)
        client.post(f"/s/{token}/tutorial", follow_redirects=False)

        _set_tutorial_breakdown(
            database_url,
            token,
            "1. **First step** does the thing\n2. Second step: <script>alert(1)</script>\n",
        )

        page = client.get(f"/s/{token}")

    assert "<strong>First step</strong>" in page.text
    assert "<script" not in _without_paste_guard(page.text)
    assert "**First step**" not in page.text
    # Legacy shape: the whole breakdown at once, and the explain-back form
    # right there — no steps to page through.
    assert "Second step" in page.text
    assert "Step 1 of" not in page.text
    assert "Explain it back" in page.text


def test_previous_reasoning_markdown_is_rendered_and_sanitized(
    grumpy_env, database_url: str
) -> None:
    repo = f"octo/reasoning-md-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        client.post(f"/s/{token}/answer", data={"answer": "I have no idea"}, follow_redirects=False)

        _set_latest_answer_reasoning(
            database_url,
            token,
            "You said `foo()` does X, but the diff shows <script>alert(1)</script> Y instead.",
        )

        page = client.get(f"/s/{token}")

    assert "<code>foo()</code>" in page.text
    assert "<script" not in _without_paste_guard(page.text)


def test_result_page_reasoning_markdown_is_rendered_and_sanitized(
    grumpy_env, database_url: str, monkeypatch
) -> None:
    monkeypatch.setenv("MAX_SESSION_ATTEMPTS", "1")
    repo = f"octo/result-reasoning-md-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        client.post(f"/s/{token}/answer", data={"answer": "I have no idea"}, follow_redirects=False)

        _set_latest_answer_reasoning(
            database_url, token, "**Wrong**: <script>alert(1)</script> see above"
        )

        result = client.get(f"/s/{token}")

    assert "<strong>Wrong</strong>" in result.text
    assert "<script" not in result.text


def test_tutorial_feedback_markdown_is_rendered_and_sanitized(
    grumpy_env, database_url: str, monkeypatch
) -> None:
    monkeypatch.setenv("ENABLE_TUTORIAL", "true")
    repo = f"octo/tutorial-feedback-md-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        client.post(f"/s/{token}/answer", data={"answer": "I have no idea"}, follow_redirects=False)
        client.post(f"/s/{token}/tutorial", follow_redirects=False)
        client.post(
            f"/s/{token}/tutorial/explain",
            data={"explanation": "i-understand what this diff does now"},
            follow_redirects=False,
        )

        _set_tutorial_reasoning(
            database_url, token, "Good, you got **the key point**. <script>alert(1)</script>"
        )

        page = client.get(f"/s/{token}")

    assert "<strong>the key point</strong>" in page.text
    assert "<script" not in _without_paste_guard(page.text)


# --- Tutorial budget: the last attempt is reserved for answering ---------
#
# A tutorial can never itself pass a session, so one that exhausts the
# shared MAX_SESSION_ATTEMPTS budget can only ever end it. Before this,
# the offer was shown whenever attempts_used < cap, so taking it on the
# last attempt flipped the session to terminal 'failed' and redirected to
# result.html — charging a model call for a breakdown that was never
# rendered, and blocking the PR without a second answer.


def _use_attempts(client: TestClient, token: str, n: int) -> None:
    """Burn n attempts with wrong answers (FakeGrader fails anything
    without the marker)."""
    for _ in range(n):
        client.post(f"/s/{token}/answer", data={"answer": "wrong"}, follow_redirects=False)


def test_tutorial_offered_while_a_spare_answer_attempt_remains(
    grumpy_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENABLE_TUTORIAL", "true")
    monkeypatch.setenv("MAX_SESSION_ATTEMPTS", "3")
    repo = f"octo/tut-offer-{secrets.token_hex(4)}"

    with TestClient(app) as client:
        session = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(session["session_url"])

        _use_attempts(client, token, 1)  # 1 used, cap 3 -> one to spare
        assert "/tutorial" in client.get(f"/s/{token}").text


def test_tutorial_not_offered_when_only_the_final_attempt_remains(
    grumpy_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENABLE_TUTORIAL", "true")
    monkeypatch.setenv("MAX_SESSION_ATTEMPTS", "3")
    repo = f"octo/tut-lastattempt-{secrets.token_hex(4)}"

    with TestClient(app) as client:
        session = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(session["session_url"])

        _use_attempts(client, token, 2)  # 2 used, cap 3 -> only the answer left
        page = client.get(f"/s/{token}")

        assert page.status_code == 200
        assert "/tutorial" not in page.text
        # Still answerable — the point is that the attempt was preserved.
        assert "</form>" in page.text


def test_tutorial_post_on_the_final_attempt_is_refused_without_spending_a_call(
    grumpy_env, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The button is hidden, but this route is an unauthenticated POST —
    a stale form or curl still reaches it, so it must refuse server-side
    and must not write a tutorial row or fail the session."""
    monkeypatch.setenv("ENABLE_TUTORIAL", "true")
    monkeypatch.setenv("MAX_SESSION_ATTEMPTS", "3")
    repo = f"octo/tut-forced-{secrets.token_hex(4)}"

    with TestClient(app) as client:
        session = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(session["session_url"])

        _use_attempts(client, token, 2)
        response = client.post(f"/s/{token}/tutorial", follow_redirects=False)

        assert response.status_code == 409

    with psycopg.connect(database_url) as conn:
        tutorials = conn.execute(
            "SELECT count(*) FROM tutorials t JOIN sessions s ON s.id = t.session_id"
            " WHERE s.token = %s",
            (token,),
        ).fetchone()[0]
        status = conn.execute(
            "SELECT status FROM sessions WHERE token = %s", (token,)
        ).fetchone()[0]

    assert tutorials == 0, "refused request must not spend a model call"
    assert status == "pending", "refusing a tutorial must not fail the session"


def test_a_tutorial_taken_early_still_leaves_an_answer_attempt(
    grumpy_env, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the reservation: take the lesson, then answer."""
    monkeypatch.setenv("ENABLE_TUTORIAL", "true")
    monkeypatch.setenv("MAX_SESSION_ATTEMPTS", "3")
    repo = f"octo/tut-thenanswer-{secrets.token_hex(4)}"

    with TestClient(app) as client:
        session = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(session["session_url"])

        _use_attempts(client, token, 1)
        client.post(f"/s/{token}/tutorial", follow_redirects=False)  # 2 used
        client.post(
            f"/s/{token}/tutorial/explain",
            data={"explanation": "i-understand"},
            follow_redirects=False,
        )

        # One attempt left, and it is answerable — with the passing marker.
        client.post(
            f"/s/{token}/answer", data={"answer": "looks-good"}, follow_redirects=False
        )

    with psycopg.connect(database_url) as conn:
        status = conn.execute(
            "SELECT status FROM sessions WHERE token = %s", (token,)
        ).fetchone()[0]

    assert status == "passed"


def test_unlimited_attempts_always_affords_a_tutorial(
    grumpy_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENABLE_TUTORIAL", "true")
    monkeypatch.setenv("MAX_SESSION_ATTEMPTS", "0")  # 0 == unlimited
    repo = f"octo/tut-unlimited-{secrets.token_hex(4)}"

    with TestClient(app) as client:
        session = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(session["session_url"])

        _use_attempts(client, token, 5)
        assert "/tutorial" in client.get(f"/s/{token}").text


def test_cap_of_one_never_offers_a_tutorial(
    grumpy_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a single attempt there is nothing to spend on a lesson."""
    monkeypatch.setenv("ENABLE_TUTORIAL", "true")
    monkeypatch.setenv("MAX_SESSION_ATTEMPTS", "1")
    repo = f"octo/tut-capone-{secrets.token_hex(4)}"

    with TestClient(app) as client:
        session = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(session["session_url"])

        assert "/tutorial" not in client.get(f"/s/{token}").text


# --- Paste guard (app/static/nopaste.js, migrations/V6__paste_guard.sql) --
#
# The blocking itself is browser behaviour, out of reach of TestClient.
# These cover what the server owns: the script is served and referenced,
# and whether it ran is recorded per submission — never enforced.


def test_paste_guard_script_is_served_as_javascript(grumpy_env) -> None:
    with TestClient(app) as client:
        response = client.get("/static/nopaste.js")

    assert response.status_code == 200
    # nosniff is on: served as anything but JavaScript, browsers won't run it.
    assert "javascript" in response.headers["content-type"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "insertFromPaste" in response.text


def test_paste_guard_is_loaded_on_both_pages_with_a_textarea(
    grumpy_env, database_url: str, monkeypatch
) -> None:
    monkeypatch.setenv("ENABLE_TUTORIAL", "true")
    suffix = secrets.token_hex(4)
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=f"octo/guard-answer-{suffix}")
        answer_page = client.get(f"/s/{_token_from_url(created['session_url'])}")

        tutorial_token = _seed_walkthrough(
            client, database_url, grumpy_env.token, f"octo/guard-tutorial-{suffix}"
        )
        last_step = client.get(f"/s/{tutorial_token}", params={"step": 2})

    assert "<textarea" in answer_page.text
    assert answer_page.text.count(_PASTE_GUARD_TAG) == 1
    assert "<textarea" in last_step.text
    assert last_step.text.count(_PASTE_GUARD_TAG) == 1


def test_answer_records_whether_the_paste_guard_ran(grumpy_env, database_url: str) -> None:
    """A missing `js_active` is the "script never ran" case (JS off, curl).
    It's recorded, but the answer is still graded normally — here it's the
    one that passes."""
    repo = f"octo/guard-flag-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        client.post(
            f"/s/{token}/answer",
            data={"answer": "I have no idea", "js_active": "1"},
            follow_redirects=False,
        )
        unguarded = client.post(
            f"/s/{token}/answer", data={"answer": "looks-good"}, follow_redirects=False
        )
        assert unguarded.status_code == 303

        assert "PASSED" in client.get(f"/s/{token}").text

    assert _answer_js_active_flags(database_url, token) == [True, False]


def test_tutorial_explanation_records_whether_the_paste_guard_ran(
    grumpy_env, database_url: str, monkeypatch
) -> None:
    monkeypatch.setenv("ENABLE_TUTORIAL", "true")
    monkeypatch.setenv("MAX_SESSION_ATTEMPTS", "0")  # room for two tutorials
    repo = f"octo/guard-explain-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        client.post(f"/s/{token}/tutorial", follow_redirects=False)
        client.post(
            f"/s/{token}/tutorial/explain",
            data={"explanation": "i-understand", "js_active": "1"},
            follow_redirects=False,
        )
        client.post(f"/s/{token}/tutorial", follow_redirects=False)
        unguarded = client.post(
            f"/s/{token}/tutorial/explain",
            data={"explanation": "i-understand"},
            follow_redirects=False,
        )
        assert unguarded.status_code == 303

    assert _explanation_js_active_flags(database_url, token) == [True, False]


def test_unguarded_submission_is_logged_without_the_token(grumpy_env) -> None:
    """Captured the way tests/test_logging.py does, not with caplog:
    configure_logging() replaces root.handlers at startup, so a handler
    has to be attached afterwards or every assertion passes vacuously."""
    emitted: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            emitted.append(self.format(record))

    repo = f"octo/guard-log-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        handler = Capture()
        handler.setFormatter(JsonFormatter())
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            client.post(
                f"/s/{token}/answer",
                data={"answer": "I have no idea", "js_active": "1"},
                follow_redirects=False,
            )
            client.post(f"/s/{token}/answer", data={"answer": "looks-good"}, follow_redirects=False)
        finally:
            root.removeHandler(handler)

    no_js = [line for line in emitted if '"outcome": "no_js"' in line]
    assert len(no_js) == 1, "only the submission without js_active is logged"
    assert "/s/<redacted>/answer" in no_js[0]
    assert repo in no_js[0]
    assert token not in "\n".join(emitted)
