"""The `grumpy/verdict` commit status: what GitHubStatusPublisher sends and
how it fails (mocked HTTP transport, no network), then where the app
calls it — POST /sessions and a graded answer that ends the session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db.exam import record_attempt
from app.db.sessions import fetch_session_by_token
from app.github.commit_status import CONTEXT, GitHubStatusPublisher, NullStatusPublisher
from app.main import app

_SHA = "a" * 40
_SESSION_URL = "https://grumpy.example.com/s/tok"


def _publisher(handler, **kwargs) -> GitHubStatusPublisher:
    transport = httpx.MockTransport(handler)
    return GitHubStatusPublisher(
        "gh-token", client_factory=lambda: httpx.AsyncClient(transport=transport), **kwargs
    )


def _publish(publisher: GitHubStatusPublisher, status: str) -> None:
    asyncio.run(
        publisher.publish(repo="octo/repo", head_sha=_SHA, status=status, target_url=_SESSION_URL)
    )


@pytest.mark.parametrize(
    ("status", "state"), [("pending", "pending"), ("passed", "success"), ("failed", "failure")]
)
def test_posts_the_mapped_state_to_the_head_commit(status: str, state: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={})

    _publish(_publisher(handler), status)

    (request,) = requests
    assert request.method == "POST"
    assert str(request.url) == f"https://api.github.com/repos/octo/repo/statuses/{_SHA}"
    assert request.headers["Authorization"] == "Bearer gh-token"
    body = json.loads(request.content)
    assert body["state"] == state
    assert body["context"] == CONTEXT
    assert body["target_url"] == _SESSION_URL
    assert 0 < len(body["description"]) <= 140


def test_enterprise_api_url_is_honoured_with_or_without_trailing_slash() -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(201, json={})

    _publish(_publisher(handler, api_url="https://ghe.example.com/api/v3/"), "pending")

    assert urls == [f"https://ghe.example.com/api/v3/repos/octo/repo/statuses/{_SHA}"]


def test_github_rejecting_the_status_is_logged_not_raised(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Resource not accessible"})

    with caplog.at_level(logging.WARNING, logger="grumpy.commit_status"):
        _publish(_publisher(handler), "passed")

    (record,) = caplog.records
    assert record.status_code == 403
    assert record.outcome == "error"


def test_network_failure_is_logged_not_raised(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with caplog.at_level(logging.WARNING, logger="grumpy.commit_status"):
        _publish(_publisher(handler), "passed")

    (record,) = caplog.records
    assert record.outcome == "error"
    assert "gh-token" not in caplog.text


# --- wiring: which publisher the app builds, and when it's called ---


class _RecordingPublisher:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def publish(self, **kwargs) -> None:
        self.calls.append(kwargs)


def _create_session(client: TestClient, token: str, repo: str):
    return client.post(
        "/sessions",
        json={
            "repo": repo,
            "pr_number": 1,
            "head_sha": _SHA,
            "base_sha": "b" * 40,
            "diff": "diff --git a/x b/x\n@@ -0,0 +1,60 @@\n" + "+hello\n" * 60,
        },
        headers={"Authorization": f"Bearer {token}"},
    )


def _session_token(session_url: str) -> str:
    return session_url.rsplit("/", 1)[-1]


def _answer(client, token: str, index: int, text: str):
    return client.post(
        f"/s/{token}/answer", data={"index": str(index), "answer": text}, follow_redirects=False
    )


def _pass_screen(client, token: str, index: int) -> None:
    _answer(client, token, index, "looks-good")


def _skip_screen(client, token: str, index: int, attempts: int = 3) -> None:
    """Exhaust a screen's tries (default MAX_QUESTION_ATTEMPTS) so it resolves
    'skipped'. FakeGrader fails anything without the marker."""
    for _ in range(attempts):
        _answer(client, token, index, "nope")


def test_blank_status_token_means_nothing_is_posted(grumpy_env, monkeypatch, capsys) -> None:
    # Blank rather than deleted: it's what docker-compose's `${VAR:-}`
    # hands the app when .env doesn't set it, and it also keeps a
    # GITHUB_STATUS_TOKEN in a developer's local .env from leaking in.
    monkeypatch.setenv("GITHUB_STATUS_TOKEN", "")
    with TestClient(app):
        assert isinstance(app.state.status_publisher, NullStatusPublisher)

    # Read off stdout, not caplog: configure_logging() replaces
    # root.handlers during startup and removes caplog's handler with them,
    # and this line is emitted during that same startup, so a handler
    # attached afterwards is too late to see it either.
    #
    # It has to be said out loud because the publisher never will —
    # NullStatusPublisher posts nothing and logs nothing, so this is the
    # only thing separating "token wasn't loaded" from "GitHub refused it".
    assert '"outcome": "disabled"' in capsys.readouterr().out, (
        "a deployment with no status token has to say so at startup"
    )


def test_status_token_enables_the_github_publisher(grumpy_env, monkeypatch, capsys) -> None:
    monkeypatch.setenv("GITHUB_STATUS_TOKEN", "gh-token")
    with TestClient(app):
        assert isinstance(app.state.status_publisher, GitHubStatusPublisher)

    assert '"outcome": "enabled"' in capsys.readouterr().out


def test_create_session_posts_pending_and_a_repeat_posts_again(grumpy_env) -> None:
    repo = f"octo/status-create-{secrets.token_hex(4)}"
    recorder = _RecordingPublisher()
    with TestClient(app) as client:
        app.state.status_publisher = recorder
        first = _create_session(client, grumpy_env.token, repo)
        second = _create_session(client, grumpy_env.token, repo)

    expected = {
        "repo": repo,
        "head_sha": _SHA,
        "status": "pending",
        "target_url": first.json()["session_url"],
    }
    assert (first.status_code, second.status_code) == (201, 200)
    assert recorder.calls == [expected, expected]


def test_passing_the_walkthrough_posts_success(grumpy_env) -> None:
    repo = f"octo/status-pass-{secrets.token_hex(4)}"
    recorder = _RecordingPublisher()
    with TestClient(app) as client:
        app.state.status_publisher = recorder
        session_url = _create_session(client, grumpy_env.token, repo).json()["session_url"]
        token = _session_token(session_url)
        for i in (0, 1, 2):  # 3 of 5 correct -> pass
            _pass_screen(client, token, i)

    # pending on creation, then success once the third correct answer lands.
    assert [call["status"] for call in recorder.calls] == ["pending", "passed"]
    assert recorder.calls[-1]["target_url"] == session_url


def test_screens_post_nothing_until_failure_becomes_certain(grumpy_env) -> None:
    repo = f"octo/status-fail-{secrets.token_hex(4)}"
    recorder = _RecordingPublisher()
    with TestClient(app) as client:
        app.state.status_publisher = recorder
        session_url = _create_session(client, grumpy_env.token, repo).json()["session_url"]
        token = _session_token(session_url)

        _skip_screen(client, token, 0)  # 1 skipped, 4 still open -> reachable
        _skip_screen(client, token, 1)  # 2 skipped, 3 still open -> reachable
        assert [call["status"] for call in recorder.calls] == ["pending"]

        _skip_screen(client, token, 2)  # 3 skipped, only 2 left -> unreachable

    assert [call["status"] for call in recorder.calls] == ["pending", "failed"]


def test_record_attempt_on_a_decided_session_is_a_noop(grumpy_env) -> None:
    """Once the session is decided, a late or racing screen must report the
    stored verdict and write nothing — never overwrite a passed session."""
    repo = f"octo/status-race-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        session_url = _create_session(client, grumpy_env.token, repo).json()["session_url"]
        token = _session_token(session_url)
        for i in (0, 1, 2):
            _pass_screen(client, token, i)  # passed

    async def _late_attempt() -> tuple[str, bool]:
        async with app.router.lifespan_context(app):
            session = await fetch_session_by_token(app.state.pool, token)
            return await record_attempt(
                app.state.pool,
                session_id=session["id"],
                index=4,
                answer="no idea",
                passed=False,
                note="late",
                js_active=None,
                reference_answer="ref",
                max_attempts=3,
                question_count=5,
                passing_marks=3,
            )

    assert asyncio.run(_late_attempt()) == ("passed", False)
