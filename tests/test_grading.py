"""Unit tests for RealGrader: the three calls (interpret, generate_exam,
grade_answer), their defensive JSON parsing, anchor clamping, prompt
selection, and transient-error retries.

All against a mocked HTTP transport — no real API key, no network, no cost.
Grading *accuracy* is a separate question answered by the eval cases under
evals/ (`make eval`), not here.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.ai import grading
from app.ai.grading import (
    HIGH_LEVEL_QUESTION,
    GradingError,
    RealGrader,
    _grade_system_prompt,
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


def _questions_response(*questions: tuple[str, int, int]) -> str:
    return json.dumps(
        {
            "questions": [
                {"question": q, "start_line": s, "end_line": e, "reference_answer": f"ref: {q}"}
                for q, s, e in questions
            ]
        }
    )


def _mark_response(passed: bool, note: str = "a note") -> str:
    return json.dumps({"passed": passed, "note": note})


def _grader_with(*items: str | httpx.Response) -> RealGrader:
    """items are consumed one per HTTP call. A bare string becomes a normal
    text response; pass an httpx.Response for anything else."""
    remaining = list(items)

    def handler(request: httpx.Request) -> httpx.Response:
        item = remaining.pop(0)
        return item if isinstance(item, httpx.Response) else _messages_response(item)

    transport = httpx.MockTransport(handler)
    return RealGrader(api_key="test-key", client_factory=lambda: httpx.AsyncClient(transport=transport))


def _capturing(requests: list[httpx.Request], response_text: str, **kwargs) -> RealGrader:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _messages_response(response_text)

    transport = httpx.MockTransport(handler)
    return RealGrader(
        api_key="test-key", client_factory=lambda: httpx.AsyncClient(transport=transport), **kwargs
    )


# --- interpret ----------------------------------------------------------


@pytest.mark.anyio
async def test_interpret_returns_text() -> None:
    grader = _grader_with("- adds a TTL to cache entries")
    assert await grader.interpret("diff") == "- adds a TTL to cache entries"


@pytest.mark.anyio
async def test_interpret_empty_raises() -> None:
    grader = _grader_with("   ")
    with pytest.raises(GradingError):
        await grader.interpret("diff")


@pytest.mark.anyio
async def test_interpret_refusal_raises() -> None:
    grader = _grader_with(_messages_response("", stop_reason="refusal"))
    with pytest.raises(GradingError):
        await grader.interpret("diff")


# --- generate_exam ------------------------------------------------------


@pytest.mark.anyio
async def test_generate_exam_prepends_high_level_and_keeps_anchors() -> None:
    grader = _grader_with(_questions_response(("Why the lock?", 5, 8), ("What breaks?", 2, 2)))
    questions = await grader.generate_exam("l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8", 3)

    assert questions[0].question == HIGH_LEVEL_QUESTION
    assert questions[0].start_line is None and questions[0].end_line is None
    assert (questions[1].question, questions[1].start_line, questions[1].end_line) == (
        "Why the lock?",
        5,
        8,
    )
    assert (questions[2].start_line, questions[2].end_line) == (2, 2)


@pytest.mark.anyio
async def test_generate_exam_parses_the_reference_answer() -> None:
    grader = _grader_with(_questions_response(("Why the lock?", 1, 2)))
    questions = await grader.generate_exam("l1\nl2", 2)
    assert questions[1].reference_answer == "ref: Why the lock?"


@pytest.mark.anyio
async def test_generate_exam_caps_scoped_questions() -> None:
    grader = _grader_with(
        _questions_response(("q2", 1, 1), ("q3", 1, 1), ("q4", 1, 1), ("q5", 1, 1))
    )
    questions = await grader.generate_exam("l1\nl2", 2)
    assert [q.question for q in questions] == [HIGH_LEVEL_QUESTION, "q2"]


@pytest.mark.anyio
async def test_generate_exam_tolerates_fewer() -> None:
    grader = _grader_with(_questions_response(("only one", 1, 1)))
    questions = await grader.generate_exam("l1\nl2", 5)
    assert [q.question for q in questions] == [HIGH_LEVEL_QUESTION, "only one"]


@pytest.mark.anyio
async def test_generate_exam_count_one_makes_no_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call for a one-question walkthrough")

    grader = RealGrader(
        api_key="test-key",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    questions = await grader.generate_exam("diff", 1)
    assert [q.question for q in questions] == [HIGH_LEVEL_QUESTION]


@pytest.mark.anyio
async def test_generate_exam_clamps_and_swaps_anchors() -> None:
    grader = _grader_with(_questions_response(("inverted", 9, 3), ("out of range", -5, 900)))
    questions = await grader.generate_exam("l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\nl9\nl10", 3)
    assert (questions[1].start_line, questions[1].end_line) == (3, 9)
    assert (questions[2].start_line, questions[2].end_line) == (1, 10)


@pytest.mark.anyio
async def test_generate_exam_unusable_anchor_drops_to_none_but_keeps_question() -> None:
    grader = _grader_with(json.dumps({"questions": [{"question": "q", "start_line": 99, "end_line": 99}]}))
    questions = await grader.generate_exam("l1\nl2", 2)
    assert questions[1].question == "q"
    assert questions[1].start_line is None and questions[1].end_line is None


@pytest.mark.anyio
async def test_generate_exam_sends_numbered_diff_and_schema() -> None:
    requests: list[httpx.Request] = []
    grader = _capturing(requests, _questions_response(("q2", 1, 1)))
    await grader.generate_exam("first line\nsecond line", 2)

    body = json.loads(requests[0].content)
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "questions" in body["output_config"]["format"]["schema"]["properties"]
    assert "1\tfirst line\n2\tsecond line" in body["messages"][0]["content"]


@pytest.mark.anyio
async def test_generate_exam_strips_a_fence() -> None:
    grader = _grader_with(f"```json\n{_questions_response(('q2', 1, 1))}\n```")
    questions = await grader.generate_exam("l1\nl2", 2)
    assert [q.question for q in questions] == [HIGH_LEVEL_QUESTION, "q2"]


@pytest.mark.anyio
async def test_generate_exam_refusal_raises() -> None:
    grader = _grader_with(_messages_response("", stop_reason="refusal"))
    with pytest.raises(GradingError):
        await grader.generate_exam("diff", 3)


# --- grade_answer -------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("passed", [True, False])
async def test_grade_answer_returns_the_mark_with_metadata(passed: bool) -> None:
    grader = _grader_with(_mark_response(passed, note="because reasons"))
    mark = await grader.grade_answer("interpretation", "question", "answer")
    assert mark.passed is passed
    assert mark.note == "because reasons"
    assert mark.model == "claude-opus-5"
    assert mark.prompt_version


@pytest.mark.anyio
async def test_grade_answer_strips_fences_and_preamble() -> None:
    grader = _grader_with("Sure:\n```json\n" + _mark_response(True) + "\n```")
    mark = await grader.grade_answer("interp", "q", "a")
    assert mark.passed is True


@pytest.mark.anyio
async def test_grade_answer_only_sees_one_question_and_the_interpretation() -> None:
    requests: list[httpx.Request] = []
    grader = _capturing(requests, _mark_response(True))
    await grader.grade_answer("the blind interpretation", "the question", "the answer")

    content = json.loads(requests[0].content)["messages"][0]["content"]
    assert "the blind interpretation" in content
    assert "the question" in content
    assert "the answer" in content


@pytest.mark.anyio
@pytest.mark.parametrize("meaniemode", [False, True])
async def test_meaniemode_selects_the_marking_tone(meaniemode: bool) -> None:
    requests: list[httpx.Request] = []
    grader = _capturing(requests, _mark_response(True), meaniemode=meaniemode)
    await grader.grade_answer("interp", "q", "a")

    system = json.loads(requests[0].content)["system"]
    assert system == _grade_system_prompt(meaniemode=meaniemode)
    assert ("scathing" in system) is meaniemode


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        '{"note": "no verdict"}',
        '{"passed": "yes", "note": "n"}',
        '{"passed": true}',
        '{"passed": true, "note": "mid',  # truncated
    ],
    ids=["not-json", "no-passed", "passed-not-bool", "no-note", "truncated"],
)
async def test_grade_answer_unusable_response_raises(payload: str) -> None:
    grader = _grader_with(payload)
    with pytest.raises(GradingError):
        await grader.grade_answer("interp", "q", "a")


@pytest.mark.anyio
async def test_grade_answer_max_tokens_names_the_budget() -> None:
    grader = _grader_with(_messages_response('{"passed": tr', stop_reason="max_tokens"))
    with pytest.raises(GradingError, match="token cap"):
        await grader.grade_answer("interp", "q", "a")


@pytest.mark.anyio
async def test_grade_answer_http_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    grader = RealGrader(
        api_key="test-key",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(GradingError):
        await grader.grade_answer("interp", "q", "a")


# --- explain ------------------------------------------------------------


@pytest.mark.anyio
async def test_explain_returns_text() -> None:
    grader = _grader_with("Here's what this section does: it takes a lock first.")
    out = await grader.explain("interp", "why the lock?")
    assert "lock" in out


@pytest.mark.anyio
async def test_explain_empty_raises() -> None:
    grader = _grader_with("   ")
    with pytest.raises(GradingError):
        await grader.explain("interp", "q")


@pytest.mark.anyio
async def test_explain_refusal_raises() -> None:
    grader = _grader_with(_messages_response("", stop_reason="refusal"))
    with pytest.raises(GradingError):
        await grader.explain("interp", "q")


# --- Transient API failures (exercised through grade_answer's one call) --


@pytest.fixture()
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(grading.asyncio, "sleep", fake_sleep)
    return slept


def _status_response(status_code: int, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status_code, json={"error": "transient"}, headers=headers or {})


@pytest.mark.anyio
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 529])
async def test_retries_transient_status_then_succeeds(status: int, no_sleep) -> None:
    grader = _grader_with(_status_response(status), _mark_response(True))
    mark = await grader.grade_answer("interp", "q", "a")
    assert mark.passed is True
    assert no_sleep == [1.0]


@pytest.mark.anyio
async def test_gives_up_after_max_attempts(no_sleep) -> None:
    grader = _grader_with(_status_response(529), _status_response(529), _status_response(529))
    with pytest.raises(GradingError, match="after 3 attempts"):
        await grader.grade_answer("interp", "q", "a")
    assert no_sleep == [1.0, 2.0]


@pytest.mark.anyio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
async def test_does_not_retry_unfixable_status(status: int, no_sleep) -> None:
    grader = _grader_with(_status_response(status))
    with pytest.raises(GradingError):
        await grader.grade_answer("interp", "q", "a")
    assert no_sleep == []


@pytest.mark.anyio
async def test_retries_a_transport_error(no_sleep) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("timed out")
        return _messages_response(_mark_response(True))

    grader = RealGrader(
        api_key="test-key",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    mark = await grader.grade_answer("interp", "q", "a")
    assert mark.passed is True
    assert no_sleep == [1.0]


@pytest.mark.anyio
async def test_honours_retry_after(no_sleep) -> None:
    grader = _grader_with(_status_response(429, headers={"retry-after": "4"}), _mark_response(True))
    await grader.grade_answer("interp", "q", "a")
    assert no_sleep == [4.0]


@pytest.mark.anyio
async def test_caps_an_unreasonable_retry_after(no_sleep) -> None:
    grader = _grader_with(_status_response(429, headers={"retry-after": "600"}), _mark_response(True))
    await grader.grade_answer("interp", "q", "a")
    assert no_sleep == [10.0]


@pytest.mark.anyio
async def test_unparseable_retry_after_falls_back(no_sleep) -> None:
    grader = _grader_with(
        _status_response(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        _mark_response(True),
    )
    await grader.grade_answer("interp", "q", "a")
    assert no_sleep == [1.0]
