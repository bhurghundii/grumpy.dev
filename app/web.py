"""Developer-facing HTML routes: the exam sheet and its submission handler.
Unauthenticated by design — the token in the URL is the credential.
(Flagged in the phase 3 report: whether this should require proving PR
authorship is an open product question, not implemented here.)

A session carries a fixed set of scoped questions (app/grading.py generates
them at session creation; the first is always the high-level overview). The
developer answers them all on one page and submits once. The whole sheet is
marked in a single lenient pass, and the session passes when at least
PASSINGMARKS answers are correct.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.answers import count_answers, fetch_latest_answer, fetch_session_by_token, record_answer
from app.grading import GradingError
from app.logging_config import redact_session_token
from app.rendering import render_markdown
from app.sessions import build_session_url

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Grading reasoning, per-question notes, and the developer's own answer text
# are all rendered through this in the templates (`{{ ... |markdown }}`)
# rather than shown as preformatted plain text — see app/rendering.py for why
# this has to be sanitized, not just converted.
templates.env.filters["markdown"] = render_markdown

router = APIRouter()
logger = logging.getLogger("grumpy.web")

_NOT_FOUND = {"heading": "Not found", "message": "This link isn't valid."}
_EXPIRED = {
    "heading": "This session expired",
    "message": "Re-run the check on the PR to get a fresh link.",
}
_ALREADY_DECIDED = {
    "heading": "Already answered",
    "message": "This session already has a verdict and can't be answered again.",
}


def _classify_diff(diff: str) -> list[tuple[str, str]]:
    """One class each for added/removed/everything-else. No syntax
    highlighting and no collapsing — the full diff is printed bare above the
    exam sheet so the developer can read the change they're answering about."""
    lines = []
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            css = "add"
        elif line.startswith("-") and not line.startswith("---"):
            css = "remove"
        else:
            css = "context"
        lines.append((css, line))
    return lines


def _is_expired(session: dict) -> bool:
    return session["expires_at"] < datetime.now(UTC)


def _questions(session: dict) -> list[str]:
    """The exam sheet. Falls back to the single stored `question` for any
    pre-exam session row that predates the `questions` column."""
    questions = session.get("questions")
    if isinstance(questions, list) and questions:
        return [str(q) for q in questions]
    return [session["question"]]


def _form_js_active(value: object) -> bool:
    """The hidden `js_active` field set by app/static/nopaste.js. Read off a
    raw form (the answer count is dynamic, so the handler can't declare fixed
    Form params), so coerce the string here the way FastAPI's bool would."""
    return str(value).strip().lower() in ("true", "on", "1")


def _serialize_sheet(questions: list[str], answers: list[str], marks: list) -> str:
    """The answers row `body`: one JSON record per question, so a failed
    sheet can be shown back with each answer, its pass/fail, and the note,
    and refilled for a retry. JSON, not prose, because the row is read back
    and rendered field by field — never shown raw."""
    items = []
    for i, question in enumerate(questions):
        mark = marks[i] if i < len(marks) else None
        items.append(
            {
                "question": question,
                "answer": answers[i] if i < len(answers) else "",
                "passed": mark.passed if mark else False,
                "note": mark.note if mark else "",
            }
        )
    return json.dumps(items)


def _parse_sheet(body: str | None) -> list[dict]:
    """Inverse of _serialize_sheet, tolerant of a row that isn't the JSON
    shape (there are none in a fresh deployment, but a hand-written row or a
    future format change shouldn't 500 the result page)."""
    if not body:
        return []
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _log_if_unguarded(request: Request, session: dict, js_active: bool) -> None:
    """A submission without `js_active` came from a page where
    app/static/nopaste.js never ran — JavaScript off, the script blocked,
    or no browser at all (curl) — so pasting wasn't blocked for it. Logged,
    not refused: the guard is friction, and grading is what decides."""
    if js_active:
        return
    logger.warning(
        "submitted without paste guard",
        extra={
            "repo": session["repo"],
            "pr_number": session["pr_number"],
            "outcome": "no_js",
            "path": redact_session_token(request.url.path),
        },
    )


def _log_grading_failure(session: dict, *, outcome: str) -> None:
    """A GradingError reaches the developer as a generic "please try again"
    with no explanation — deliberately, since the cause is a model-side
    detail they can do nothing about. This is the only record of why it
    happened, so it carries the traceback. Call from inside the `except`
    block — exc_info picks up the exception being handled."""
    logger.warning(
        "grading call failed",
        extra={
            "repo": session["repo"],
            "pr_number": session["pr_number"],
            "head_sha": session["head_sha"],
            "outcome": outcome,
        },
        exc_info=True,
    )


async def _exam_context(
    pool,
    settings,
    session: dict,
    *,
    error: str | None = None,
    drafts: list[str] | None = None,
) -> dict:
    """The exam.html context. On a retry (a failed-but-not-terminal sheet)
    the previous answers and their per-question notes are shown back, plus
    the overall reasoning banner — a session can't be told apart as "never
    answered" vs "answered wrong, can retry" by status alone (both are
    'pending'), so the latest answer row is what distinguishes them."""
    questions = _questions(session)
    latest = await fetch_latest_answer(pool, session["id"])
    attempts_used = await count_answers(pool, session["id"])
    prior = {item["question"]: item for item in _parse_sheet(latest["body"])} if latest else {}

    rows = []
    for i, question in enumerate(questions):
        previous = prior.get(question)
        draft = drafts[i] if drafts and i < len(drafts) else None
        rows.append(
            {
                "index": i,
                "question": question,
                "answer_body": draft if draft is not None else (previous["answer"] if previous else ""),
                "passed": previous["passed"] if previous else None,
                "note": previous["note"] if previous else None,
            }
        )

    return {
        "token": session["token"],
        "repo": session["repo"],
        "pr_number": session["pr_number"],
        "diff_lines": _classify_diff(session["diff"]),
        "questions": rows,
        "error": error,
        "previous_reasoning": latest["reasoning"] if latest and latest["passed"] is False else None,
        "attempts_used": attempts_used,
        "max_session_attempts": settings.max_session_attempts,
        "passingmarks": settings.passing_marks_for(len(questions)),
    }


@router.get("/s/{token}")
async def view_session(request: Request, token: str) -> HTMLResponse:
    pool = request.app.state.pool
    settings = request.app.state.settings
    session = await fetch_session_by_token(pool, token)

    if session is None:
        return templates.TemplateResponse(request, "error.html", _NOT_FOUND, status_code=404)

    if session["status"] in ("passed", "failed"):
        answer = await fetch_latest_answer(pool, session["id"])
        return templates.TemplateResponse(
            request,
            "result.html",
            {
                "repo": session["repo"],
                "pr_number": session["pr_number"],
                "diff_lines": _classify_diff(session["diff"]),
                "status": session["status"],
                "items": _parse_sheet(answer["body"]) if answer else [],
                "reasoning": answer["reasoning"] if answer else None,
            },
        )

    if _is_expired(session):
        return templates.TemplateResponse(request, "error.html", _EXPIRED, status_code=410)

    context = await _exam_context(pool, settings, session)
    return templates.TemplateResponse(request, "exam.html", context)


@router.post("/s/{token}/submit")
async def submit_exam(request: Request, token: str) -> HTMLResponse:
    pool = request.app.state.pool
    settings = request.app.state.settings
    session = await fetch_session_by_token(pool, token)

    if session is None:
        return templates.TemplateResponse(request, "error.html", _NOT_FOUND, status_code=404)

    if session["status"] in ("passed", "failed"):
        return templates.TemplateResponse(
            request, "error.html", _ALREADY_DECIDED, status_code=409
        )

    if _is_expired(session):
        return templates.TemplateResponse(request, "error.html", _EXPIRED, status_code=410)

    questions = _questions(session)
    form = await request.form()
    answers = [str(form.get(f"answer_{i}", "")) for i in range(len(questions))]
    js_active = _form_js_active(form.get("js_active"))

    if any(not a.strip() for a in answers):
        context = await _exam_context(
            pool,
            settings,
            session,
            error="Please answer every question before submitting.",
            drafts=answers,
        )
        return templates.TemplateResponse(request, "exam.html", context, status_code=422)

    # One cap over the whole sheet (every answer, concatenated) — bounds
    # Postgres storage and the grading call's input regardless of how the
    # bytes are split across questions.
    if sum(len(a.encode("utf-8")) for a in answers) > settings.max_answer_bytes:
        context = await _exam_context(
            pool,
            settings,
            session,
            error=f"Your answers are too long (max {settings.max_answer_bytes} bytes total) — please shorten them and resubmit.",
            drafts=answers,
        )
        return templates.TemplateResponse(request, "exam.html", context, status_code=422)

    grader = request.app.state.grader
    try:
        result = await grader.grade_exam(session["diff"], questions, answers)
    except GradingError:
        # Pending-equivalent error: no answers row, no status change — the
        # developer can resubmit. Must never silently pass or fail.
        _log_grading_failure(session, outcome="grade_failed")
        context = await _exam_context(
            pool,
            settings,
            session,
            error="Grading failed — please try submitting your answers again.",
            drafts=answers,
        )
        return templates.TemplateResponse(request, "exam.html", context, status_code=502)

    passed = result.score >= settings.passing_marks_for(len(questions))

    _log_if_unguarded(request, session, js_active)
    status = await record_answer(
        pool,
        session_id=session["id"],
        body=_serialize_sheet(questions, answers, result.marks),
        passed=passed,
        max_session_attempts=settings.max_session_attempts,
        model=result.model,
        prompt_version=result.prompt_version,
        reasoning=result.reasoning,
        js_active=js_active,
    )

    # Only a terminal verdict is posted to GitHub. A failing sheet with
    # attempts left stays pending — already what GitHub shows, nothing to post.
    if status != "pending":
        await request.app.state.status_publisher.publish(
            repo=session["repo"],
            head_sha=session["head_sha"],
            status=status,
            target_url=build_session_url(settings.grumpy_base_url, token),
        )

    # Post/redirect/get: a refresh after this re-fetches the result, not
    # resubmits the sheet.
    return RedirectResponse(url=f"/s/{token}", status_code=303)
