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

from app.answers import fetch_session_by_token, record_answer
from app.commit_status import CONTEXT, GitHubStatusPublisher, NullStatusPublisher
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
            "diff": "diff --git a/x b/x\n+hello\n",
        },
        headers={"Authorization": f"Bearer {token}"},
    )


def _session_token(session_url: str) -> str:
    return session_url.rsplit("/", 1)[-1]


def test_blank_status_token_means_nothing_is_posted(grumpy_env, monkeypatch) -> None:
    # Blank rather than deleted: it's what docker-compose's `${VAR:-}`
    # hands the app when .env doesn't set it, and it also keeps a
    # GITHUB_STATUS_TOKEN in a developer's local .env from leaking in.
    monkeypatch.setenv("GITHUB_STATUS_TOKEN", "")
    with TestClient(app):
        assert isinstance(app.state.status_publisher, NullStatusPublisher)


def test_status_token_enables_the_github_publisher(grumpy_env, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_STATUS_TOKEN", "gh-token")
    with TestClient(app):
        assert isinstance(app.state.status_publisher, GitHubStatusPublisher)


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


def test_passing_answer_posts_success(grumpy_env) -> None:
    repo = f"octo/status-pass-{secrets.token_hex(4)}"
    recorder = _RecordingPublisher()
    with TestClient(app) as client:
        app.state.status_publisher = recorder
        session_url = _create_session(client, grumpy_env.token, repo).json()["session_url"]
        client.post(
            f"/s/{_session_token(session_url)}/answer",
            data={"answer": "looks-good"},
            follow_redirects=False,
        )

    assert recorder.calls[-1] == {
        "repo": repo,
        "head_sha": _SHA,
        "status": "passed",
        "target_url": session_url,
    }


def test_wrong_answers_post_nothing_until_the_last_attempt_posts_failure(
    grumpy_env, monkeypatch
) -> None:
    monkeypatch.setenv("MAX_SESSION_ATTEMPTS", "2")
    repo = f"octo/status-fail-{secrets.token_hex(4)}"
    recorder = _RecordingPublisher()
    with TestClient(app) as client:
        app.state.status_publisher = recorder
        session_url = _create_session(client, grumpy_env.token, repo).json()["session_url"]
        token = _session_token(session_url)

        client.post(f"/s/{token}/answer", data={"answer": "no idea"}, follow_redirects=False)
        assert [call["status"] for call in recorder.calls] == ["pending"]

        client.post(f"/s/{token}/answer", data={"answer": "still no idea"}, follow_redirects=False)

    assert [call["status"] for call in recorder.calls] == ["pending", "failed"]


def test_record_answer_reports_the_stored_status_not_its_own_decision(grumpy_env) -> None:
    """A request that loses a race to an already-decided session must
    report what the session holds — that return value is what gets posted
    to GitHub — not the verdict it would have written."""
    repo = f"octo/status-race-{secrets.token_hex(4)}"
    with TestClient(app) as client:
        session_url = _create_session(client, grumpy_env.token, repo).json()["session_url"]
        token = _session_token(session_url)
        client.post(f"/s/{token}/answer", data={"answer": "looks-good"}, follow_redirects=False)

    async def _late_failing_answer() -> str:
        async with app.router.lifespan_context(app):
            session = await fetch_session_by_token(app.state.pool, token)
            return await record_answer(
                app.state.pool,
                session_id=session["id"],
                body="no idea",
                passed=False,
                max_session_attempts=1,
            )

    assert asyncio.run(_late_failing_answer()) == "passed"
