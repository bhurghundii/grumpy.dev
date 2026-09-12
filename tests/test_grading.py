"""Unit tests for RealGrader's defensive JSON parsing and error handling.

These run against a mocked HTTP transport — no real API key, no network,
no cost — and exist to prove gate item 4 ("a malformed model response
surfaces an error rather than a verdict") without needing live model
access. Grading *accuracy* — whether the two-call flow actually produces
correct verdicts — is a different question, answered by the five real
eval cases under evals/ (`make eval`), not by these.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.grading import GradingError, RealGrader


def _messages_response(text: str, *, stop_reason: str = "end_turn") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": "claude-opus-5",
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )


def _grader_with(*items: str | httpx.Response) -> RealGrader:
    """items are consumed in order: one per HTTP call the grader makes
    (interpretation, then comparison). A bare string becomes a normal
    text response; pass an httpx.Response for anything else."""
    remaining = list(items)

    def handler(request: httpx.Request) -> httpx.Response:
        item = remaining.pop(0)
        return item if isinstance(item, httpx.Response) else _messages_response(item)

    transport = httpx.MockTransport(handler)
    return RealGrader(api_key="test-key", client_factory=lambda: httpx.AsyncClient(transport=transport))


@pytest.mark.anyio
async def test_grade_passes_on_clean_json() -> None:
    grader = _grader_with(
        "- adds a TTL to cache entries",
        json.dumps({"passed": True, "reasoning": "matches the interpretation"}),
    )
    result = await grader.grade("diff", "question", "answer")
    assert result.passed is True
    assert result.model == "claude-opus-5"
    assert result.prompt_version


@pytest.mark.anyio
async def test_grade_strips_code_fences() -> None:
    grader = _grader_with(
        "- adds a TTL",
        "```json\n" + json.dumps({"passed": False, "reasoning": "missing the TTL"}) + "\n```",
    )
    result = await grader.grade("diff", "question", "answer")
    assert result.passed is False


@pytest.mark.anyio
async def test_grade_strips_preamble_text() -> None:
    grader = _grader_with(
        "- adds a TTL",
        "Sure, here's my verdict:\n" + json.dumps({"passed": True, "reasoning": "ok"}),
    )
    result = await grader.grade("diff", "question", "answer")
    assert result.passed is True


@pytest.mark.anyio
async def test_malformed_json_raises_grading_error() -> None:
    grader = _grader_with("- adds a TTL", "not json at all")
    with pytest.raises(GradingError):
        await grader.grade("diff", "question", "answer")


@pytest.mark.anyio
async def test_missing_required_field_raises_grading_error() -> None:
    grader = _grader_with("- adds a TTL", json.dumps({"passed": True}))
    with pytest.raises(GradingError):
        await grader.grade("diff", "question", "answer")


@pytest.mark.anyio
async def test_wrong_field_type_raises_grading_error() -> None:
    grader = _grader_with("- adds a TTL", json.dumps({"passed": "yes", "reasoning": "ok"}))
    with pytest.raises(GradingError):
        await grader.grade("diff", "question", "answer")


@pytest.mark.anyio
async def test_refusal_stop_reason_raises_grading_error() -> None:
    grader = _grader_with(_messages_response("", stop_reason="refusal"))
    with pytest.raises(GradingError):
        await grader.grade("diff", "question", "answer")


@pytest.mark.anyio
async def test_empty_interpretation_raises_grading_error() -> None:
    grader = _grader_with("   ")
    with pytest.raises(GradingError):
        await grader.grade("diff", "question", "answer")


@pytest.mark.anyio
async def test_http_error_raises_grading_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    grader = RealGrader(
        api_key="test-key",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(GradingError):
        await grader.grade("diff", "question", "answer")
