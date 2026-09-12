"""Idempotent session creation.

Two concurrent Action runs on the same head SHA are the normal case, not an
edge case — this uses INSERT ... ON CONFLICT DO NOTHING RETURNING *, falling
back to a SELECT only when the insert is skipped. Never select-then-insert:
that has a check-then-act gap and produces duplicate sessions (different
tokens) for the same SHA under real concurrency.

Why the fallback SELECT is safe: Postgres's conflict check on the unique
index blocks a second INSERT if another transaction is concurrently
inserting the same key and hasn't committed yet. By the time our INSERT
returns with no row (conflict, skipped), the winning transaction has
already committed, so the subsequent SELECT is guaranteed to find it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

_INSERT_SQL = """
    INSERT INTO sessions
        (repo, pr_number, head_sha, base_sha, diff, question, token, status, expires_at)
    VALUES
        (%(repo)s, %(pr_number)s, %(head_sha)s, %(base_sha)s, %(diff)s,
         %(question)s, %(token)s, 'pending', %(expires_at)s)
    ON CONFLICT (repo, pr_number, head_sha) DO NOTHING
    RETURNING *
"""

_SELECT_SQL = """
    SELECT * FROM sessions
    WHERE repo = %(repo)s AND pr_number = %(pr_number)s AND head_sha = %(head_sha)s
"""


async def create_or_get_session(
    pool: AsyncConnectionPool,
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    base_sha: str,
    diff: str,
    question: str,
    token: str,
    ttl_days: int,
) -> tuple[dict[str, Any], bool]:
    """Returns (session_row, created). created is True only if this call's
    insert won the race; False means a session already existed (created by
    a prior request or a concurrent one that committed first)."""
    expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    params = {
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "diff": diff,
        "question": question,
        "token": token,
        "expires_at": expires_at,
    }

    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_INSERT_SQL, params)
            row = await cur.fetchone()
            if row is not None:
                return row, True

            await cur.execute(
                _SELECT_SQL,
                {"repo": repo, "pr_number": pr_number, "head_sha": head_sha},
            )
            row = await cur.fetchone()
            if row is None:
                # Unreachable in practice: ON CONFLICT DO NOTHING only skips
                # the insert when a conflicting row already exists.
                raise RuntimeError("session vanished between insert and select")
            return row, False
