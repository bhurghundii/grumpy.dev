"""Grading, behind a protocol so callers never touch the model directly.

The flow is an "exam sheet": a session carries a fixed set of scoped
questions about the diff (the first always the high-level overview), and the
developer answers all of them at once. Two responsibilities live here:

  1. generate_exam: given the diff, produce the scoped questions. The
     first question is always HIGH_LEVEL_QUESTION; the model writes the
     rest, each targeting a specific part of the change.
  2. grade_exam: given the diff, the questions, and the developer's answers,
     mark each answer pass/fail in one call and return per-question marks
     plus an overall reasoning. Marking is deliberately lenient (high-level
     acceptance): only a wrong or empty answer fails.

Grading first interprets the diff on its own (no answers in view) and marks
against that interpretation. The split matters: a single call that sees the
answers alongside the diff rationalises toward them and produces confident
false passes. Generating the interpretation blind is what keeps the marking
honest.

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
from typing import Protocol

import httpx

MODEL = "claude-opus-5"
PROMPT_VERSION = "v6"

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"

# The fixed first question on every exam sheet. Kept here (not in
# app/questions.py) so the single module that generates the sheet owns both
# the fixed question and the model-written ones.
HIGH_LEVEL_QUESTION = "How does this change work on a high level?"

# Thinking is on by default on claude-opus-5 and shares max_tokens with the
# response text — too low a value risks truncating the answer mid-thought.
# Deliberately not disabling thinking: doing so has its own documented
# failure modes (tool calls emitted as plain text, <thinking> tag leakage
# into visible output), neither of which this grader needs to risk.
#
# 16000 is the documented default for non-streaming requests and sits well
# inside the 120s client timeout; max_tokens is a ceiling, not a
# reservation, so raising it costs nothing on responses that don't need it.
# The exam-sheet marking (one short note per question plus a summary) and
# question generation both fit comfortably.
_MAX_TOKENS = 16000

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

_EXAM_GEN_SYSTEM_PROMPT = """\
You are setting a short exam that checks whether a developer understands a \
code diff they are responsible for.

Write exactly {count} questions about this diff. Each must be answerable \
from the diff alone, and each must target a specific, different part of the \
change — a particular added function, a changed condition, a removed line, \
a new config value. Ask why something is done, what would break if it were \
wrong, or what a changed line now does — not yes/no questions, and not \
questions answered by reading one line back verbatim.

Do not ask a broad "what does this change do overall" question: that \
question is already asked separately and must not be duplicated here. Order \
the questions roughly in the order the relevant code appears in the diff. \
Keep each to a single sentence."""

_EXAM_QUESTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["questions"],
    "additionalProperties": False,
}

_EXAM_GRADE_PREAMBLE = """\
You are marking a developer's exam sheet about a code change. You are given \
an independent interpretation of the diff, produced without seeing any of \
the answers, so it is not biased toward them. Mark each answer against that \
interpretation."""

# The marking rule: lenient / high-level acceptance. A correct-but-partial
# or vague answer passes; only a wrong or empty one fails.
_EXAM_GRADE_RULE = """\
Marking rule (decides each `passed`, and is not affected by the tone \
guidance below):
- Produce exactly one mark per question, in the same order as the \
questions, and nothing else.
- An answer that states something the diff does not do fails.
- An answer that is vague, partial, or high-level still passes, as long as \
it is recognisably about what that question asked and nothing in it is \
wrong. Accept answers at a high level — do not demand completeness or \
precise detail.
- An empty answer, or one with nothing specific to this change in it (e.g. \
"fixes stuff", "see the diff"), fails: there is nothing to check.
- Terse phrasing is fine. Poorly written or non-native English phrasing is \
fine. Wrong content is not."""

# MEANIEMODE=true: the scathing "grumpy" roast persona, in the notes/summary
# only — never in whether a mark passed.
_EXAM_TONE_MEAN = """\
You are "grumpy" — live up to the name in each `note` and in `reasoning`, \
but only there; every `passed` is decided purely by the marking rule above.
- For a passed answer: one plain clause confirming what they got right. No \
routine praise.
- For a failed answer: be scathing and specific — name exactly which claim \
from the interpretation the answer contradicted or ignored, so the insult \
and the explanation are the same sentence.
`reasoning` is one or two sentences summarising how they did overall.

Respond with your marks."""

# MEANIEMODE=false (default): direct and professional — safe for an
# enterprise deployment with zero config.
_EXAM_TONE_PROFESSIONAL = """\
Keep every `note` and `reasoning` direct, specific, and professional — no \
sarcasm, mockery, or personal remarks, regardless of the marks.
- For a passed answer: one plain clause confirming what they got right. No \
routine praise.
- For a failed answer: state plainly what was wrong or missing, naming the \
claim from the interpretation it contradicted or ignored, like a terse \
code-review comment.
`reasoning` is one or two sentences summarising how they did overall.

Respond with your marks."""


def _exam_grade_system_prompt(*, meaniemode: bool) -> str:
    tone = _EXAM_TONE_MEAN if meaniemode else _EXAM_TONE_PROFESSIONAL
    return f"{_EXAM_GRADE_PREAMBLE}\n\n{_EXAM_GRADE_RULE}\n\n{tone}"


_EXAM_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "marks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "passed": {"type": "boolean"},
                    "note": {"type": "string"},
                },
                "required": ["passed", "note"],
                "additionalProperties": False,
            },
        },
        "reasoning": {"type": "string"},
    },
    "required": ["marks", "reasoning"],
    "additionalProperties": False,
}

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


@dataclass
class QuestionMark:
    """One question's result: whether the answer passed and a short note on
    why, shown beside the question after grading."""

    passed: bool
    note: str


@dataclass
class ExamResult:
    """The marked sheet. `score` is how many answers passed; app/web.py
    compares it against PASSINGMARKS to decide the session verdict."""

    marks: list[QuestionMark] = field(default_factory=list)
    reasoning: str = ""
    model: str | None = None
    prompt_version: str | None = None

    @property
    def score(self) -> int:
        return sum(1 for mark in self.marks if mark.passed)


class Grader(Protocol):
    async def generate_exam(self, diff: str, count: int) -> list[str]: ...

    async def grade_exam(
        self, diff: str, questions: list[str], answers: list[str]
    ) -> ExamResult: ...


class GradingError(Exception):
    """Raised when the grader can't produce a result — a malformed model
    response, a refusal, or a failed call. The caller must treat the
    session as still pending and let the developer resubmit; it must never
    be treated as a pass or a fail."""


class FakeGrader:
    """Deliberately dumb: marks an answer passed only if it contains the
    marker string. Proves the wiring end to end; it is not a real judgment.
    FAKE_GRADER=true is what `make seed` and the whole test suite run on, so
    this is the path that exercises the exam page during development."""

    MARKER = "looks-good"

    async def generate_exam(self, diff: str, count: int) -> list[str]:
        questions = [HIGH_LEVEL_QUESTION]
        for n in range(2, max(count, 1) + 1):
            questions.append(f"FAKE scoped question {n}?")
        return questions

    async def grade_exam(
        self, diff: str, questions: list[str], answers: list[str]
    ) -> ExamResult:
        marks = []
        for answer in answers:
            if self.MARKER in answer:
                marks.append(QuestionMark(passed=True, note=f"contains '{self.MARKER}'"))
            else:
                marks.append(QuestionMark(passed=False, note=f"missing '{self.MARKER}'"))
        passed = sum(1 for m in marks if m.passed)
        return ExamResult(marks=marks, reasoning=f"{passed}/{len(marks)} answers contained the marker")


class RealGrader:
    """Grader against the Anthropic Messages API.

    client_factory exists as a seam for evals/tests to replay recorded
    responses (via a custom httpx transport) instead of hitting the real
    API — it doesn't change what this class does, only how it gets an
    httpx.AsyncClient.

    meaniemode selects the marking tone: off (default) is direct and
    professional, on is the scathing "grumpy" roast persona. See
    app/config.py's MEANIEMODE. It never changes whether a mark passed.
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

    async def generate_exam(self, diff: str, count: int) -> list[str]:
        """Returns HIGH_LEVEL_QUESTION followed by up to count-1 model-written
        scoped questions. Tolerant of the model returning fewer: the sheet is
        just shorter (app/web.py clamps the pass threshold to the sheet's
        length), never empty, since the high-level question is always first."""
        scoped_wanted = max(count - 1, 0)
        if scoped_wanted == 0:
            return [HIGH_LEVEL_QUESTION]
        try:
            async with self._client_factory() as client:
                data = await self._call(
                    client,
                    system=_EXAM_GEN_SYSTEM_PROMPT.format(count=scoped_wanted),
                    user_content=f"Diff:\n\n{diff}",
                    output_schema=_EXAM_QUESTIONS_SCHEMA,
                )
                _check_stop_reason(data)
                scoped = _parse_questions(_extract_text(data))
        except GradingError:
            raise
        except Exception as exc:  # httpx errors, timeouts, anything unexpected
            raise GradingError(f"exam generation call failed: {exc}") from exc
        return [HIGH_LEVEL_QUESTION, *scoped[:scoped_wanted]]

    async def grade_exam(
        self, diff: str, questions: list[str], answers: list[str]
    ) -> ExamResult:
        try:
            async with self._client_factory() as client:
                interpretation = await self._interpret(client, diff)
                return await self._mark(client, interpretation, questions, answers)
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

    async def _mark(
        self,
        client: httpx.AsyncClient,
        interpretation: str,
        questions: list[str],
        answers: list[str],
    ) -> ExamResult:
        sheet = "\n\n".join(
            f"Question {n}: {q}\nAnswer {n}: {a}"
            for n, (q, a) in enumerate(zip(questions, answers, strict=False), start=1)
        )
        user_content = (
            f"Interpretation of the change:\n{interpretation}\n\n"
            f"Exam sheet ({len(questions)} questions):\n{sheet}\n\n"
            "Mark each answer per the marking rule above, one mark per "
            "question, in order."
        )
        data = await self._call(
            client,
            system=_exam_grade_system_prompt(meaniemode=self._meaniemode),
            user_content=user_content,
            output_schema=_EXAM_VERDICT_SCHEMA,
        )
        _check_stop_reason(data)
        parsed = _parse_exam_verdict(_extract_text(data))
        marks = _align_marks(parsed["marks"], len(questions))
        return ExamResult(
            marks=marks,
            reasoning=parsed["reasoning"],
            model=MODEL,
            prompt_version=PROMPT_VERSION,
        )

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
    # Checked here rather than left to the parser: a truncated response is
    # still well-formed JSON right up to where it stops, so the parser
    # reports it as "model output was not valid JSON" — which reads like the
    # model misbehaved when the real cause is _MAX_TOKENS being too low for
    # what was asked. Name the budget instead.
    if data.get("stop_reason") == "max_tokens":
        raise GradingError(f"model hit the {_MAX_TOKENS}-token cap before finishing")


def _extract_text(data: dict) -> str:
    for block in data.get("content", []):
        if block.get("type") == "text":
            return block.get("text", "")
    raise GradingError(f"model response contained no text block: {data!r}")


def _align_marks(raw_marks: list[dict], question_count: int) -> list[QuestionMark]:
    """One QuestionMark per question, however many marks the model returned.
    A model that returns fewer marks than questions leaves the unmarked ones
    failed (there is no evidence they passed); extra marks are dropped. This
    keeps marks and questions index-aligned for display and keeps the score
    out of (0 .. question_count)."""
    marks: list[QuestionMark] = []
    for i in range(question_count):
        if i < len(raw_marks):
            marks.append(QuestionMark(passed=raw_marks[i]["passed"], note=raw_marks[i]["note"]))
        else:
            marks.append(QuestionMark(passed=False, note="No mark returned for this question."))
    return marks


def _extract_json_object(text: str) -> dict:
    """Shared defensive parse: the model will occasionally wrap output in
    code fences or add a preamble even when asked for JSON only. Strip
    fences, take the substring between the first '{' and the last '}', and
    validate it is a JSON object. Any failure raises GradingError — a
    result must never be conjured from garbage."""
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
    return parsed


def _parse_questions(text: str) -> list[str]:
    """The scoped questions from generate_exam. Non-string or blank entries
    are dropped; an empty result is allowed (the sheet falls back to just the
    high-level question) rather than raising, so a terse diff never blocks
    session creation."""
    parsed = _extract_json_object(text)
    raw = parsed.get("questions")
    if not isinstance(raw, list):
        raise GradingError(f"model output missing a 'questions' list: {parsed!r}")
    return [q.strip() for q in raw if isinstance(q, str) and q.strip()]


def _parse_exam_verdict(text: str) -> dict:
    """{"marks": [{"passed": bool, "note": str}, ...], "reasoning": str}.
    Malformed individual marks are dropped (a missing mark becomes a failed
    one upstream, in _align_marks); a response with no marks array, or no
    reasoning, raises."""
    parsed = _extract_json_object(text)
    raw_marks = parsed.get("marks")
    if not isinstance(raw_marks, list):
        raise GradingError(f"model output missing a 'marks' list: {parsed!r}")
    if not isinstance(parsed.get("reasoning"), str):
        raise GradingError(f"model output missing string 'reasoning': {parsed!r}")

    marks = [
        {"passed": m["passed"], "note": m["note"].strip()}
        for m in raw_marks
        if isinstance(m, dict)
        and isinstance(m.get("passed"), bool)
        and isinstance(m.get("note"), str)
    ]
    return {"marks": marks, "reasoning": parsed["reasoning"]}
