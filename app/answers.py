"""DB access for the answer-submission flow: look up a session by its
public token, and record a graded answer against it.

record_answer() is a plain insert + update, not an atomic conditional
transition — unlike phase 2's session-creation idempotency (which the spec
calls "the normal case" under real concurrent Action runs), a human
double-clicking submit at the exact same instant is a rare enough edge case
that the calling route's plain check-then-act (see app/web.py) is enough
for phase 3. Noted as a deliberate MVP trade-off, not an oversight.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

_SELECT_BY_TOKEN_SQL = "SELECT * FROM sessions WHERE token = %(token)s"

_LATEST_ANSWER_SQL = """
    SELECT * FROM answers WHERE session_id = %(session_id)s
    ORDER BY created_at DESC LIMIT 1
"""

_INSERT_ANSWER_SQL = """
    INSERT INTO answers (session_id, body, passed, model, prompt_version, reasoning, created_at)
    VALUES (%(session_id)s, %(body)s, %(passed)s, %(model)s, %(prompt_version)s, %(reasoning)s, now())
"""

_UPDATE_STATUS_SQL = "UPDATE sessions SET status = %(status)s WHERE id = %(session_id)s"


async def fetch_session_by_token(pool: AsyncConnectionPool, token: str) -> dict[str, Any] | None:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_SELECT_BY_TOKEN_SQL, {"token": token})
            return await cur.fetchone()


async def fetch_latest_answer(pool: AsyncConnectionPool, session_id: UUID) -> dict[str, Any] | None:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_LATEST_ANSWER_SQL, {"session_id": session_id})
            return await cur.fetchone()


async def record_answer(
    pool: AsyncConnectionPool,
    *,
    session_id: UUID,
    body: str,
    passed: bool,
    model: str | None = None,
    prompt_version: str | None = None,
    reasoning: str | None = None,
) -> None:
    status = "passed" if passed else "failed"
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                _INSERT_ANSWER_SQL,
                {
                    "session_id": session_id,
                    "body": body,
                    "passed": passed,
                    "model": model,
                    "prompt_version": prompt_version,
                    "reasoning": reasoning,
                },
            )
            await cur.execute(_UPDATE_STATUS_SQL, {"status": status, "session_id": session_id})
