"""DB access for the answer-submission flow: look up a session by its
public token, and record a graded answer against it.

record_answer() counts-then-decides-then-writes inside a single
transaction (see its docstring), but that's still not a fully atomic
conditional transition against a second, truly concurrent request for the
same session — unlike phase 2's session-creation idempotency (which the
spec calls "the normal case" under real concurrent Action runs), a human
double-clicking submit at the exact same instant is a rare enough edge
case that this is enough: the final UPDATE is guarded by
`WHERE status = 'pending'` so a losing writer can never clobber a session
another request already finalized, and the worst case is one extra unit
of MAX_SESSION_ATTEMPTS budget consumed, never a false pass. Noted as a
deliberate MVP trade-off, not an oversight.
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

_COUNT_ANSWERS_SQL = "SELECT count(*) FROM answers WHERE session_id = %(session_id)s"
_COUNT_TUTORIALS_SQL = "SELECT count(*) FROM tutorials WHERE session_id = %(session_id)s"

_INSERT_ANSWER_SQL = """
    INSERT INTO answers (session_id, body, passed, model, prompt_version, reasoning, js_active, created_at)
    VALUES (%(session_id)s, %(body)s, %(passed)s, %(model)s, %(prompt_version)s, %(reasoning)s, %(js_active)s, now())
"""

# Only ever transitions a session out of 'pending' — a request that loses
# a race against another write for the same session (already flipped to
# 'passed'/'failed') is a no-op here, not an overwrite.
_UPDATE_STATUS_IF_PENDING_SQL = """
    UPDATE sessions SET status = %(status)s
    WHERE id = %(session_id)s AND status = 'pending'
"""

_SELECT_STATUS_SQL = "SELECT status FROM sessions WHERE id = %(session_id)s"


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


async def count_answers(pool: AsyncConnectionPool, session_id: UUID) -> int:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_COUNT_ANSWERS_SQL, {"session_id": session_id})
            (count,) = await cur.fetchone()
            return count


async def record_answer(
    pool: AsyncConnectionPool,
    *,
    session_id: UUID,
    body: str,
    passed: bool,
    max_session_attempts: int,
    model: str | None = None,
    prompt_version: str | None = None,
    reasoning: str | None = None,
    js_active: bool | None = None,
) -> str:
    """Inserts the answer row and decides the resulting session status in
    the same transaction as the write, counting existing answers AND
    tutorials — the MAX_SESSION_ATTEMPTS budget is shared between the two
    action types (see app/config.py and app/tutorials.py's symmetric
    create_tutorial). A passing answer always sets 'passed'. A failing
    answer sets 'failed' only once max_session_attempts is exhausted (0
    means never, i.e. unlimited retries); otherwise the session stays
    'pending' so /verdict keeps reporting PENDING and the developer can
    retry.

    Returns the status the session actually holds afterwards, read back
    rather than taken from the decision above: if a concurrent request
    already finalized the session, the guarded UPDATE is a no-op and this
    request's own decision never landed. app/web.py reports the return
    value to GitHub, where a losing request's stale "failed" would
    otherwise overwrite the winner's "passed".

    `js_active` (whether the paste guard was running) is stored alongside,
    never consulted here — see migrations/V6__paste_guard.sql.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_COUNT_ANSWERS_SQL, {"session_id": session_id})
            (answers_count,) = await cur.fetchone()
            await cur.execute(_COUNT_TUTORIALS_SQL, {"session_id": session_id})
            (tutorials_count,) = await cur.fetchone()
            attempts_used = answers_count + tutorials_count + 1

            if passed:
                status = "passed"
            elif max_session_attempts and attempts_used >= max_session_attempts:
                status = "failed"
            else:
                status = "pending"

            await cur.execute(
                _INSERT_ANSWER_SQL,
                {
                    "session_id": session_id,
                    "body": body,
                    "passed": passed,
                    "model": model,
                    "prompt_version": prompt_version,
                    "reasoning": reasoning,
                    "js_active": js_active,
                },
            )
            await cur.execute(
                _UPDATE_STATUS_IF_PENDING_SQL, {"status": status, "session_id": session_id}
            )
            await cur.execute(_SELECT_STATUS_SQL, {"session_id": session_id})
            (stored_status,) = await cur.fetchone()
    return stored_status
