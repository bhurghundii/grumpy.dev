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

from app import grading
from app.grading import (
    _COMPARISON_SYSTEM_PROMPT_MEAN,
    _COMPARISON_SYSTEM_PROMPT_PROFESSIONAL,
    GradingError,
    RealGrader,
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


def _grader_capturing_system(requests: list[httpx.Request], *, meaniemode: bool) -> RealGrader:
    """Like _grader_with, but records every outgoing request (so a test can
    inspect the `system` field actually sent) and always returns a passing
    verdict for both calls."""
    responses = iter(
        [
            _messages_response("- adds a TTL"),
            _messages_response(json.dumps({"passed": True, "reasoning": "ok"})),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(responses)

    transport = httpx.MockTransport(handler)
    return RealGrader(
        api_key="test-key",
        client_factory=lambda: httpx.AsyncClient(transport=transport),
        meaniemode=meaniemode,
    )


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
async def test_meaniemode_off_by_default_sends_professional_prompt() -> None:
    requests: list[httpx.Request] = []
    grader = _grader_capturing_system(requests, meaniemode=False)
    await grader.grade("diff", "question", "answer")

    comparison_body = json.loads(requests[1].content)
    assert comparison_body["system"] == _COMPARISON_SYSTEM_PROMPT_PROFESSIONAL
    assert comparison_body["system"] != _COMPARISON_SYSTEM_PROMPT_MEAN


@pytest.mark.anyio
async def test_meaniemode_on_sends_mean_prompt() -> None:
    requests: list[httpx.Request] = []
    grader = _grader_capturing_system(requests, meaniemode=True)
    await grader.grade("diff", "question", "answer")

    comparison_body = json.loads(requests[1].content)
    assert comparison_body["system"] == _COMPARISON_SYSTEM_PROMPT_MEAN
    assert comparison_body["system"] != _COMPARISON_SYSTEM_PROMPT_PROFESSIONAL


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


# --- generate_tutorial / grade_explanation: one HTTP call each, so
# _grader_with's single-item form is enough. grade_explanation reuses the
# same _extract_text/_check_stop_reason/_parse_verdict helpers already
# exercised above, so this doesn't re-run the full parser matrix for it.
# generate_tutorial does get its own matrix below: _parse_tutorial is a
# second parser with rules of its own — notably that a bad line anchor
# costs a step its code slice but not its prose.

_DIFF = "\n".join(f"line {n}" for n in range(1, 21))


def _steps_response(*steps: dict) -> str:
    return json.dumps({"steps": list(steps)})


def _step(**overrides) -> dict:
    return {"title": "A step", "body": "What it does.", "start_line": 2, "end_line": 4} | overrides


@pytest.mark.anyio
async def test_generate_tutorial_returns_steps_with_model_metadata() -> None:
    grader = _grader_with(
        _steps_response(
            _step(title="First", body="Sets up the lock.", start_line=2, end_line=4),
            _step(title="Second", body="Then records the charge.", start_line=7, end_line=9),
        )
    )
    breakdown = await grader.generate_tutorial(_DIFF, "question")

    assert [(s.title, s.start_line, s.end_line) for s in breakdown.steps] == [
        ("First", 2, 4),
        ("Second", 7, 9),
    ]
    assert breakdown.model == "claude-opus-5"
    assert breakdown.prompt_version


@pytest.mark.anyio
async def test_generate_tutorial_flattens_steps_into_breakdown_text() -> None:
    """`text` is what lands in tutorials.breakdown and what
    grade_explanation is handed later, so it has to stay a readable prose
    rendering of the same steps rather than raw JSON."""
    grader = _grader_with(
        _steps_response(_step(title="First", body="Sets up."), _step(title="Second", body="Then."))
    )
    breakdown = await grader.generate_tutorial(_DIFF, "question")

    assert breakdown.text == "1. First\n\nSets up.\n\n2. Second\n\nThen."


@pytest.mark.anyio
async def test_generate_tutorial_sends_schema_and_numbered_diff() -> None:
    """The model can only anchor a step to a line range it can see, so the
    diff goes out numbered — with the same 1-based indexing app/web.py
    resolves those anchors against."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _messages_response(_steps_response(_step(), _step()))

    grader = RealGrader(
        api_key="test-key",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await grader.generate_tutorial("first line\nsecond line", "question")

    body = json.loads(requests[0].content)
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "steps" in body["output_config"]["format"]["schema"]["properties"]
    assert "1\tfirst line\n2\tsecond line" in body["messages"][0]["content"]


@pytest.mark.anyio
async def test_generate_tutorial_empty_response_raises_grading_error() -> None:
    grader = _grader_with("   ")
    with pytest.raises(GradingError):
        await grader.generate_tutorial("diff", "question")


@pytest.mark.anyio
async def test_generate_tutorial_parses_steps_wrapped_in_a_code_fence() -> None:
    grader = _grader_with(f"Here you go:\n```json\n{_steps_response(_step(), _step())}\n```")
    breakdown = await grader.generate_tutorial(_DIFF, "question")
    assert len(breakdown.steps) == 2


@pytest.mark.anyio
async def test_generate_tutorial_swaps_inverted_line_bounds() -> None:
    grader = _grader_with(_steps_response(_step(start_line=9, end_line=3), _step()))
    breakdown = await grader.generate_tutorial(_DIFF, "question")
    assert (breakdown.steps[0].start_line, breakdown.steps[0].end_line) == (3, 9)


@pytest.mark.anyio
async def test_generate_tutorial_clamps_out_of_range_line_bounds() -> None:
    grader = _grader_with(_steps_response(_step(start_line=-5, end_line=900), _step()))
    breakdown = await grader.generate_tutorial(_DIFF, "question")
    assert (breakdown.steps[0].start_line, breakdown.steps[0].end_line) == (1, 20)


@pytest.mark.anyio
async def test_generate_tutorial_keeps_step_whose_anchor_is_unusable() -> None:
    """A step the model couldn't anchor still teaches something; it just
    loses its code slice. Dropping the prose over a bad pointer would throw
    away the part that took a model call to produce."""
    grader = _grader_with(
        _steps_response(_step(start_line="nonsense", end_line=None), _step(start_line=99, end_line=99))
    )
    breakdown = await grader.generate_tutorial(_DIFF, "question")

    assert len(breakdown.steps) == 2
    assert breakdown.steps[0].start_line is None
    assert breakdown.steps[0].body == "What it does."
    # Wholly past the end of the diff — clamping can't rescue it either.
    assert breakdown.steps[1].start_line is None


@pytest.mark.anyio
async def test_generate_tutorial_drops_steps_missing_prose() -> None:
    grader = _grader_with(_steps_response(_step(), {"start_line": 1, "end_line": 2}, _step(body="  ")))
    breakdown = await grader.generate_tutorial(_DIFF, "question")
    assert len(breakdown.steps) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        '{"reasoning": "not a walkthrough"}',
        '{"steps": "not a list"}',
        '{"steps": []}',
        '{"steps": [{"title": "no body"}]}',
        "not json at all",
        '{"steps": [{"title": "truncated", "body": "mid-obj',
    ],
    ids=["no-steps-key", "steps-not-a-list", "steps-empty", "no-usable-steps", "not-json", "truncated"],
)
async def test_generate_tutorial_unusable_response_raises_grading_error(payload: str) -> None:
    """Nothing here may quietly become an empty walkthrough — the caller
    (app/web.py:request_tutorial) turns GradingError into a retryable 502,
    which is the only honest outcome when the model produced no lesson."""
    grader = _grader_with(payload)
    with pytest.raises(GradingError):
        await grader.generate_tutorial(_DIFF, "question")


@pytest.mark.anyio
async def test_generate_tutorial_refusal_raises_grading_error() -> None:
    grader = _grader_with(_messages_response("", stop_reason="refusal"))
    with pytest.raises(GradingError):
        await grader.generate_tutorial("diff", "question")


@pytest.mark.anyio
async def test_generate_tutorial_http_error_raises_grading_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    grader = RealGrader(
        api_key="test-key",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(GradingError):
        await grader.generate_tutorial("diff", "question")


@pytest.mark.anyio
async def test_grade_explanation_passes_on_clean_json() -> None:
    grader = _grader_with(json.dumps({"passed": True, "reasoning": "shows understanding"}))
    result = await grader.grade_explanation("diff", "breakdown", "explanation")
    assert result.passed is True
    assert result.model == "claude-opus-5"
    assert result.prompt_version


@pytest.mark.anyio
async def test_grade_explanation_fails_on_clean_json() -> None:
    grader = _grader_with(json.dumps({"passed": False, "reasoning": "still confused"}))
    result = await grader.grade_explanation("diff", "breakdown", "explanation")
    assert result.passed is False


@pytest.mark.anyio
async def test_grade_explanation_malformed_json_raises_grading_error() -> None:
    grader = _grader_with("not json at all")
    with pytest.raises(GradingError):
        await grader.grade_explanation("diff", "breakdown", "explanation")


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
    grader = _grader_with(
        _status_response(status),
        "- adds a TTL",
        json.dumps({"passed": True, "reasoning": "ok"}),
    )

    result = await grader.grade("diff", "question", "answer")

    assert result.passed is True
    assert no_sleep == [1.0], "one retry, one second of backoff"


@pytest.mark.anyio
async def test_gives_up_after_max_attempts_with_exponential_backoff(no_sleep) -> None:
    grader = _grader_with(
        _status_response(529), _status_response(529), _status_response(529)
    )

    with pytest.raises(GradingError, match="after 3 attempts"):
        await grader.grade("diff", "question", "answer")

    assert no_sleep == [1.0, 2.0], "backs off between attempts, not after the last"


@pytest.mark.anyio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
async def test_does_not_retry_a_failure_that_will_not_fix_itself(status: int, no_sleep) -> None:
    """A bad API key or a malformed request retries identically; burning
    two more calls and 3s of the developer's wait proves nothing."""
    grader = _grader_with(_status_response(status))

    with pytest.raises(GradingError):
        await grader.grade("diff", "question", "answer")

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
        return _messages_response(json.dumps({"passed": True, "reasoning": "ok"}))

    grader = RealGrader(
        api_key="test-key",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    result = await grader.grade("diff", "question", "answer")

    assert result.passed is True
    assert no_sleep == [1.0]


@pytest.mark.anyio
async def test_honours_retry_after_when_the_api_sends_one(no_sleep) -> None:
    grader = _grader_with(
        _status_response(429, headers={"retry-after": "4"}),
        "- adds a TTL",
        json.dumps({"passed": True, "reasoning": "ok"}),
    )

    await grader.grade("diff", "question", "answer")

    assert no_sleep == [4.0], "the API knows its own backpressure better than a fixed curve"


@pytest.mark.anyio
async def test_caps_an_unreasonable_retry_after(no_sleep) -> None:
    """The developer is sitting on a synchronous request; a 10-minute
    Retry-After is not something to hold it open for."""
    grader = _grader_with(
        _status_response(429, headers={"retry-after": "600"}),
        "- adds a TTL",
        json.dumps({"passed": True, "reasoning": "ok"}),
    )

    await grader.grade("diff", "question", "answer")

    assert no_sleep == [10.0]


@pytest.mark.anyio
async def test_unparseable_retry_after_falls_back_to_backoff(no_sleep) -> None:
    """The HTTP-date form isn't parsed; treat it as absent rather than
    failing the call over a header."""
    grader = _grader_with(
        _status_response(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        "- adds a TTL",
        json.dumps({"passed": True, "reasoning": "ok"}),
    )

    await grader.grade("diff", "question", "answer")

    assert no_sleep == [1.0]
