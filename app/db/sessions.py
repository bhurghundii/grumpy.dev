"""Idempotent session creation, and lookup by public token.

One INSERT ... ON CONFLICT ... RETURNING *, with a SELECT fallback only when
the insert is skipped. Never select-then-insert: concurrent runs on the same
head SHA would create duplicate sessions. The fallback is safe because the
winning transaction has committed by the time the conflicting insert returns.

An expired pending session is re-issued a fresh token and expiry in place, so
re-running the check doesn't leave the PR stuck at pending. The old link stops
working.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

_INSERT_SQL = """
    INSERT INTO sessions
        (repo, pr_number, head_sha, base_sha, diff, question, questions,
         interpretation, token, status, expires_at)
    VALUES
        (%(repo)s, %(pr_number)s, %(head_sha)s, %(base_sha)s, %(diff)s,
         %(question)s, %(questions)s, %(interpretation)s, %(token)s,
         'pending', %(expires_at)s)
    ON CONFLICT (repo, pr_number, head_sha) DO UPDATE
        SET token = EXCLUDED.token, expires_at = EXCLUDED.expires_at
        WHERE sessions.status = 'pending' AND sessions.expires_at < now()
    RETURNING *
"""

_SELECT_SQL = """
    SELECT * FROM sessions
    WHERE repo = %(repo)s AND pr_number = %(pr_number)s AND head_sha = %(head_sha)s
"""


_SELECT_BY_TOKEN_SQL = "SELECT * FROM sessions WHERE token = %(token)s"


def build_session_url(base_url: str, token: str) -> str:
    """Built from the configured base URL, never the Host header (wrong behind a proxy)."""
    return f"{base_url.rstrip('/')}/s/{token}"


async def create_or_get_session(
    pool: AsyncConnectionPool,
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    base_sha: str,
    diff: str,
    question: str,
    questions: list[dict[str, Any]],
    interpretation: str,
    token: str,
    ttl_days: int,
) -> tuple[dict[str, Any], bool]:
    """Returns (session_row, created). created is True for a new session or a
    re-issued expired one, False if a live session already existed. `questions`
    and `interpretation` are written only on insert, so reruns keep the original
    walkthrough."""
    expires_at = datetime.now(UTC) + timedelta(days=ttl_days)
    params = {
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "diff": diff,
        "question": question,
        "questions": Jsonb(questions),
        "interpretation": interpretation,
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
                # Unreachable in practice: the conflict clause only skips
                # the write when a conflicting row already exists.
                raise RuntimeError("session vanished between insert and select")
            return row, False


async def fetch_session_by_pr(
    pool: AsyncConnectionPool, *, repo: str, pr_number: int, head_sha: str
) -> dict[str, Any] | None:
    """The session for this (repo, PR, head SHA), if any. Lets POST /sessions skip
    the model calls for a session that already exists."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                _SELECT_SQL, {"repo": repo, "pr_number": pr_number, "head_sha": head_sha}
            )
            return await cur.fetchone()


async def fetch_session_by_token(pool: AsyncConnectionPool, token: str) -> dict[str, Any] | None:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_SELECT_BY_TOKEN_SQL, {"token": token})
            return await cur.fetchone()
