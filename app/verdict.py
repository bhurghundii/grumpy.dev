"""GET /verdict lookup.

Four states, deliberately not collapsed: UNKNOWN (no session row at all) is
different from PENDING (a session exists but hasn't been answered) — an
Action re-run after a force-push must see UNKNOWN and create a fresh
session, not read PENDING and block forever on a session for a SHA that no
longer exists.
"""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

_STATUS_TO_VERDICT = {
    "pending": "PENDING",
    "passed": "PASSED",
    "failed": "FAILED",
}

_SELECT_SQL = """
    SELECT status FROM sessions
    WHERE repo = %(repo)s AND pr_number = %(pr_number)s AND head_sha = %(head_sha)s
"""


async def fetch_verdict(
    pool: AsyncConnectionPool, *, repo: str, pr_number: int, head_sha: str
) -> str:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                _SELECT_SQL, {"repo": repo, "pr_number": pr_number, "head_sha": head_sha}
            )
            row = await cur.fetchone()

    if row is None:
        return "UNKNOWN"
    return _STATUS_TO_VERDICT[row["status"]]
