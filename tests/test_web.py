"""The developer-facing exam-sheet loop — GET /s/{token}, POST
/s/{token}/submit — plus its error states.

FakeGrader marks an answer passed only when it contains the marker string
"looks-good"; everything here builds on that to prove the wiring. A fresh
sheet has EXAM_QUESTION_COUNT questions (default 5), and the session passes
at PASSINGMARKS correct (default 3).
"""

from __future__ import annotations

import logging
import secrets

import psycopg
from fastapi.testclient import TestClient

from app.grading import HIGH_LEVEL_QUESTION, GradingError
from app.logging_config import JsonFormatter
from app.main import app

_VALID_SHA = "1" * 40


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_session(
    client: TestClient, token: str, *, repo: str, diff: str = "diff --git a/x b/x\n+hello\n"
) -> dict:
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


def _submit(client: TestClient, token: str, answers: dict[str, str], **extra):
    """POST one exam sheet. `answers` maps answer_i -> text; `extra` adds
    other form fields (e.g. js_active)."""
    return client.post(
        f"/s/{token}/submit", data={**answers, **extra}, follow_redirects=False
    )


def _submit_same(client: TestClient, token: str, text: str, **extra):
    """Submit the same text for every question. FakeGrader marks an answer
    passed iff it contains 'looks-good', so 'looks-good' passes the whole
    sheet and anything else fails it. Sends more answer_i fields than there
    are questions; the handler reads only as many as the sheet has."""
    return _submit(client, token, {f"answer_{i}": text for i in range(10)}, **extra)


def _submit_marks(client: TestClient, token: str, n_pass: int, n_total: int = 5):
    """A sheet with exactly n_pass correct answers (the first n_pass contain
    the marker, the rest don't). Assumes the default EXAM_QUESTION_COUNT."""
    answers = {
        f"answer_{i}": ("looks-good" if i < n_pass else "nope") for i in range(n_total)
    }
    return _submit(client, token, answers)


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


def _set_latest_answer_reasoning(database_url: str, token: str, reasoning: str) -> None:
    """FakeGrader's reasoning is fixed, so markdown/script-bearing content
    (which only a real grader would produce from a crafted diff) is written
    directly, the same way _expire reaches past the API to set up state."""
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "UPDATE answers SET reasoning = %s WHERE session_id = (SELECT id FROM sessions WHERE token = %s)",
            (reasoning, token),
        )
        conn.commit()


_PASTE_GUARD_TAG = '<script src="/static/nopaste.js" defer></script>'


def _without_paste_guard(html: str) -> str:
    return html.replace(_PASTE_GUARD_TAG, "")


# --- the core loop ------------------------------------------------------


def test_sheet_renders_the_high_level_question_first(grumpy_env) -> None:
    repo = f"octo/sheet-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        page = client.get(f"/s/{token}")

    assert page.status_code == 200
    assert HIGH_LEVEL_QUESTION in page.text
    assert "FAKE scoped question" in page.text
    assert page.text.count("<textarea") == 5  # EXAM_QUESTION_COUNT default


def test_full_path_pass(grumpy_env) -> None:
    repo = f"octo/pass-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        submit = _submit_same(client, token, "looks-good, this makes sense to me")
        assert submit.status_code == 303
        assert submit.headers["location"] == f"/s/{token}"

        result = client.get(f"/s/{token}")
        assert "PASSED" in result.text

        verdict = client.get(
            "/verdict",
            params={"repo": repo, "pr_number": 1, "head_sha": _VALID_SHA},
            headers=_headers(grumpy_env.token),
        )
        assert verdict.json() == {"status": "PASSED"}


def test_full_path_fail(grumpy_env, monkeypatch) -> None:
    monkeypatch.setenv("MAX_SESSION_ATTEMPTS", "1")
    repo = f"octo/fail-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        submit = _submit_same(client, token, "I have no idea what this does")
        assert submit.status_code == 303

        result = client.get(f"/s/{token}")
        assert "FAILED" in result.text

        verdict = client.get(
            "/verdict",
            params={"repo": repo, "pr_number": 1, "head_sha": _VALID_SHA},
            headers=_headers(grumpy_env.token),
        )
        assert verdict.json() == {"status": "FAILED"}


def test_passes_at_the_mark_threshold(grumpy_env, monkeypatch) -> None:
    """PASSINGMARKS of EXAM_QUESTION_COUNT is enough — not every answer."""
    monkeypatch.setenv("EXAM_QUESTION_COUNT", "5")
    monkeypatch.setenv("PASSINGMARKS", "3")
    repo = f"octo/threshold-pass-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        _submit_marks(client, token, n_pass=3)

        assert "PASSED" in client.get(f"/s/{token}").text


def test_fails_below_the_mark_threshold(grumpy_env, monkeypatch) -> None:
    monkeypatch.setenv("EXAM_QUESTION_COUNT", "5")
    monkeypatch.setenv("PASSINGMARKS", "3")
    monkeypatch.setenv("MAX_SESSION_ATTEMPTS", "1")
    repo = f"octo/threshold-fail-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        _submit_marks(client, token, n_pass=2)

        assert "FAILED" in client.get(f"/s/{token}").text


def test_wrong_sheet_within_cap_allows_retry(grumpy_env, database_url: str) -> None:
    """Default MAX_SESSION_ATTEMPTS (3): a failing sheet isn't terminal — it
    shows the per-question outcome and stays retryable."""
    repo = f"octo/retry-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        assert _submit_same(client, token, "no idea").status_code == 303

        page = client.get(f"/s/{token}")
        assert page.status_code == 200
        assert "Not quite" in page.text
        assert "Missed last time" in page.text
        assert "Submit" in page.text

        assert _submit_same(client, token, "looks-good").status_code == 303
        assert "PASSED" in client.get(f"/s/{token}").text

    assert _answer_count(database_url, token) == 2


def test_wrong_sheet_at_cap_boundary_is_terminal(grumpy_env, database_url: str, monkeypatch) -> None:
    monkeypatch.setenv("MAX_SESSION_ATTEMPTS", "1")
    repo = f"octo/capped-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        assert _submit_same(client, token, "no idea").status_code == 303
        assert "FAILED" in client.get(f"/s/{token}").text

        second = _submit_same(client, token, "looks-good")
        assert second.status_code == 409

    assert _answer_count(database_url, token) == 1


def test_unlimited_attempts_never_locks(grumpy_env, monkeypatch) -> None:
    monkeypatch.setenv("MAX_SESSION_ATTEMPTS", "0")
    repo = f"octo/unlimited-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        for _ in range(5):
            assert _submit_same(client, token, "still wrong").status_code == 303

        verdict = client.get(
            "/verdict",
            params={"repo": repo, "pr_number": 1, "head_sha": _VALID_SHA},
            headers=_headers(grumpy_env.token),
        )
        assert verdict.json() == {"status": "PENDING"}


def test_double_submission_after_pass_returns_409(grumpy_env, database_url: str) -> None:
    repo = f"octo/double-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        assert _submit_same(client, token, "looks-good").status_code == 303
        assert _submit_same(client, token, "looks-good again").status_code == 409

    assert _answer_count(database_url, token) == 1


# --- error states -------------------------------------------------------


def test_expired_session_returns_410_on_get_and_rejects_post(grumpy_env, database_url: str) -> None:
    repo = f"octo/expired-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        _expire(database_url, token)

        assert client.get(f"/s/{token}").status_code == 410
        assert _submit_same(client, token, "looks-good").status_code == 410

    assert _answer_count(database_url, token) == 0


def test_rerun_after_expiry_issues_a_fresh_link(grumpy_env, database_url: str) -> None:
    repo = f"octo/expired-rerun-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        old_token = _token_from_url(created["session_url"])
        _expire(database_url, old_token)

        rerun = _create_session(client, grumpy_env.token, repo=repo)
        new_token = _token_from_url(rerun["session_url"])

        assert new_token != old_token
        assert client.get(f"/s/{old_token}").status_code == 404
        assert client.get(f"/s/{new_token}").status_code == 200

        again = client.post(
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
        assert again.status_code == 200
        assert again.json()["session_url"] == rerun["session_url"]


def test_unknown_token_returns_generic_404(grumpy_env) -> None:
    unknown_token = secrets.token_urlsafe(32)
    with TestClient(app) as client:
        response = client.get(f"/s/{unknown_token}")

    assert response.status_code == 404
    assert "expired" not in response.text.lower()
    assert unknown_token not in response.text


def test_empty_answer_rerenders_form_with_error_and_writes_no_row(grumpy_env, database_url: str) -> None:
    repo = f"octo/empty-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        # One blank answer; the rest filled.
        answers = {f"answer_{i}": "looks-good" for i in range(5)}
        answers["answer_2"] = "   "
        response = _submit(client, token, answers)

    assert response.status_code == 422
    assert "answer every question" in response.text
    assert _answer_count(database_url, token) == 0


def test_sheet_over_max_length_rerenders_with_error_and_writes_no_row(
    grumpy_env, database_url: str, monkeypatch
) -> None:
    monkeypatch.setenv("MAX_ANSWER_BYTES", "10")
    repo = f"octo/toolong-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        response = _submit_same(client, token, "way over ten bytes")

    assert response.status_code == 422
    assert "too long" in response.text
    assert _answer_count(database_url, token) == 0


# --- rendering / escaping ------------------------------------------------


def test_diff_with_script_tag_is_escaped(grumpy_env) -> None:
    repo = f"octo/escape-{secrets.token_hex(4)}"
    malicious_diff = "diff --git a/x b/x\n+<script>alert(1)</script>\n"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo, diff=malicious_diff)
        token = _token_from_url(created["session_url"])

        page = client.get(f"/s/{token}")

    assert "<script>alert(1)</script>" not in _without_paste_guard(page.text)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text


def test_answer_markdown_is_rendered_on_result_page(grumpy_env) -> None:
    repo = f"octo/answer-md-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        _submit_same(client, token, "looks-good, and **this part** is bold")
        result = client.get(f"/s/{token}")

    assert "<strong>this part</strong>" in result.text
    assert "**this part**" not in result.text


def test_answer_script_tag_is_stripped_on_result_page(grumpy_env, monkeypatch) -> None:
    monkeypatch.setenv("MAX_SESSION_ATTEMPTS", "1")
    repo = f"octo/answer-xss-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        _submit_same(client, token, "no idea <script>alert(1)</script>")
        result = client.get(f"/s/{token}")

    assert "<script" not in result.text


def test_previous_reasoning_markdown_is_rendered_and_sanitized(grumpy_env, database_url: str) -> None:
    repo = f"octo/reasoning-md-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        _submit_same(client, token, "no idea")  # fails, stays pending (cap 3)
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

        _submit_same(client, token, "no idea")  # terminal fail at cap 1
        _set_latest_answer_reasoning(
            database_url, token, "**Wrong**: <script>alert(1)</script> see above"
        )

        result = client.get(f"/s/{token}")

    assert "<strong>Wrong</strong>" in result.text
    assert "<script" not in result.text


# --- paste guard (app/static/nopaste.js) --------------------------------


def test_paste_guard_script_is_served_as_javascript(grumpy_env) -> None:
    with TestClient(app) as client:
        response = client.get("/static/nopaste.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "insertFromPaste" in response.text


def test_paste_guard_is_loaded_once_on_the_exam_page(grumpy_env) -> None:
    repo = f"octo/guard-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        page = client.get(f"/s/{_token_from_url(created['session_url'])}")

    assert "<textarea" in page.text
    assert page.text.count(_PASTE_GUARD_TAG) == 1


def test_sheet_records_whether_the_paste_guard_ran(grumpy_env, database_url: str) -> None:
    repo = f"octo/guard-flag-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        _submit_same(client, token, "no idea", js_active="1")
        unguarded = _submit_same(client, token, "looks-good")
        assert unguarded.status_code == 303
        assert "PASSED" in client.get(f"/s/{token}").text

    assert _answer_js_active_flags(database_url, token) == [True, False]


def test_unguarded_submission_is_logged_without_the_token(grumpy_env) -> None:
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
            _submit_same(client, token, "no idea", js_active="1")
            _submit_same(client, token, "looks-good")
        finally:
            root.removeHandler(handler)

    no_js = [line for line in emitted if '"outcome": "no_js"' in line]
    assert len(no_js) == 1, "only the submission without js_active is logged"
    assert "/s/<redacted>/submit" in no_js[0]
    assert repo in no_js[0]
    assert token not in "\n".join(emitted)


def test_grading_failure_logs_why_and_returns_502(grumpy_env, monkeypatch) -> None:
    """The developer is told only "please try again", by design — so if this
    line doesn't carry the cause, nothing does."""
    emitted: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            emitted.append(self.format(record))

    class FailingGrader:
        async def generate_exam(self, diff, count):
            return [HIGH_LEVEL_QUESTION, "q2", "q3", "q4", "q5"]

        async def grade_exam(self, diff, questions, answers):
            raise GradingError("model hit the 16000-token cap before finishing")

    repo = f"octo/grade-fail-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        created = _create_session(client, grumpy_env.token, repo=repo)
        token = _token_from_url(created["session_url"])

        app.state.grader = FailingGrader()
        handler = Capture()
        handler.setFormatter(JsonFormatter())
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            response = _submit_same(client, token, "looks-good")
        finally:
            root.removeHandler(handler)

    assert response.status_code == 502
    failures = [line for line in emitted if '"outcome": "grade_failed"' in line]
    assert len(failures) == 1
    assert "16000-token cap" in failures[0]
    assert repo in failures[0]
    assert token not in "\n".join(emitted)
