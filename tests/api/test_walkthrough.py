"""The developer-facing paged walkthrough — GET /s/{token}, POST
/s/{token}/answer, POST /s/{token}/explain — one screen per question, each
graded on submit, with retries, an on-demand explanation, and an answer
reveal once the tries run out.

Defaults: EXAM_QUESTION_COUNT=5, PASSINGMARKS=3, MAX_QUESTION_ATTEMPTS=3.
FakeGrader marks an answer passed only when it contains "looks-good".
"""

from __future__ import annotations

import logging
import secrets

import psycopg
from fastapi.testclient import TestClient

from app.ai.grading import HIGH_LEVEL_QUESTION, FakeGrader, GradingError, QuestionMark
from app.logging.config import JsonFormatter
from app.main import app

_VALID_SHA = "1" * 40
_DIFF = (
    "diff --git a/app/pay.py b/app/pay.py\n"
    "--- a/app/pay.py\n"
    "+++ b/app/pay.py\n"
    "@@ -1,3 +1,4 @@\n"
    " def charge(order):\n"
    "-    process(order)\n"
    "+    with acquire_lock(order):\n"
    "+        process(order)\n"
    # Enough changed lines for a full-length sheet; see question_count_for.
    + "".join(f"+    audit_step_{n}(order)\n" for n in range(60))
)


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_session(client: TestClient, token: str, *, repo: str, diff: str = _DIFF) -> dict:
    response = client.post(
        "/sessions",
        json={"repo": repo, "pr_number": 1, "head_sha": _VALID_SHA, "base_sha": "2" * 40, "diff": diff},
        headers=_headers(token),
    )
    assert response.status_code == 201
    return response.json()


def _token_from_url(session_url: str) -> str:
    return session_url.rsplit("/", 1)[-1]


def _answer(client: TestClient, token: str, index: int, text: str, **extra):
    return client.post(
        f"/s/{token}/answer", data={"index": str(index), "answer": text, **extra}, follow_redirects=False
    )


def _explain(client: TestClient, token: str, index: int):
    return client.post(f"/s/{token}/explain", data={"index": str(index)}, follow_redirects=False)


def _skip(client: TestClient, token: str, index: int):
    return client.post(f"/s/{token}/skip", data={"index": str(index)}, follow_redirects=False)


def _pass_screen(client: TestClient, token: str, index: int):
    return _answer(client, token, index, "looks-good")


def _skip_screen(client: TestClient, token: str, index: int, attempts: int = 3) -> None:
    for _ in range(attempts):
        _answer(client, token, index, "nope")


def _expire(database_url: str, token: str) -> None:
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "UPDATE sessions SET expires_at = now() - interval '1 day' WHERE token = %s", (token,)
        )
        conn.commit()


def _marks(database_url: str, token: str) -> dict:
    with psycopg.connect(database_url) as conn:
        row = conn.execute("SELECT marks FROM sessions WHERE token = %s", (token,)).fetchone()
    return row[0] if row else {}


def _status(database_url: str, token: str) -> str:
    with psycopg.connect(database_url) as conn:
        row = conn.execute("SELECT status FROM sessions WHERE token = %s", (token,)).fetchone()
    return row[0]


_PASTE_GUARD_TAG = '<script src="/static/nopaste.js" defer></script>'


def _without_paste_guard(html: str) -> str:
    return html.replace(_PASTE_GUARD_TAG, "")


# --- the screen, retries, reveal ----------------------------------------


def test_first_screen_is_the_high_level_question_with_both_buttons(grumpy_env) -> None:
    repo = f"octo/first-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        page = client.get(f"/s/{token}")

    assert "Question 1 of 5" in page.text
    assert HIGH_LEVEL_QUESTION in page.text
    assert "acquire_lock" in page.text  # whole diff on the high-level screen
    assert "Submit answer" in page.text
    assert "Explain it for me" in page.text


def test_wrong_answer_keeps_the_screen_open_for_another_try(grumpy_env, database_url: str) -> None:
    repo = f"octo/retry-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        assert _answer(client, token, 0, "no idea").status_code == 303
        page = client.get(f"/s/{token}", params={"step": 0})

    assert "Not quite" in page.text
    assert "missing 'looks-good'" in page.text
    assert "Attempt 2 of 3" in page.text
    assert "<textarea" in page.text  # still answerable
    assert _marks(database_url, token)["0"]["state"] == "open"


def test_correct_answer_marks_passed_and_advances(grumpy_env, database_url: str) -> None:
    repo = f"octo/correct-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        _pass_screen(client, token, 0)
        page = client.get(f"/s/{token}", params={"step": 0})

    assert "Correct" in page.text
    assert 'href="?step=1"' in page.text
    assert _marks(database_url, token)["0"]["state"] == "passed"


def test_out_of_tries_reveals_the_answer_and_skips(grumpy_env, database_url: str) -> None:
    repo = f"octo/reveal-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        _skip_screen(client, token, 0)  # 3 wrong tries
        page = client.get(f"/s/{token}", params={"step": 0})

    assert "Out of tries" in page.text
    assert "FAKE reference answer 1" in page.text
    assert "<textarea" not in _without_paste_guard(page.text)  # no more answering
    marks = _marks(database_url, token)
    assert marks["0"]["state"] == "skipped"
    assert len(marks["0"]["attempts"]) == 3


def test_passes_at_three_correct(grumpy_env, database_url: str) -> None:
    repo = f"octo/pass-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        for i in (0, 1, 2):
            _pass_screen(client, token, i)

        result = client.get(f"/s/{token}")
        assert "PASSED" in result.text
        assert "3 of 5 correct" in result.text

        verdict = client.get(
            "/verdict",
            params={"repo": repo, "pr_number": 1, "head_sha": _VALID_SHA},
            headers=_headers(grumpy_env.token),
        )
        assert verdict.json() == {"status": "PASSED"}

    assert _status(database_url, token) == "passed"


def test_fails_once_three_are_skipped(grumpy_env, database_url: str) -> None:
    repo = f"octo/fail-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        for i in (0, 1, 2):  # 3 skipped of 5, only 2 left -> unreachable
            _skip_screen(client, token, i)
        result = client.get(f"/s/{token}")

    assert "FAILED" in result.text
    assert _status(database_url, token) == "failed"


# --- user skip ----------------------------------------------------------


def test_skip_reveals_the_answer_and_moves_on(grumpy_env, database_url: str) -> None:
    repo = f"octo/skip-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        assert _skip(client, token, 0).status_code == 303
        page = client.get(f"/s/{token}", params={"step": 0})
        # The next question is now answerable.
        assert _pass_screen(client, token, 1).status_code == 303

    assert "Skipped" in page.text
    assert "FAKE reference answer 1" in page.text
    assert "Next question" in page.text
    assert "<textarea" not in _without_paste_guard(page.text)
    marks = _marks(database_url, token)
    assert marks["0"]["state"] == "skipped"
    assert marks["0"]["by_user"] is True
    assert marks["0"]["attempts"] == []
    assert marks["1"]["state"] == "passed"


def test_fails_once_three_are_skipped_by_the_user(grumpy_env, database_url: str) -> None:
    repo = f"octo/skipfail-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        for i in (0, 1, 2):
            _skip(client, token, i)
        result = client.get(f"/s/{token}")

    assert "FAILED" in result.text
    assert "Skipped" in result.text
    assert _status(database_url, token) == "failed"


def test_skip_out_of_order_is_a_noop(grumpy_env, database_url: str) -> None:
    repo = f"octo/skipahead-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        assert _skip(client, token, 3).status_code == 303

    assert _marks(database_url, token) == {}


def test_skipping_a_decided_session_returns_409(grumpy_env) -> None:
    repo = f"octo/skipdecided-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        for i in (0, 1, 2):
            _skip(client, token, i)
        assert _skip(client, token, 3).status_code == 409


# --- explain ------------------------------------------------------------


def test_explain_shows_help_without_consuming_an_attempt(grumpy_env, database_url: str) -> None:
    repo = f"octo/explain-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        assert _explain(client, token, 0).status_code == 303
        page = client.get(f"/s/{token}", params={"step": 0})

        assert "Explanation" in page.text
        assert "FAKE explanation for" in page.text
        assert "Attempt 1 of 3" in page.text  # explaining didn't use a try
        assert "<textarea" in page.text  # still answerable

        _pass_screen(client, token, 0)
        assert "Correct" in client.get(f"/s/{token}", params={"step": 0}).text

    marks = _marks(database_url, token)
    assert marks["0"]["state"] == "passed"
    assert len(marks["0"]["attempts"]) == 1  # the explain didn't count


def test_explain_failure_is_502_and_records_nothing(grumpy_env, database_url: str) -> None:
    class ExplainFails(FakeGrader):
        async def explain(self, interpretation, question):
            raise GradingError("model hit the 16000-token cap before finishing")

    repo = f"octo/explain-fail-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        app.state.grader = ExplainFails()
        resp = _explain(client, token, 0)

    assert resp.status_code == 502
    assert _marks(database_url, token) == {}


# --- navigation + error states ------------------------------------------


def test_cannot_skip_ahead(grumpy_env) -> None:
    repo = f"octo/skip-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        page = client.get(f"/s/{token}", params={"step": 4})
    assert "Question 1 of 5" in page.text


def test_out_of_order_submit_bounces_without_recording(grumpy_env, database_url: str) -> None:
    repo = f"octo/order-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        resp = _answer(client, token, 3, "looks-good")  # frontier is 0
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/s/{token}"
    assert _marks(database_url, token) == {}


def test_expired_session_returns_410(grumpy_env, database_url: str) -> None:
    repo = f"octo/expired-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        _expire(database_url, token)
        assert client.get(f"/s/{token}").status_code == 410
        assert _answer(client, token, 0, "looks-good").status_code == 410
        assert _explain(client, token, 0).status_code == 410
    assert _marks(database_url, token) == {}


def test_unknown_token_returns_generic_404(grumpy_env) -> None:
    unknown = secrets.token_urlsafe(32)
    with TestClient(app) as client:
        response = client.get(f"/s/{unknown}")
    assert response.status_code == 404
    assert "expired" not in response.text.lower()
    assert unknown not in response.text


def test_answering_a_decided_session_returns_409(grumpy_env) -> None:
    repo = f"octo/decided-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        for i in (0, 1, 2):
            _pass_screen(client, token, i)  # passed
        assert _answer(client, token, 3, "looks-good").status_code == 409


def test_empty_answer_rerenders_with_error_and_no_attempt(grumpy_env, database_url: str) -> None:
    repo = f"octo/empty-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        resp = _answer(client, token, 0, "   ")
    assert resp.status_code == 422
    assert "Write an answer" in resp.text
    assert _marks(database_url, token) == {}


def test_oversized_answer_rerenders_with_error(grumpy_env, database_url: str, monkeypatch) -> None:
    monkeypatch.setenv("MAX_ANSWER_BYTES", "10")
    repo = f"octo/toolong-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        resp = _answer(client, token, 0, "way over the ten byte limit")
    assert resp.status_code == 422
    assert "too long" in resp.text
    assert _marks(database_url, token) == {}


# --- rendering / escaping ------------------------------------------------


def test_diff_is_escaped_on_the_screen(grumpy_env) -> None:
    repo = f"octo/escape-{secrets.token_hex(4)}"
    diff = "diff --git a/x b/x\n+<script>alert(1)</script>\n"
    with TestClient(app) as client:
        token = _token_from_url(
            _create_session(client, grumpy_env.token, repo=repo, diff=diff)["session_url"]
        )
        page = client.get(f"/s/{token}")
    assert "<script>alert(1)</script>" not in _without_paste_guard(page.text)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text


def test_developer_answer_is_escaped_on_a_passed_screen(grumpy_env) -> None:
    repo = f"octo/ans-escape-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        _answer(client, token, 0, "looks-good <script>alert(2)</script>")  # passes, shown back
        page = client.get(f"/s/{token}", params={"step": 0})
    assert "<script>alert(2)" not in _without_paste_guard(page.text)
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in page.text


def test_note_markdown_is_rendered_and_sanitized(grumpy_env) -> None:
    class MarkdownNoteGrader(FakeGrader):
        async def grade_answer(self, interpretation, question, answer):
            return QuestionMark(passed=False, note="**bad** <script>alert(1)</script>")

    repo = f"octo/note-md-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        app.state.grader = MarkdownNoteGrader()
        _answer(client, token, 0, "whatever")  # wrong -> note shown on open screen
        page = client.get(f"/s/{token}", params={"step": 0})
    assert "<strong>bad</strong>" in page.text
    assert "<script" not in _without_paste_guard(page.text)


# --- paste / copy guard -------------------------------------------------


def test_guard_script_blocks_copy_and_paste(grumpy_env) -> None:
    with TestClient(app) as client:
        response = client.get("/static/nopaste.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "insertFromPaste" in response.text  # paste blocking
    assert '"copy"' in response.text and '"cut"' in response.text  # copy blocking


def test_guard_loaded_once_on_the_screen(grumpy_env) -> None:
    repo = f"octo/guard-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        page = client.get(f"/s/{token}")
    assert "<textarea" in page.text
    assert page.text.count(_PASTE_GUARD_TAG) == 1


def test_attempt_records_whether_the_paste_guard_ran(grumpy_env, database_url: str) -> None:
    repo = f"octo/guard-flag-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        _answer(client, token, 0, "no idea", js_active="1")
        _answer(client, token, 0, "nope")  # no js_active, same screen (still open)
    attempts = _marks(database_url, token)["0"]["attempts"]
    assert attempts[0]["js_active"] is True
    assert attempts[1]["js_active"] is False


def test_unguarded_submission_is_logged_without_the_token(grumpy_env) -> None:
    emitted: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            emitted.append(self.format(record))

    repo = f"octo/guard-log-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        handler = Capture()
        handler.setFormatter(JsonFormatter())
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            _answer(client, token, 0, "no idea", js_active="1")
            _answer(client, token, 0, "nope")  # unguarded
        finally:
            root.removeHandler(handler)

    no_js = [line for line in emitted if '"outcome": "no_js"' in line]
    assert len(no_js) == 1
    assert "/s/<redacted>/answer" in no_js[0]
    assert token not in "\n".join(emitted)


def test_grading_failure_logs_why_and_returns_502(grumpy_env, database_url: str) -> None:
    class FailingGrader(FakeGrader):
        async def grade_answer(self, interpretation, question, answer):
            raise GradingError("model hit the 16000-token cap before finishing")

    emitted: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            emitted.append(self.format(record))

    repo = f"octo/grade-fail-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        app.state.grader = FailingGrader()
        handler = Capture()
        handler.setFormatter(JsonFormatter())
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            resp = _answer(client, token, 0, "anything")
        finally:
            root.removeHandler(handler)

    assert resp.status_code == 502
    failures = [line for line in emitted if '"outcome": "grade_failed"' in line]
    assert len(failures) == 1
    assert "16000-token cap" in failures[0]
    assert _marks(database_url, token) == {}  # nothing recorded on a failed grade


# --- model-spend guards ---------------------------------------------------


def test_repeat_explain_reuses_the_stored_explanation(grumpy_env) -> None:
    class CountingExplain(FakeGrader):
        calls = 0

        async def explain(self, interpretation, question):
            type(self).calls += 1
            return "a plain-language walkthrough"

    repo = f"octo/explain-twice-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        app.state.grader = CountingExplain()
        first = _explain(client, token, 0)
        second = _explain(client, token, 0)
        page = client.get(f"/s/{token}")

    assert first.status_code == second.status_code == 303
    assert CountingExplain.calls == 1
    assert "a plain-language walkthrough" in page.text


def test_concurrent_submits_for_one_screen_grade_once(grumpy_env) -> None:
    import asyncio

    import httpx

    release = asyncio.Event()

    class SlowGrader(FakeGrader):
        calls = 0

        async def grade_answer(self, interpretation, question, answer):
            type(self).calls += 1
            await release.wait()
            return QuestionMark(passed=False, note="no", model="m", prompt_version="v")

    repo = f"octo/inflight-{secrets.token_hex(4)}"

    async def _run() -> list[int]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                created = await client.post(
                    "/sessions",
                    json={
                        "repo": repo,
                        "pr_number": 1,
                        "head_sha": _VALID_SHA,
                        "base_sha": "2" * 40,
                        "diff": _DIFF,
                    },
                    headers=_headers(grumpy_env.token),
                )
                token = _token_from_url(created.json()["session_url"])
                app.state.grader = SlowGrader()
                form = {"index": "0", "answer": "some answer"}
                first = asyncio.create_task(client.post(f"/s/{token}/answer", data=form))
                while SlowGrader.calls == 0:
                    await asyncio.sleep(0)
                try:
                    # Without the in-flight guard this would also block on the slow grader.
                    second = await asyncio.wait_for(
                        client.post(f"/s/{token}/answer", data=form), timeout=5
                    )
                finally:
                    release.set()
                return [second.status_code, (await first).status_code]

    codes = asyncio.run(_run())

    assert SlowGrader.calls == 1
    assert codes == [303, 303]


# --- start over -----------------------------------------------------------


def _fail_session(client: TestClient, token: str) -> None:
    for i in (0, 1, 2):
        _skip_screen(client, token, i)


def _restart(client: TestClient, token: str):
    return client.post(f"/s/{token}/restart", follow_redirects=False)


def test_failed_page_offers_start_over_and_passed_does_not(grumpy_env) -> None:
    repo = f"octo/restart-btn-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        failed = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        _fail_session(client, failed)
        failed_page = client.get(f"/s/{failed}")

        other = f"octo/restart-btn-pass-{secrets.token_hex(4)}"
        passed = _token_from_url(_create_session(client, grumpy_env.token, repo=other)["session_url"])
        for i in range(3):
            _pass_screen(client, passed, i)
        passed_page = client.get(f"/s/{passed}")

    assert "Start over" in failed_page.text
    assert "Start over" not in passed_page.text


def test_start_over_resets_a_failed_session_to_pending(grumpy_env, database_url: str) -> None:
    repo = f"octo/restart-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        _fail_session(client, token)
        assert _status(database_url, token) == "failed"

        resp = _restart(client, token)
        page = client.get(f"/s/{token}")
        answered = _pass_screen(client, token, 0)

    assert resp.status_code == 303
    assert _status(database_url, token) == "pending"
    assert "Question 1 of 5" in page.text
    assert answered.status_code == 303
    assert _marks(database_url, token)["0"]["state"] == "passed"


def test_start_over_can_be_repeated_without_limit(grumpy_env, database_url: str) -> None:
    repo = f"octo/restart-many-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        for _ in range(5):
            _fail_session(client, token)
            assert _status(database_url, token) == "failed"
            _restart(client, token)
            assert _status(database_url, token) == "pending"


def test_start_over_is_a_no_op_unless_failed(grumpy_env, database_url: str) -> None:
    repo = f"octo/restart-noop-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        _pass_screen(client, token, 0)
        before = _marks(database_url, token)
        resp = _restart(client, token)

    assert resp.status_code == 303
    assert _marks(database_url, token) == before
    assert _status(database_url, token) == "pending"


def test_start_over_asks_for_questions_it_has_not_asked_before(grumpy_env) -> None:
    seen: list[list[str] | None] = []

    class RecordingGrader(FakeGrader):
        async def generate_exam(self, diff, count, avoid=None):
            seen.append(avoid)
            return await super().generate_exam(diff, count, avoid)

    repo = f"octo/restart-avoid-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        _fail_session(client, token)
        app.state.grader = RecordingGrader()
        _restart(client, token)

    assert seen and seen[0] and HIGH_LEVEL_QUESTION in seen[0]


def test_start_over_failure_is_502_and_leaves_the_session_failed(
    grumpy_env, database_url: str
) -> None:
    class CannotWrite(FakeGrader):
        async def generate_exam(self, diff, count, avoid=None):
            raise GradingError("model unavailable")

    repo = f"octo/restart-fail-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        token = _token_from_url(_create_session(client, grumpy_env.token, repo=repo)["session_url"])
        _fail_session(client, token)
        app.state.grader = CannotWrite()
        resp = _restart(client, token)

    assert resp.status_code == 502
    assert _status(database_url, token) == "failed"
