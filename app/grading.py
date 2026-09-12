"""Grading, behind a protocol so callers never touch the model directly.

Two model calls, in that order, per the phase 4 spec — never merged:

  1. Interpretation: the diff only, no answer. Output is a short free-text
     list of discrete claims about what the change does.
  2. Comparison: the interpretation from (1) plus the developer's answer.
     Output is {"passed": bool, "reasoning": str}.

The split matters: a single call that sees the answer alongside the diff
rationalises toward the answer and produces confident false passes.
Generating the interpretation blind is what keeps the comparison honest.

async httpx against the raw Messages API — never the anthropic SDK, sync
or async, and never `requests`. This project's own conventions (§7 from
phase 1) call for httpx specifically for any future model/HTTP call, and
every dependency in this project so far has been the raw-protocol version
of its category (psycopg3 + raw SQL over an ORM, httpx over an SDK) —
adding the SDK now would be the first exception to that, not a neutral
choice.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

MODEL = "claude-opus-5"
PROMPT_VERSION = "v4"

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"

# Thinking is on by default on claude-opus-5 and shares max_tokens with the
# response text — too low a value risks truncating the answer mid-thought.
# Deliberately not disabling thinking: doing so has its own documented
# failure modes (tool calls emitted as plain text, <thinking> tag leakage
# into visible output), neither of which this grader needs to risk.
_MAX_TOKENS = 4096

# Transient, self-resolving conditions worth retrying: 429 rate limit, 529
# overloaded_error, and genuine server-side faults. Deliberately excludes
# 400 (a malformed request retries identically), 401/403 (a bad API key
# will not fix itself), and 404 — retrying those just adds latency to a
# failure the developer is already waiting on.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 529})

# Three attempts total, not three retries. Grading is synchronous inside
# the developer's HTTP request, so the ceiling on total added latency
# (~3s of backoff, plus the request timeout) matters more than exhausting
# every possibility.
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0
# A Retry-After longer than this is not worth holding a request open for;
# fail fast and let the developer resubmit instead.
_MAX_RETRY_AFTER_SECONDS = 10.0

_INTERPRETATION_SYSTEM_PROMPT = """\
You are analysing a code diff in isolation. You will not see any developer \
answer — do not speculate about one.

List the discrete, verifiable claims about what this diff changes, as a \
short bulleted list. Base every claim strictly on what the diff shows. Do \
not infer intent beyond what the diff makes visible."""

# Shared by both comparison-prompt tone variants below — the grading rule
# itself never changes with MEANIEMODE, only how `reasoning` is worded.
_COMPARISON_GRADING_RULE = """\
You are grading whether a developer's answer demonstrates real \
understanding of a code change. You are given an independent \
interpretation of the diff, produced without seeing the answer, so it is \
not biased toward it.

Grading rule: weight contradictions over missing coverage. This rule \
decides passed/failed and is not affected by anything below.
- An answer that states something the diff does not do fails.
- An answer that is correct but covers less than the full interpretation \
still passes, unless what it omits is the point of the change.
- Terse phrasing is fine. Poorly written or non-native English phrasing is \
fine. Wrong content is not."""

# MEANIEMODE=true: today's scathing "grumpy" roast persona.
_COMPARISON_TONE_MEAN = """\
You are "grumpy" — live up to the name in `reasoning`, but only there; \
`passed` is decided purely by the grading rule above.
- If passed is true: one or two plain sentences confirming what they got \
right. No routine praise.
- If passed is false: be scathing. Sarcastic, exasperated, personally \
offended by the effort level — and specific. Cite exactly which claims \
from the interpretation the answer contradicted or ignored, by name, so \
the insult and the explanation are the same sentence. A developer reading \
a failed verdict must walk away both mocked and correctly informed about \
what they missed; an insult with no citation attached is a wasted one, \
and an explanation with no bite defeats the point of asking for it.

Respond with your verdict."""

# MEANIEMODE=false (default): direct and professional — safe for an
# enterprise deployment with zero config. Same passed=true branch as the
# mean variant; only the failed-verdict wording differs.
_COMPARISON_TONE_PROFESSIONAL = """\
Keep `reasoning` direct, specific, and professional — no sarcasm, mockery, \
or personal remarks, regardless of the verdict.
- If passed is true: one or two plain sentences confirming what they got \
right. No routine praise.
- If passed is false: state plainly and specifically what was wrong or \
missing. Cite exactly which claims from the interpretation the answer \
contradicted or ignored, by name, so the explanation is concrete rather \
than generic. Read like a terse, professional code-review comment — \
matter-of-fact, not harsh.

Respond with your verdict."""

_COMPARISON_SYSTEM_PROMPT_MEAN = f"{_COMPARISON_GRADING_RULE}\n\n{_COMPARISON_TONE_MEAN}"
_COMPARISON_SYSTEM_PROMPT_PROFESSIONAL = (
    f"{_COMPARISON_GRADING_RULE}\n\n{_COMPARISON_TONE_PROFESSIONAL}"
)

_TUTORIAL_SYSTEM_PROMPT = """\
A developer just got a question about this diff wrong, and asked for a \
step-by-step breakdown instead of just retrying blind. Explain what the \
change does, one step at a time, in plain teaching language — the goal is \
for them to actually understand it, not to re-ask the original question \
in different words.

Base every step strictly on what the diff shows. Do not infer intent \
beyond what the diff makes visible. Do not be "grumpy" or sarcastic here —
this is a teaching moment, not a grading one; save the personality for \
the verdict, not the lesson.

The diff below is presented with a line number at the start of every \
line. Each step must be anchored to the range of those numbers it is \
teaching, so the developer is shown exactly the lines you are talking \
about while they read the step:
- `start_line` and `end_line` are inclusive, and both must be line \
numbers that actually appear in the numbered diff.
- Keep each range tight — the few lines the step is really about, not the \
whole file. Include an unchanged line or two around them when that's what \
makes the change readable.
- Order the steps so they build on each other, and make each `title` a \
short phrase naming what that step covers.
- Use between two and six steps. One step is a wall of text, which is the \
thing this format exists to avoid; more than six and the reader is \
clicking, not learning.
- Do not repeat the line numbers inside `body`; the developer sees the \
code itself next to it. Keep each `body` to a short paragraph or two."""

_EXPLAIN_BACK_SYSTEM_PROMPT = """\
A developer was given a step-by-step breakdown of a diff after getting a \
question about it wrong. You are checking whether their explanation, in \
their own words, shows basic comprehension of the breakdown — this is a \
looser, more lenient check than a real grading pass, meant to confirm \
they engaged with the lesson, not to gate anything.

Grading rule:
- An explanation that clearly contradicts the breakdown (states something \
the diff does not do) fails.
- An explanation that is thin, partial, or only covers some of the steps \
still passes, as long as nothing in it is wrong.
- Terse phrasing is fine. Poorly written or non-native English phrasing is \
fine. Give the benefit of the doubt more readily than a strict grader \
would.

Unlike a failed answer verdict, `reasoning` here should be encouraging, \
not scathing, regardless of whether passed is true or false — this \
result never blocks anything; the developer gets a fresh attempt at the \
original question either way, so the tone should help them try again, \
not punish them.

Respond with your verdict."""

_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "reasoning": {"type": "string"},
    },
    "required": ["passed", "reasoning"],
    "additionalProperties": False,
}

# No minItems/maxItems, deliberately: the Messages API's json_schema
# format rejects both outright ("'minItems' values other than 0 or 1 are
# not supported", "property 'maxItems' is not supported"), so a step count
# cannot be enforced here at all — the "two to six steps" ask lives in the
# prompt instead. Nothing downstream depends on the count holding:
# _parse_tutorial rejects a response with no usable steps, the page paginates
# however many arrive, and _MAX_TOKENS bounds the response (and so the
# JSONB row) regardless of what the model attempts.
_TUTORIAL_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["title", "body", "start_line", "end_line"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["steps"],
    "additionalProperties": False,
}

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


@dataclass
class GradeResult:
    passed: bool
    reasoning: str
    model: str | None = None
    prompt_version: str | None = None


@dataclass
class TutorialStep:
    """One step of a tutorial walkthrough, anchored to an inclusive,
    1-based line range of the diff as split by `str.splitlines()` — the
    same indexing app/web.py:_classify_diff produces, which is what lets
    the page show the step's own slice of code beside it.

    start_line/end_line are None when the model's anchor was unusable and
    _parse_tutorial dropped it: the prose is still worth showing, just
    without a slice.
    """

    title: str
    body: str
    start_line: int | None = None
    end_line: int | None = None


@dataclass
class TutorialBreakdown:
    """`text` is the flattened prose form of `steps`, kept because it's
    what grade_explanation() is given later and what the tutorials.breakdown
    column stores — see _flatten_steps."""

    text: str
    steps: list[TutorialStep] = field(default_factory=list)
    model: str | None = None
    prompt_version: str | None = None


class Grader(Protocol):
    async def grade(self, diff: str, question: str, answer: str) -> GradeResult: ...

    async def generate_tutorial(self, diff: str, question: str) -> TutorialBreakdown: ...

    async def grade_explanation(
        self, diff: str, breakdown: str, explanation: str
    ) -> GradeResult: ...


class GradingError(Exception):
    """Raised when the grader can't produce a verdict — a malformed model
    response, a refusal, or a failed call. The caller must treat the
    session as still pending and let the developer resubmit; it must never
    be treated as a pass or a fail."""


class FakeGrader:
    """Deliberately dumb: passes only if the answer contains the marker
    string. Proves the wiring end to end; it is not a real judgment."""

    MARKER = "looks-good"
    EXPLAIN_MARKER = "i-understand"

    async def grade(self, diff: str, question: str, answer: str) -> GradeResult:
        if self.MARKER in answer:
            return GradeResult(passed=True, reasoning=f"answer contains '{self.MARKER}'")
        return GradeResult(passed=False, reasoning=f"answer is missing '{self.MARKER}'")

    async def generate_tutorial(self, diff: str, question: str) -> TutorialBreakdown:
        """Structured like the real thing — three steps over three equal
        slices of the diff — because FAKE_GRADER=true is what `make seed`
        and the whole test suite run on, so this is the only path that
        exercises the step-through page during development."""
        total = max(len(diff.splitlines()), 1)
        size = max(total // 3, 1)
        thirds = [(1, size), (size + 1, size * 2), (size * 2 + 1, total)]

        steps = []
        for n, (start, end) in enumerate(thirds, start=1):
            if start > total:  # diff too short to fill three slices
                break
            steps.append(
                TutorialStep(
                    title=f"FAKE step {n}",
                    body=(
                        f"FAKE step-by-step breakdown for: {question}"
                        if n == 1
                        else f"FAKE walkthrough of diff lines {start}-{min(end, total)}."
                    ),
                    start_line=start,
                    end_line=min(end, total),
                )
            )
        return TutorialBreakdown(text=_flatten_steps(steps), steps=steps)

    async def grade_explanation(
        self, diff: str, breakdown: str, explanation: str
    ) -> GradeResult:
        if self.EXPLAIN_MARKER in explanation:
            return GradeResult(
                passed=True, reasoning=f"explanation contains '{self.EXPLAIN_MARKER}'"
            )
        return GradeResult(
            passed=False, reasoning=f"explanation is missing '{self.EXPLAIN_MARKER}'"
        )


class RealGrader:
    """Two-call grader against the Anthropic Messages API.

    client_factory exists as a seam for evals/tests to replay recorded
    responses (via a custom httpx transport) instead of hitting the real
    API — it doesn't change what this class does, only how it gets an
    httpx.AsyncClient.

    meaniemode selects the comparison prompt's failed-verdict tone: off
    (default) is direct and professional, on is the scathing "grumpy" roast
    persona. See app/config.py's MEANIEMODE.
    """

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 120.0,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        meaniemode: bool = False,
    ) -> None:
        self._api_key = api_key
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(timeout=timeout)
        )
        self._meaniemode = meaniemode

    async def grade(self, diff: str, question: str, answer: str) -> GradeResult:
        try:
            async with self._client_factory() as client:
                interpretation = await self._interpret(client, diff)
                return await self._compare(client, interpretation, answer)
        except GradingError:
            raise
        except Exception as exc:  # httpx errors, timeouts, anything unexpected
            raise GradingError(f"grading call failed: {exc}") from exc

    async def _interpret(self, client: httpx.AsyncClient, diff: str) -> str:
        data = await self._call(
            client,
            system=_INTERPRETATION_SYSTEM_PROMPT,
            user_content=f"Diff:\n\n{diff}",
        )
        _check_stop_reason(data)
        text = _extract_text(data).strip()
        if not text:
            raise GradingError("model returned an empty interpretation")
        return text

    async def _compare(
        self, client: httpx.AsyncClient, interpretation: str, answer: str
    ) -> GradeResult:
        user_content = (
            f"Interpretation of the change:\n{interpretation}\n\n"
            f"Developer's answer:\n{answer}\n\n"
            "Does the answer demonstrate real understanding, per the grading "
            "rule above?"
        )
        data = await self._call(
            client,
            system=(
                _COMPARISON_SYSTEM_PROMPT_MEAN
                if self._meaniemode
                else _COMPARISON_SYSTEM_PROMPT_PROFESSIONAL
            ),
            user_content=user_content,
            output_schema=_VERDICT_SCHEMA,
        )
        _check_stop_reason(data)
        verdict = _parse_verdict(_extract_text(data))
        return GradeResult(
            passed=verdict["passed"],
            reasoning=verdict["reasoning"],
            model=MODEL,
            prompt_version=PROMPT_VERSION,
        )

    async def generate_tutorial(self, diff: str, question: str) -> TutorialBreakdown:
        try:
            async with self._client_factory() as client:
                data = await self._call(
                    client,
                    system=_TUTORIAL_SYSTEM_PROMPT,
                    user_content=(
                        f"Numbered diff:\n\n{_number_diff(diff)}\n\n"
                        f"Question the developer got wrong:\n{question}"
                    ),
                    output_schema=_TUTORIAL_SCHEMA,
                )
                _check_stop_reason(data)
                steps = _parse_tutorial(_extract_text(data), len(diff.splitlines()))
                return TutorialBreakdown(
                    text=_flatten_steps(steps),
                    steps=steps,
                    model=MODEL,
                    prompt_version=PROMPT_VERSION,
                )
        except GradingError:
            raise
        except Exception as exc:  # httpx errors, timeouts, anything unexpected
            raise GradingError(f"tutorial generation call failed: {exc}") from exc

    async def grade_explanation(
        self, diff: str, breakdown: str, explanation: str
    ) -> GradeResult:
        try:
            async with self._client_factory() as client:
                user_content = (
                    f"Tutorial breakdown shown to the developer:\n{breakdown}\n\n"
                    f"Developer's explanation back:\n{explanation}\n\n"
                    "Does this show basic comprehension, per the lenient rubric above?"
                )
                data = await self._call(
                    client,
                    system=_EXPLAIN_BACK_SYSTEM_PROMPT,
                    user_content=user_content,
                    output_schema=_VERDICT_SCHEMA,
                )
                _check_stop_reason(data)
                verdict = _parse_verdict(_extract_text(data))
                return GradeResult(
                    passed=verdict["passed"],
                    reasoning=verdict["reasoning"],
                    model=MODEL,
                    prompt_version=PROMPT_VERSION,
                )
        except GradingError:
            raise
        except Exception as exc:  # httpx errors, timeouts, anything unexpected
            raise GradingError(f"explanation grading call failed: {exc}") from exc

    async def _call(
        self,
        client: httpx.AsyncClient,
        *,
        system: str,
        user_content: str,
        output_schema: dict | None = None,
    ) -> dict:
        body: dict = {
            "model": MODEL,
            "max_tokens": _MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": user_content}],
        }
        if output_schema is not None:
            body["output_config"] = {"format": {"type": "json_schema", "schema": output_schema}}

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }

        # Grading happens synchronously inside the developer's request, and
        # a GradingError surfaces to them as "please try again" with no
        # explanation. A single 429 or a 529 overloaded_error — both routine
        # and both self-resolving in seconds — should not be something they
        # have to notice and retry by hand.
        last_exc: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = await client.post(_API_URL, headers=headers, json=body)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in _RETRYABLE_STATUS:
                    raise
                last_exc = exc
                delay = _retry_delay(exc.response, attempt)
            except httpx.TransportError as exc:
                # Connect/read timeouts and dropped connections. Not
                # httpx.HTTPError, which would also catch the status errors
                # already handled (and re-raised) above.
                last_exc = exc
                delay = _backoff_delay(attempt)

            if attempt == _MAX_ATTEMPTS - 1:
                break
            await asyncio.sleep(delay)

        raise GradingError(
            f"model API call failed after {_MAX_ATTEMPTS} attempts: {last_exc}"
        ) from last_exc


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff: 1s, then 2s. `attempt` is 0-based."""
    return _BACKOFF_BASE_SECONDS * (2**attempt)


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Honour Retry-After when the API sends one, since it knows better
    than a fixed curve does, but never wait longer than
    _MAX_RETRY_AFTER_SECONDS — the developer is sitting on a synchronous
    request. Only the delta-seconds form is parsed; the HTTP-date form is
    not used by this API, and treating an unparseable value as "no header"
    falls back to ordinary backoff rather than failing.
    """
    raw = response.headers.get("retry-after")
    if raw is not None:
        try:
            return min(max(float(raw), 0.0), _MAX_RETRY_AFTER_SECONDS)
        except ValueError:
            pass
    return _backoff_delay(attempt)


def _check_stop_reason(data: dict) -> None:
    if data.get("stop_reason") == "refusal":
        raise GradingError("model declined to respond (safety refusal)")


def _extract_text(data: dict) -> str:
    for block in data.get("content", []):
        if block.get("type") == "text":
            return block.get("text", "")
    raise GradingError(f"model response contained no text block: {data!r}")


def _number_diff(diff: str) -> str:
    """Prefixes every line with its 1-based index, so the model can anchor
    a step to a line range it can actually see rather than one it has to
    count. The indexing matches `str.splitlines()`, which is also what
    app/web.py:_classify_diff uses — that shared basis is what makes an
    anchor produced here resolvable to a slice there."""
    return "\n".join(f"{n}\t{line}" for n, line in enumerate(diff.splitlines(), start=1))


def _flatten_steps(steps: list[TutorialStep]) -> str:
    """The prose form stored in tutorials.breakdown. grade_explanation()
    takes the breakdown as a plain string, and pre-V5 rows have only that
    column, so keeping a readable flattening means neither has to learn
    about steps."""
    return "\n\n".join(f"{n}. {step.title}\n\n{step.body}" for n, step in enumerate(steps, start=1))


def _parse_step(raw: Any, line_count: int) -> TutorialStep | None:
    """One step, or None if it isn't salvageable. A bad line range costs
    the step its code slice, not the step itself — the prose is still worth
    reading. Missing prose, on the other hand, leaves nothing to show."""
    if not isinstance(raw, dict):
        return None
    title, body = raw.get("title"), raw.get("body")
    if not isinstance(title, str) or not isinstance(body, str):
        return None
    if not title.strip() or not body.strip():
        return None

    start, end = raw.get("start_line"), raw.get("end_line")
    # bool is an int subclass; True would otherwise clamp to line 1.
    if isinstance(start, bool) or isinstance(end, bool):
        start = end = None
    elif isinstance(start, int) and isinstance(end, int):
        if start > end:
            start, end = end, start
        start, end = max(start, 1), min(end, line_count)
        if start > end:  # entirely outside the diff — no usable anchor
            start = end = None
    else:
        start = end = None

    return TutorialStep(title=title.strip(), body=body.strip(), start_line=start, end_line=end)


def _parse_tutorial(text: str, line_count: int) -> list[TutorialStep]:
    """Defensive JSON parsing for the tutorial walkthrough, on the same
    terms as _parse_verdict below: strip fences, take the substring between
    the first '{' and the last '}', validate the shape. Individual
    malformed steps are dropped, but a response with no usable steps left
    raises rather than producing an empty walkthrough — same bar as a
    verdict, which must never be conjured from garbage."""
    cleaned = _FENCE_RE.sub("", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise GradingError(f"could not find a JSON object in model output: {text!r}")

    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise GradingError(f"model output was not valid JSON: {text!r}") from exc

    if not isinstance(parsed, dict):
        raise GradingError(f"model output was not a JSON object: {parsed!r}")
    raw_steps = parsed.get("steps")
    if not isinstance(raw_steps, list):
        raise GradingError(f"model output missing a 'steps' list: {parsed!r}")

    steps = [step for step in (_parse_step(raw, line_count) for raw in raw_steps) if step]
    if not steps:
        raise GradingError(f"model output contained no usable tutorial steps: {parsed!r}")
    return steps


def _parse_verdict(text: str) -> dict:
    """Defensive JSON parsing: the model will occasionally wrap output in
    code fences or add a preamble even when asked for JSON only. Strip
    fences, take the substring between the first '{' and the last '}', and
    validate the required shape. Any failure raises GradingError — this
    must never silently produce a verdict from garbage."""
    cleaned = _FENCE_RE.sub("", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise GradingError(f"could not find a JSON object in model output: {text!r}")

    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise GradingError(f"model output was not valid JSON: {text!r}") from exc

    if not isinstance(parsed, dict):
        raise GradingError(f"model output was not a JSON object: {parsed!r}")
    if not isinstance(parsed.get("passed"), bool):
        raise GradingError(f"model output missing boolean 'passed': {parsed!r}")
    if not isinstance(parsed.get("reasoning"), str):
        raise GradingError(f"model output missing string 'reasoning': {parsed!r}")

    return parsed
