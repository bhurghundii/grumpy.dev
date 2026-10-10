"""Two concurrent POST /sessions for the same (repo, pr_number, head_sha)
must produce exactly one row and return the same session_url — the normal
case for grumpy (two Action runs racing on the same head SHA), not an edge
case.

Run genuinely concurrently via asyncio.gather, not sequentially: a
sequential test also passes against a select-then-insert implementation and
proves nothing about the race this is meant to catch.

fastapi.testclient.TestClient is sync and thread-backed, which can't
reliably prove two requests actually overlapped. Instead this drives the
app's real lifespan directly (the same async context manager FastAPI calls
internally) and fires two requests through httpx.ASGITransport inside one
asyncio.gather call.
"""

from __future__ import annotations

import asyncio
import secrets

import httpx
import psycopg

from app.main import app


def test_concurrent_session_creation_is_idempotent(grumpy_env, database_url: str) -> None:
    repo = f"octo/race-{secrets.token_hex(4)}"
    pr_number = 99
    head_sha = "d4" * 20
    body = {
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "base_sha": "e5" * 20,
        "diff": "diff --git a/x b/x\n+hello\n",
    }
    headers = {"Authorization": f"Bearer {grumpy_env.token}"}

    async def _run() -> tuple[httpx.Response, httpx.Response]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await asyncio.gather(
                    client.post("/sessions", json=body, headers=headers),
                    client.post("/sessions", json=body, headers=headers),
                )

    first, second = asyncio.run(_run())

    assert {first.status_code, second.status_code} == {200, 201}
    assert first.json()["session_url"] == second.json()["session_url"]

    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT count(*) FROM sessions WHERE repo = %s AND pr_number = %s AND head_sha = %s",
            (repo, pr_number, head_sha),
        ).fetchone()
    assert row is not None
    assert row[0] == 1


def test_rerunning_for_the_same_sha_makes_no_further_model_calls(grumpy_env) -> None:
    from fastapi.testclient import TestClient

    from app.ai.grading import FakeGrader

    class CountingGrader(FakeGrader):
        interpret_calls = 0
        exam_calls = 0

        async def interpret(self, diff):
            type(self).interpret_calls += 1
            return await super().interpret(diff)

        async def generate_exam(self, diff, count):
            type(self).exam_calls += 1
            return await super().generate_exam(diff, count)

    body = {
        "repo": f"octo/rerun-{secrets.token_hex(4)}",
        "pr_number": 7,
        "head_sha": "ab" * 20,
        "base_sha": "cd" * 20,
        "diff": "diff --git a/x b/x\n+hello\n",
    }
    headers = {"Authorization": f"Bearer {grumpy_env.token}"}

    with TestClient(app) as client:
        app.state.grader = CountingGrader()
        first = client.post("/sessions", json=body, headers=headers)
        second = client.post("/sessions", json=body, headers=headers)

    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["session_url"] == second.json()["session_url"]
    assert CountingGrader.interpret_calls == 1
    assert CountingGrader.exam_calls == 1
