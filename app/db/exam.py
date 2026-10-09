"""Per-screen state for the paged walkthrough, kept in sessions.marks by question index.

An entry is {"state": "open" | "passed" | "skipped", "attempts": [...],
"revealed_answer", "explanation"}. A correct answer resolves a question as
passed; running out of tries reveals the answer and resolves it as skipped,
which counts neither way. The user can also skip a question outright, which
reveals the answer and resolves it the same way (flagged by_user). The verdict is passed once PASSINGMARKS questions
are passed, failed once that is unreachable, and pending otherwise.

Writes run under SELECT ... FOR UPDATE and skip resolved questions and
decided sessions, so a double-submit is a no-op.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

_RESOLVED = ("passed", "skipped")

_SELECT_FOR_UPDATE_SQL = "SELECT marks, status FROM sessions WHERE id = %(id)s FOR UPDATE"
_UPDATE_SQL = "UPDATE sessions SET marks = %(marks)s, status = %(status)s WHERE id = %(id)s"
_UPDATE_MARKS_ONLY_SQL = "UPDATE sessions SET marks = %(marks)s WHERE id = %(id)s"


def verdict_for(marks: dict[str, Any], question_count: int, passing_marks: int) -> str:
    """The session status implied by the marks so far. Pure, so routes can reuse it."""
    passed = sum(1 for m in marks.values() if m.get("state") == "passed")
    resolved = sum(1 for m in marks.values() if m.get("state") in _RESOLVED)
    still_open = question_count - resolved
    if passed >= passing_marks:
        return "passed"
    if passed + still_open < passing_marks:
        return "failed"
    return "pending"


def _entry(marks: dict[str, Any], key: str) -> dict[str, Any]:
    return marks.get(key) or {
        "state": "open",
        "attempts": [],
        "revealed_answer": None,
        "explanation": None,
    }


async def record_attempt(
    pool: AsyncConnectionPool,
    *,
    session_id: UUID,
    index: int,
    answer: str,
    passed: bool,
    note: str,
    js_active: bool | None,
    reference_answer: str,
    max_attempts: int,
    question_count: int,
    passing_marks: int,
) -> tuple[str, bool]:
    """Record one attempt and return (resulting status, written). written is False
    when the session was already decided or the question already resolved."""
    key = str(index)
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_SELECT_FOR_UPDATE_SQL, {"id": session_id})
            row = await cur.fetchone()
            marks = dict(row["marks"] or {})

            if row["status"] != "pending":
                return row["status"], False
            entry = _entry(marks, key)
            if entry["state"] in _RESOLVED:
                return row["status"], False

            entry["attempts"].append(
                {"answer": answer, "passed": passed, "note": note, "js_active": js_active}
            )
            if passed:
                entry["state"] = "passed"
            elif len(entry["attempts"]) >= max_attempts:
                entry["state"] = "skipped"
                entry["revealed_answer"] = reference_answer
            marks[key] = entry

            status = verdict_for(marks, question_count, passing_marks)
            await cur.execute(
                _UPDATE_SQL, {"marks": Jsonb(marks), "status": status, "id": session_id}
            )
            return status, True


async def skip_question(
    pool: AsyncConnectionPool,
    *,
    session_id: UUID,
    index: int,
    reference_answer: str,
    question_count: int,
    passing_marks: int,
) -> tuple[str, bool]:
    """Fail a question on the user's request: reveal the answer and resolve it as
    skipped (by_user). Returns (resulting status, written); a no-op when the session
    is decided or the question already resolved."""
    key = str(index)
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_SELECT_FOR_UPDATE_SQL, {"id": session_id})
            row = await cur.fetchone()
            marks = dict(row["marks"] or {})

            if row["status"] != "pending":
                return row["status"], False
            entry = _entry(marks, key)
            if entry["state"] in _RESOLVED:
                return row["status"], False

            entry["state"] = "skipped"
            entry["revealed_answer"] = reference_answer
            entry["by_user"] = True
            marks[key] = entry

            status = verdict_for(marks, question_count, passing_marks)
            await cur.execute(
                _UPDATE_SQL, {"marks": Jsonb(marks), "status": status, "id": session_id}
            )
            return status, True


async def record_explanation(
    pool: AsyncConnectionPool, *, session_id: UUID, index: int, explanation: str
) -> bool:
    """Store an on-demand explanation. Never resolves the question or changes the
    verdict; a no-op once resolved or decided."""
    key = str(index)
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_SELECT_FOR_UPDATE_SQL, {"id": session_id})
            row = await cur.fetchone()
            marks = dict(row["marks"] or {})

            if row["status"] != "pending":
                return False
            entry = _entry(marks, key)
            if entry["state"] in _RESOLVED:
                return False

            entry["explanation"] = explanation
            marks[key] = entry
            await cur.execute(_UPDATE_MARKS_ONLY_SQL, {"marks": Jsonb(marks), "id": session_id})
            return True
