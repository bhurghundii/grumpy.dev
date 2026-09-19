"""Developer-facing HTML routes: the answer page and its submission
handler, plus the tutorial-breakdown flow offered alongside it.
Unauthenticated by design — the token in the URL is the credential.
(Flagged in the phase 3 report: whether this should require proving PR
authorship is an open product question, not implemented here.)
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from psycopg.errors import UniqueViolation

from app.answers import count_answers, fetch_latest_answer, fetch_session_by_token, record_answer
from app.grading import GradingError
from app.logging_config import redact_session_token
from app.rendering import render_markdown
from app.sessions import build_session_url
from app.tutorials import (
    count_tutorials,
    create_tutorial,
    fetch_latest_tutorial,
    record_tutorial_explanation,
)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Tutorial breakdown, grading reasoning, and the developer's own answer
# text are all rendered through this in the templates (`{{ ... |markdown
# }}`) rather than shown as preformatted plain text — see
# app/rendering.py for why this has to be sanitized, not just converted.
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
_TUTORIAL_DISABLED = {
    "heading": "Tutorial breakdown not available",
    "message": "This deployment doesn't offer tutorial breakdowns.",
}
_NO_TUTORIAL_IN_PROGRESS = {
    "heading": "No tutorial in progress",
    "message": "Request a tutorial breakdown first, then explain it back.",
}

# Sentinel for _resolve_step: re-render the walkthrough at its final step,
# which is the only step carrying the explain-back form — so an error on
# that form comes back with the form (and what was typed into it) still on
# screen, rather than dropping the developer at step 1.
_LAST_STEP = "last"


def _classify_diff(diff: str) -> list[tuple[str, str]]:
    """One class each for added/removed/everything-else. No syntax
    highlighting and no collapsing — phase 5 polish at the earliest. (A
    line-number gutter does exist now, but only on the per-step slices the
    tutorial walkthrough shows; the full diff is still printed bare.)

    Indexing matters beyond this function: position in the returned list is
    the 1-based line number a tutorial step's anchor refers to, which is
    the same basis app/grading.py:_number_diff shows the model.
    """
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


def _slice_diff(
    diff_lines: list[tuple[str, str]], start: int | None, end: int | None
) -> list[tuple[int, str, str]]:
    """The lines one tutorial step is about, as (line number, css, text).
    Bounds are 1-based and inclusive, and re-clamped here rather than
    trusted: they reach this point from model output, and a slice that
    silently ran off the end would be worse than a short one."""
    if not start or not end:
        return []
    lo, hi = max(start, 1), min(end, len(diff_lines))
    return [(n, css, line) for n, (css, line) in enumerate(diff_lines[lo - 1 : hi], start=lo)]


def _file_for_line(diff_lines: list[tuple[str, str]], line_number: int | None) -> str | None:
    """The file a step's slice *starts* in: the nearest `+++ b/` or
    `diff --git` header at or above it. Derived from the diff rather than
    asked of the model — the diff already states it unambiguously, and one
    less field to invent is one less field to validate. `+++ /dev/null`
    (a deletion) deliberately doesn't match, so the scan falls through to
    the `diff --git` line, which still names the file.

    A range spanning two files is captioned with the first, which is why
    _TUTORIAL_SYSTEM_PROMPT asks for tight ranges: a step covering half of
    one file and half of the next has a bigger problem than its caption."""
    if not line_number:
        return None
    for _, line in reversed(diff_lines[:line_number]):
        if line.startswith("+++ b/"):
            return line[len("+++ b/") :].strip() or None
        if line.startswith("diff --git ") and " b/" in line:
            return line.split(" b/", 1)[1].strip() or None
    return None


def _format_range(start: int | None, end: int | None) -> str | None:
    """Caption for a step's slice, e.g. "lines 40–44"."""
    if not start or not end:
        return None
    return f"line {start}" if start == end else f"lines {start}–{end}"


def _resolve_step(raw: str | None, total: int) -> int:
    """Resolves `?step=` to a step that exists: out-of-range numbers clamp
    to the nearest end of the walkthrough, and anything unparseable opens
    at the first step. Never raises — the query string is navigation state,
    not part of the credential, so a truncated or hand-edited link should
    cost someone their place, not an otherwise valid session."""
    if total <= 0:
        return 1
    if raw == _LAST_STEP:
        return total
    try:
        return min(max(int(raw), 1), total)
    except (TypeError, ValueError):
        return 1


def _is_expired(session: dict) -> bool:
    return session["expires_at"] < datetime.now(UTC)


def _can_afford_tutorial(attempts_used: int, cap: int) -> bool:
    """Whether a tutorial can be requested without spending the last
    attempt in the shared MAX_SESSION_ATTEMPTS budget.

    `attempts_used < cap` is the wrong test, and was the bug: at
    attempts_used == cap - 1 it still offered the tutorial, and taking it
    made create_tutorial() flip the session to terminal 'failed'. The
    redirect then landed on view_session's passed/failed branch, which
    renders result.html — so the developer spent their last attempt and an
    Anthropic call on a breakdown they were never shown, and were blocked
    from merging without ever getting a second answer.

    A tutorial can never itself pass a session, so one that exhausts the
    budget can only ever end it. Requiring a spare attempt to answer with
    afterwards is what makes the offer honest: take the lesson, then use
    what you learned. cap == 0 is unlimited.
    """
    return cap == 0 or attempts_used < cap - 1


def _log_if_unguarded(request: Request, session: dict, js_active: bool) -> None:
    """A submission without `js_active` came from a page where
    app/static/nopaste.js never ran — JavaScript off, the script blocked,
    or no browser at all (curl) — so pasting wasn't blocked for it. Logged,
    not refused: the guard is friction, and grading is what decides.

    Called only once a submission is about to be recorded, so a rejected
    or failed-to-grade POST doesn't log twice when it's retried."""
    if js_active:
        return
    logger.warning(
        "submitted without paste guard",
        extra={
            "repo": session["repo"],
            "pr_number": session["pr_number"],
            "outcome": "no_js",
            # Never request.url.path raw — see app/main.py:log_requests.
            "path": redact_session_token(request.url.path),
        },
    )


def _answer_context(
    session: dict,
    *,
    error: str | None = None,
    answer_body: str = "",
    previous_reasoning: str | None = None,
    tutorial_feedback: str | None = None,
    show_tutorial_option: bool = False,
    attempts_used: int = 0,
    max_session_attempts: int = 0,
) -> dict:
    return {
        "token": session["token"],
        "repo": session["repo"],
        "pr_number": session["pr_number"],
        "question": session["question"],
        "diff_lines": _classify_diff(session["diff"]),
        "error": error,
        "answer_body": answer_body,
        "previous_reasoning": previous_reasoning,
        "tutorial_feedback": tutorial_feedback,
        "show_tutorial_option": show_tutorial_option,
        "attempts_used": attempts_used,
        "max_session_attempts": max_session_attempts,
    }


async def _build_answer_context(
    pool, settings, session: dict, *, error: str | None = None, answer_body: str = ""
) -> dict:
    """Assembles the answer.html context, including the "wrong answer"
    banner and the tutorial-breakdown offer. A session can no longer be
    told apart as "never answered" vs. "answered wrong, can retry" by
    status alone (both are 'pending') — this is what actually
    distinguishes them, by looking at the latest answer/tutorial rows.
    """
    latest_answer = await fetch_latest_answer(pool, session["id"])
    latest_tutorial = await fetch_latest_tutorial(pool, session["id"])
    answers_used = await count_answers(pool, session["id"])
    tutorials_used = await count_tutorials(pool, session["id"])
    attempts_used = answers_used + tutorials_used

    cap = settings.max_session_attempts
    wrong_before = latest_answer is not None and latest_answer["passed"] is False
    tutorial_done = latest_tutorial is not None and latest_tutorial["explanation_body"] is not None

    return _answer_context(
        session,
        error=error,
        answer_body=answer_body,
        previous_reasoning=latest_answer["reasoning"] if wrong_before else None,
        tutorial_feedback=latest_tutorial["reasoning"] if tutorial_done else None,
        show_tutorial_option=settings.enable_tutorial and _can_afford_tutorial(attempts_used, cap),
        attempts_used=attempts_used,
        max_session_attempts=cap,
    )


def _tutorial_context(
    session: dict,
    tutorial: dict,
    *,
    step: str | None = None,
    error: str | None = None,
    explanation_body: str = "",
) -> dict:
    """Context for one step of the walkthrough.

    Which step is pure navigation state read off the query string — no row
    is written and no model call is made to move between them, so a
    tutorial still costs exactly one call however many times it's paged
    through (see app/tutorials.py:create_tutorial on the shared budget).

    Rows written before migrations/V5__tutorial_steps.sql have no `steps`,
    so `steps_total` is 0 and the template falls back to rendering
    `breakdown` as one block, exactly as it did before this existed.
    """
    diff_lines = _classify_diff(session["diff"])
    steps = tutorial.get("steps") or []
    total = len(steps)
    number = _resolve_step(step, total)
    current = steps[number - 1] if total else {}

    start, end = current.get("start_line"), current.get("end_line")

    return {
        "token": session["token"],
        "repo": session["repo"],
        "pr_number": session["pr_number"],
        "question": session["question"],
        "diff_lines": diff_lines,
        "breakdown": tutorial["breakdown"],
        "error": error,
        "explanation_body": explanation_body,
        "steps_total": total,
        "step_number": number,
        "step_title": current.get("title"),
        "step_body": current.get("body"),
        "step_lines": _slice_diff(diff_lines, start, end),
        "step_file": _file_for_line(diff_lines, start),
        "step_range": _format_range(start, end),
        "is_last_step": number >= total,
        "prev_step": number - 1 if number > 1 else None,
        "next_step": number + 1 if number < total else None,
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
                "question": session["question"],
                "diff_lines": _classify_diff(session["diff"]),
                "status": session["status"],
                "answer_body": answer["body"] if answer else "",
                "reasoning": answer["reasoning"] if answer else None,
            },
        )

    if _is_expired(session):
        return templates.TemplateResponse(request, "error.html", _EXPIRED, status_code=410)

    latest_tutorial = await fetch_latest_tutorial(pool, session["id"])
    if latest_tutorial is not None and latest_tutorial["explanation_body"] is None:
        return templates.TemplateResponse(
            request,
            "tutorial.html",
            _tutorial_context(session, latest_tutorial, step=request.query_params.get("step")),
        )

    context = await _build_answer_context(pool, settings, session)
    return templates.TemplateResponse(request, "answer.html", context)


@router.post("/s/{token}/answer")
async def submit_answer(
    request: Request, token: str, answer: str = Form(...), js_active: bool = Form(False)
) -> HTMLResponse:
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

    if not answer.strip():
        context = await _build_answer_context(
            pool,
            settings,
            session,
            error="Please write an answer before submitting.",
            answer_body=answer,
        )
        return templates.TemplateResponse(request, "answer.html", context, status_code=422)

    if len(answer.encode("utf-8")) > settings.max_answer_bytes:
        context = await _build_answer_context(
            pool,
            settings,
            session,
            error=f"Answer is too long (max {settings.max_answer_bytes} bytes) — please shorten it and resubmit.",
            answer_body=answer,
        )
        return templates.TemplateResponse(request, "answer.html", context, status_code=422)

    grader = request.app.state.grader
    try:
        result = await grader.grade(session["diff"], session["question"], answer)
    except GradingError:
        # Pending-equivalent error: no answers row, no status change — the
        # developer can resubmit. Must never silently pass or fail.
        context = await _build_answer_context(
            pool,
            settings,
            session,
            error="Grading failed — please try submitting your answer again.",
            answer_body=answer,
        )
        return templates.TemplateResponse(request, "answer.html", context, status_code=502)

    _log_if_unguarded(request, session, js_active)
    status = await record_answer(
        pool,
        session_id=session["id"],
        body=answer,
        passed=result.passed,
        max_session_attempts=settings.max_session_attempts,
        model=result.model,
        prompt_version=result.prompt_version,
        reasoning=result.reasoning,
        js_active=js_active,
    )

    # The only place a session reaches a verdict: request_tutorial refuses
    # a tutorial that would exhaust the budget rather than let it fail the
    # session. A wrong answer with attempts left stays pending — already
    # what GitHub shows, so nothing to post.
    if status != "pending":
        await request.app.state.status_publisher.publish(
            repo=session["repo"],
            head_sha=session["head_sha"],
            status=status,
            target_url=build_session_url(settings.grumpy_base_url, token),
        )

    # Post/redirect/get: a page refresh after this must re-fetch the result,
    # not resubmit the answer.
    return RedirectResponse(url=f"/s/{token}", status_code=303)


@router.post("/s/{token}/tutorial")
async def request_tutorial(request: Request, token: str) -> HTMLResponse:
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

    if not settings.enable_tutorial:
        return templates.TemplateResponse(
            request, "error.html", _TUTORIAL_DISABLED, status_code=404
        )

    latest_tutorial = await fetch_latest_tutorial(pool, session["id"])
    if latest_tutorial is not None and latest_tutorial["explanation_body"] is None:
        # Already an in-progress tutorial (e.g. a double-click) — redirect
        # without spending another model call.
        return RedirectResponse(url=f"/s/{token}", status_code=303)

    # Enforced here, not just by hiding the button in _build_answer_context:
    # this route is a plain unauthenticated POST, so a stale form, a
    # double-submit, or curl reaches it regardless of what the page offered.
    # Checked before the grader call, so a refused request costs nothing.
    attempts_used = await count_answers(pool, session["id"]) + await count_tutorials(
        pool, session["id"]
    )
    if not _can_afford_tutorial(attempts_used, settings.max_session_attempts):
        context = await _build_answer_context(
            pool,
            settings,
            session,
            error=(
                "Not enough attempts left for a tutorial breakdown — a tutorial "
                "costs an attempt, and the last one is reserved for your answer."
            ),
        )
        return templates.TemplateResponse(request, "answer.html", context, status_code=409)

    grader = request.app.state.grader
    try:
        result = await grader.generate_tutorial(session["diff"], session["question"])
    except GradingError:
        context = await _build_answer_context(
            pool,
            settings,
            session,
            error="Generating the tutorial breakdown failed — please try again.",
        )
        return templates.TemplateResponse(request, "answer.html", context, status_code=502)

    try:
        await create_tutorial(
            pool,
            session_id=session["id"],
            breakdown=result.text,
            steps=[asdict(step) for step in result.steps],
            max_session_attempts=settings.max_session_attempts,
            model=result.model,
            prompt_version=result.prompt_version,
        )
    except UniqueViolation:
        # Lost a race against a concurrent tutorial request for the same
        # session (migrations/V4__tutorials.sql's partial unique index) —
        # someone already got one; just show it, don't 500.
        pass

    return RedirectResponse(url=f"/s/{token}", status_code=303)


@router.post("/s/{token}/tutorial/explain")
async def submit_tutorial_explanation(
    request: Request, token: str, explanation: str = Form(...), js_active: bool = Form(False)
) -> HTMLResponse:
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

    latest_tutorial = await fetch_latest_tutorial(pool, session["id"])
    if latest_tutorial is None or latest_tutorial["explanation_body"] is not None:
        return templates.TemplateResponse(
            request, "error.html", _NO_TUTORIAL_IN_PROGRESS, status_code=409
        )

    if not explanation.strip():
        context = _tutorial_context(
            session,
            latest_tutorial,
            step=_LAST_STEP,
            error="Please write an explanation before submitting.",
            explanation_body=explanation,
        )
        return templates.TemplateResponse(request, "tutorial.html", context, status_code=422)

    # Same byte cap as an answer — bounding storage and Anthropic spend per
    # submission applies identically here; not worth a third size knob.
    if len(explanation.encode("utf-8")) > settings.max_answer_bytes:
        context = _tutorial_context(
            session,
            latest_tutorial,
            step=_LAST_STEP,
            error=f"Explanation is too long (max {settings.max_answer_bytes} bytes) — please shorten it and resubmit.",
            explanation_body=explanation,
        )
        return templates.TemplateResponse(request, "tutorial.html", context, status_code=422)

    grader = request.app.state.grader
    try:
        result = await grader.grade_explanation(
            session["diff"], latest_tutorial["breakdown"], explanation
        )
    except GradingError:
        context = _tutorial_context(
            session,
            latest_tutorial,
            step=_LAST_STEP,
            error="Grading failed — please try submitting your explanation again.",
            explanation_body=explanation,
        )
        return templates.TemplateResponse(request, "tutorial.html", context, status_code=502)

    _log_if_unguarded(request, session, js_active)
    await record_tutorial_explanation(
        pool,
        tutorial_id=latest_tutorial["id"],
        explanation_body=explanation,
        passed=result.passed,
        reasoning=result.reasoning,
        model=result.model,
        prompt_version=result.prompt_version,
        js_active=js_active,
    )

    # Pass or fail, this always unlocks a fresh attempt at the original
    # question — not a "keep retrying the tutorial until you pass" design.
    return RedirectResponse(url=f"/s/{token}", status_code=303)
