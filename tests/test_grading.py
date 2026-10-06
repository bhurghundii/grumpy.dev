"""Unit tests for RealGrader's exam generation and marking: defensive JSON
parsing, prompt selection, per-question mark alignment, and transient-error
retries.

These run against a mocked HTTP transport — no real API key, no network,
no cost — and exist to prove that a malformed model response surfaces an
error rather than a verdict. Grading *accuracy* — whether the marking
actually produces correct verdicts — is a different question, answered by
the real eval cases under evals/ (`make eval`), not by these.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app import grading
from app.grading import (
    HIGH_LEVEL_QUESTION,
    GradingError,
    RealGrader,
    _exam_grade_system_prompt,
)


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


def _marks_response(*passed: bool, reasoning: str = "ok") -> str:
    return json.dumps(
        {"marks": [{"passed": p, "note": f"note {i}"} for i, p in enumerate(passed)], "reasoning": reasoning}
    )


def _questions_response(*questions: str) -> str:
    return json.dumps({"questions": list(questions)})


def _grader_with(*items: str | httpx.Response) -> RealGrader:
    """items are consumed in order: one per HTTP call the grader makes.
    grade_exam makes two (interpretation, then marking); generate_exam makes
    one. A bare string becomes a normal text response; pass an httpx.Response
    for anything else."""
    remaining = list(items)

    def handler(request: httpx.Request) -> httpx.Response:
        item = remaining.pop(0)
        return item if isinstance(item, httpx.Response) else _messages_response(item)

    transport = httpx.MockTransport(handler)
    return RealGrader(api_key="test-key", client_factory=lambda: httpx.AsyncClient(transport=transport))


def _grader_capturing(requests: list[httpx.Request], *, meaniemode: bool = False) -> RealGrader:
    """Records every outgoing request (so a test can inspect the `system`
    field actually sent) and returns an interpretation then an all-pass
    marking."""
    responses = iter([_messages_response("- adds a TTL"), _messages_response(_marks_response(True, True))])

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(responses)

    transport = httpx.MockTransport(handler)
    return RealGrader(
        api_key="test-key",
        client_factory=lambda: httpx.AsyncClient(transport=transport),
        meaniemode=meaniemode,
    )


# --- grade_exam ---------------------------------------------------------


@pytest.mark.anyio
async def test_grade_exam_marks_each_answer_and_carries_metadata() -> None:
    grader = _grader_with("- adds a TTL", _marks_response(True, False, reasoning="one right"))
    result = await grader.grade_exam("diff", ["q1", "q2"], ["a1", "a2"])
    assert [m.passed for m in result.marks] == [True, False]
    assert result.score == 1
    assert result.reasoning == "one right"
    assert result.model == "claude-opus-5"
    assert result.prompt_version


@pytest.mark.anyio
async def test_grade_exam_strips_code_fences() -> None:
    grader = _grader_with("- adds a TTL", "```json\n" + _marks_response(True) + "\n```")
    result = await grader.grade_exam("diff", ["q1"], ["a1"])
    assert result.score == 1


@pytest.mark.anyio
async def test_grade_exam_strips_preamble_text() -> None:
    grader = _grader_with("- adds a TTL", "Sure, here are the marks:\n" + _marks_response(False))
    result = await grader.grade_exam("diff", ["q1"], ["a1"])
    assert result.score == 0


@pytest.mark.anyio
async def test_fewer_marks_than_questions_fail_the_unmarked_ones() -> None:
    """A model that marks only some answers leaves the rest failed — there's
    no evidence they passed, and the score must stay within 0..question_count."""
    grader = _grader_with("- adds a TTL", _marks_response(True))
    result = await grader.grade_exam("diff", ["q1", "q2", "q3"], ["a1", "a2", "a3"])
    assert [m.passed for m in result.marks] == [True, False, False]
    assert result.score == 1


@pytest.mark.anyio
async def test_extra_marks_are_dropped() -> None:
    grader = _grader_with("- adds a TTL", _marks_response(True, True, True))
    result = await grader.grade_exam("diff", ["q1", "q2"], ["a1", "a2"])
    assert len(result.marks) == 2


@pytest.mark.anyio
async def test_grade_exam_sends_blind_interpretation_then_marking() -> None:
    requests: list[httpx.Request] = []
    grader = _grader_capturing(requests)
    await grader.grade_exam("diff", ["q1", "q2"], ["a1", "a2"])

    interpretation_body = json.loads(requests[0].content)
    marking_body = json.loads(requests[1].content)
    # The interpretation is produced blind — it must not carry the answers.
    assert interpretation_body["system"] == grading._INTERPRETATION_SYSTEM_PROMPT
    assert "a1" not in interpretation_body["messages"][0]["content"]
    # The marking call carries the sheet and uses the marking schema.
    assert "a1" in marking_body["messages"][0]["content"]
    assert marking_body["output_config"]["format"]["type"] == "json_schema"
    assert "marks" in marking_body["output_config"]["format"]["schema"]["properties"]


@pytest.mark.anyio
@pytest.mark.parametrize("meaniemode", [False, True])
async def test_meaniemode_selects_the_marking_tone(meaniemode: bool) -> None:
    requests: list[httpx.Request] = []
    grader = _grader_capturing(requests, meaniemode=meaniemode)
    await grader.grade_exam("diff", ["q1"], ["a1"])

    marking_body = json.loads(requests[1].content)
    assert marking_body["system"] == _exam_grade_system_prompt(meaniemode=meaniemode)
    assert ("scathing" in marking_body["system"]) is meaniemode


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        '{"reasoning": "no marks"}',
        '{"marks": "not a list"}',
        '{"marks": [{"passed": true, "note": "ok"}]',  # truncated
    ],
    ids=["not-json", "no-marks-key", "marks-not-a-list", "truncated"],
)
async def test_grade_exam_unusable_marking_raises(payload: str) -> None:
    grader = _grader_with("- adds a TTL", payload)
    with pytest.raises(GradingError):
        await grader.grade_exam("diff", ["q1"], ["a1"])


@pytest.mark.anyio
async def test_grade_exam_missing_reasoning_raises() -> None:
    grader = _grader_with("- adds a TTL", json.dumps({"marks": [{"passed": True, "note": "n"}]}))
    with pytest.raises(GradingError):
        await grader.grade_exam("diff", ["q1"], ["a1"])


@pytest.mark.anyio
async def test_grade_exam_refusal_raises() -> None:
    grader = _grader_with(_messages_response("", stop_reason="refusal"))
    with pytest.raises(GradingError):
        await grader.grade_exam("diff", ["q1"], ["a1"])


@pytest.mark.anyio
async def test_grade_exam_max_tokens_names_the_budget() -> None:
    truncated = _messages_response('{"marks": [{"passed": tr', stop_reason="max_tokens")
    grader = _grader_with("- adds a TTL", truncated)
    with pytest.raises(GradingError, match="token cap"):
        await grader.grade_exam("diff", ["q1"], ["a1"])


@pytest.mark.anyio
async def test_grade_exam_empty_interpretation_raises() -> None:
    grader = _grader_with("   ")
    with pytest.raises(GradingError):
        await grader.grade_exam("diff", ["q1"], ["a1"])


@pytest.mark.anyio
async def test_grade_exam_http_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    grader = RealGrader(
        api_key="test-key",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(GradingError):
        await grader.grade_exam("diff", ["q1"], ["a1"])


# --- generate_exam ------------------------------------------------------


@pytest.mark.anyio
async def test_generate_exam_prepends_the_high_level_question() -> None:
    grader = _grader_with(_questions_response("Why the lock?", "What breaks without the TTL?"))
    questions = await grader.generate_exam("diff", 3)
    assert questions == [HIGH_LEVEL_QUESTION, "Why the lock?", "What breaks without the TTL?"]


@pytest.mark.anyio
async def test_generate_exam_caps_scoped_questions_to_count_minus_one() -> None:
    grader = _grader_with(_questions_response("q2", "q3", "q4", "q5", "q6"))
    questions = await grader.generate_exam("diff", 3)
    assert questions == [HIGH_LEVEL_QUESTION, "q2", "q3"]


@pytest.mark.anyio
async def test_generate_exam_tolerates_fewer_questions() -> None:
    grader = _grader_with(_questions_response("only one scoped"))
    questions = await grader.generate_exam("diff", 5)
    assert questions == [HIGH_LEVEL_QUESTION, "only one scoped"]


@pytest.mark.anyio
async def test_generate_exam_count_one_makes_no_model_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call should be made for a one-question sheet")

    grader = RealGrader(
        api_key="test-key",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert await grader.generate_exam("diff", 1) == [HIGH_LEVEL_QUESTION]


@pytest.mark.anyio
async def test_generate_exam_sends_the_questions_schema() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _messages_response(_questions_response("q2"))

    grader = RealGrader(
        api_key="test-key",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await grader.generate_exam("the diff body", 2)

    body = json.loads(requests[0].content)
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "questions" in body["output_config"]["format"]["schema"]["properties"]
    assert "the diff body" in body["messages"][0]["content"]


@pytest.mark.anyio
async def test_generate_exam_strips_a_code_fence() -> None:
    grader = _grader_with(f"```json\n{_questions_response('q2', 'q3')}\n```")
    questions = await grader.generate_exam("diff", 3)
    assert questions == [HIGH_LEVEL_QUESTION, "q2", "q3"]


@pytest.mark.anyio
async def test_generate_exam_refusal_raises() -> None:
    grader = _grader_with(_messages_response("", stop_reason="refusal"))
    with pytest.raises(GradingError):
        await grader.generate_exam("diff", 3)


@pytest.mark.anyio
async def test_generate_exam_http_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    grader = RealGrader(
        api_key="test-key",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(GradingError):
        await grader.generate_exam("diff", 3)


# --- Transient API failures ---------------------------------------------
#
# Grading runs synchronously inside the developer's request, and a
# GradingError reaches them as "please try again" with no explanation. A
# single 429, or a 529 overloaded_error, is routine and self-resolving --
# it should not be something a human has to notice and retry by hand.


@pytest.fixture()
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Records the backoff delays without actually waiting them out."""
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(grading.asyncio, "sleep", fake_sleep)
    return slept


def _status_response(status_code: int, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status_code, json={"error": "transient"}, headers=headers or {})


@pytest.mark.anyio
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 529])
async def test_retries_transient_status_and_then_succeeds(status: int, no_sleep) -> None:
    grader = _grader_with(_status_response(status), "- adds a TTL", _marks_response(True))

    result = await grader.grade_exam("diff", ["q1"], ["a1"])

    assert result.score == 1
    assert no_sleep == [1.0], "one retry, one second of backoff"


@pytest.mark.anyio
async def test_gives_up_after_max_attempts_with_exponential_backoff(no_sleep) -> None:
    grader = _grader_with(_status_response(529), _status_response(529), _status_response(529))

    with pytest.raises(GradingError, match="after 3 attempts"):
        await grader.grade_exam("diff", ["q1"], ["a1"])

    assert no_sleep == [1.0, 2.0], "backs off between attempts, not after the last"


@pytest.mark.anyio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
async def test_does_not_retry_a_failure_that_will_not_fix_itself(status: int, no_sleep) -> None:
    """A bad API key or a malformed request retries identically; burning
    two more calls and 3s of the developer's wait proves nothing."""
    grader = _grader_with(_status_response(status))

    with pytest.raises(GradingError):
        await grader.grade_exam("diff", ["q1"], ["a1"])

    assert no_sleep == [], "no backoff for a non-retryable status"


@pytest.mark.anyio
async def test_retries_a_transport_error(no_sleep) -> None:
    """Connect/read timeouts and dropped connections are transient too."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("timed out")
        if calls["n"] == 2:
            return _messages_response("- adds a TTL")
        return _messages_response(_marks_response(True))

    grader = RealGrader(
        api_key="test-key",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    result = await grader.grade_exam("diff", ["q1"], ["a1"])

    assert result.score == 1
    assert no_sleep == [1.0]


@pytest.mark.anyio
async def test_honours_retry_after_when_the_api_sends_one(no_sleep) -> None:
    grader = _grader_with(
        _status_response(429, headers={"retry-after": "4"}), "- adds a TTL", _marks_response(True)
    )

    await grader.grade_exam("diff", ["q1"], ["a1"])

    assert no_sleep == [4.0], "the API knows its own backpressure better than a fixed curve"


@pytest.mark.anyio
async def test_caps_an_unreasonable_retry_after(no_sleep) -> None:
    """The developer is sitting on a synchronous request; a 10-minute
    Retry-After is not something to hold it open for."""
    grader = _grader_with(
        _status_response(429, headers={"retry-after": "600"}), "- adds a TTL", _marks_response(True)
    )

    await grader.grade_exam("diff", ["q1"], ["a1"])

    assert no_sleep == [10.0]


@pytest.mark.anyio
async def test_unparseable_retry_after_falls_back_to_backoff(no_sleep) -> None:
    """The HTTP-date form isn't parsed; treat it as absent rather than
    failing the call over a header."""
    grader = _grader_with(
        _status_response(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        "- adds a TTL",
        _marks_response(True),
    )

    await grader.grade_exam("diff", ["q1"], ["a1"])

    assert no_sleep == [1.0]
