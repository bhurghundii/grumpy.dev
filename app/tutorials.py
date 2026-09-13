"""DB access for the tutorial-breakdown flow: generate (and later grade
the explain-back for) a step-by-step walkthrough of a diff, offered after
a wrong-but-not-yet-terminal answer. Mirrors app/answers.py's structure
and trade-offs.

A session has at most one "in progress" (unexplained) tutorial row at a
time — enforced by migrations/V4__tutorials.sql's partial unique index
(tutorials_session_in_progress_key) as the durable backstop behind
app/web.py's cheaper app-level check.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

_LATEST_TUTORIAL_SQL = """
    SELECT * FROM tutorials WHERE session_id = %(session_id)s
    ORDER BY created_at DESC LIMIT 1
"""

_COUNT_TUTORIALS_SQL = "SELECT count(*) FROM tutorials WHERE session_id = %(session_id)s"
_COUNT_ANSWERS_SQL = "SELECT count(*) FROM answers WHERE session_id = %(session_id)s"

_INSERT_TUTORIAL_SQL = """
    INSERT INTO tutorials (session_id, breakdown, steps, model, prompt_version, created_at)
    VALUES (%(session_id)s, %(breakdown)s, %(steps)s, %(model)s, %(prompt_version)s, now())
    RETURNING id
"""

_UPDATE_EXPLANATION_SQL = """
    UPDATE tutorials
    SET explanation_body = %(explanation_body)s,
        passed = %(passed)s,
        reasoning = %(reasoning)s,
        model = %(model)s,
        prompt_version = %(prompt_version)s,
        explanation_js_active = %(js_active)s
    WHERE id = %(tutorial_id)s
"""

# Only ever transitions a session out of 'pending' — see
# app/answers.py:_UPDATE_STATUS_IF_PENDING_SQL for the same guard and why.
_UPDATE_STATUS_IF_PENDING_SQL = """
    UPDATE sessions SET status = %(status)s
    WHERE id = %(session_id)s AND status = 'pending'
"""


async def fetch_latest_tutorial(pool: AsyncConnectionPool, session_id: UUID) -> dict[str, Any] | None:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_LATEST_TUTORIAL_SQL, {"session_id": session_id})
            return await cur.fetchone()


async def count_tutorials(pool: AsyncConnectionPool, session_id: UUID) -> int:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_COUNT_TUTORIALS_SQL, {"session_id": session_id})
            (count,) = await cur.fetchone()
            return count


async def create_tutorial(
    pool: AsyncConnectionPool,
    *,
    session_id: UUID,
    breakdown: str,
    max_session_attempts: int,
    steps: list[dict[str, Any]] | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
) -> tuple[UUID, str]:
    """Inserts the tutorial row and, if this request exhausts the shared
    MAX_SESSION_ATTEMPTS budget (counting answers AND tutorials — see
    app/answers.py:record_answer for the symmetric counterpart), flips the
    session to terminal 'failed' in the same transaction.

    `breakdown` and `steps` are the same content in two forms — prose and
    structured — not two pieces of content; see
    migrations/V5__tutorial_steps.sql. `steps` is None only for a grader
    that produced no structured walkthrough, which the tutorial page
    renders through its prose fallback branch. Cap exhaustion
    must reliably stop all further spend even when the exhausting action
    is a tutorial request rather than a wrong answer — a tutorial can
    never itself pass a session, only exhaust it. Returns
    (tutorial_id, resulting_session_status).
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_COUNT_ANSWERS_SQL, {"session_id": session_id})
            answers_count = (await cur.fetchone())["count"]
            await cur.execute(_COUNT_TUTORIALS_SQL, {"session_id": session_id})
            tutorials_count = (await cur.fetchone())["count"]
            attempts_used = answers_count + tutorials_count + 1

            status = (
                "failed"
                if (max_session_attempts and attempts_used >= max_session_attempts)
                else "pending"
            )

            await cur.execute(
                _INSERT_TUTORIAL_SQL,
                {
                    "session_id": session_id,
                    "breakdown": breakdown,
                    "steps": Jsonb(steps) if steps else None,
                    "model": model,
                    "prompt_version": prompt_version,
                },
            )
            tutorial_id = (await cur.fetchone())["id"]

            if status == "failed":
                await cur.execute(
                    _UPDATE_STATUS_IF_PENDING_SQL, {"status": status, "session_id": session_id}
                )
    return tutorial_id, status


async def record_tutorial_explanation(
    pool: AsyncConnectionPool,
    *,
    tutorial_id: UUID,
    explanation_body: str,
    passed: bool,
    reasoning: str | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
    js_active: bool | None = None,
) -> None:
    """Never touches sessions.status — the explain-back result (pass or
    fail) always unlocks a fresh answer attempt; it never gates anything
    itself (see app/grading.py's _EXPLAIN_BACK_SYSTEM_PROMPT). `js_active`
    is stored only, as in app/answers.py:record_answer."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                _UPDATE_EXPLANATION_SQL,
                {
                    "tutorial_id": tutorial_id,
                    "explanation_body": explanation_body,
                    "passed": passed,
                    "reasoning": reasoning,
                    "model": model,
                    "prompt_version": prompt_version,
                    "js_active": js_active,
                },
            )
