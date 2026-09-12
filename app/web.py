"""Developer-facing HTML routes: the answer page and its submission
handler. Unauthenticated by design — the token in the URL is the
credential. (Flagged in the phase 3 report: whether this should require
proving PR authorship is an open product question, not implemented here.)
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.answers import fetch_latest_answer, fetch_session_by_token, record_answer
from app.grading import GradingError

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()

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
    highlighting, no collapsing, no line-number gutter — phase 5 polish
    at the earliest."""
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
    return session["expires_at"] < datetime.now(timezone.utc)


def _answer_context(session: dict, *, error: str | None = None, answer_body: str = "") -> dict:
    return {
        "token": session["token"],
        "repo": session["repo"],
        "pr_number": session["pr_number"],
        "question": session["question"],
        "diff_lines": _classify_diff(session["diff"]),
        "error": error,
        "answer_body": answer_body,
    }


@router.get("/s/{token}")
async def view_session(request: Request, token: str) -> HTMLResponse:
    pool = request.app.state.pool
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

    return templates.TemplateResponse(request, "answer.html", _answer_context(session))


@router.post("/s/{token}/answer")
async def submit_answer(request: Request, token: str, answer: str = Form(...)) -> HTMLResponse:
    pool = request.app.state.pool
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
        context = _answer_context(
            session, error="Please write an answer before submitting.", answer_body=answer
        )
        return templates.TemplateResponse(request, "answer.html", context, status_code=422)

    settings = request.app.state.settings
    if len(answer.encode("utf-8")) > settings.max_answer_bytes:
        context = _answer_context(
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
        context = _answer_context(
            session,
            error="Grading failed — please try submitting your answer again.",
            answer_body=answer,
        )
        return templates.TemplateResponse(request, "answer.html", context, status_code=502)

    await record_answer(
        pool,
        session_id=session["id"],
        body=answer,
        passed=result.passed,
        model=result.model,
        prompt_version=result.prompt_version,
        reasoning=result.reasoning,
    )

    # Post/redirect/get: a page refresh after this must re-fetch the result,
    # not resubmit the answer.
    return RedirectResponse(url=f"/s/{token}", status_code=303)
