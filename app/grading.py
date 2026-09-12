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

import json
import re
from dataclasses import dataclass
from typing import Callable, Protocol

import httpx

MODEL = "claude-opus-5"
PROMPT_VERSION = "v2"

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"

# Thinking is on by default on claude-opus-5 and shares max_tokens with the
# response text — too low a value risks truncating the answer mid-thought.
# Deliberately not disabling thinking: doing so has its own documented
# failure modes (tool calls emitted as plain text, <thinking> tag leakage
# into visible output), neither of which this grader needs to risk.
_MAX_TOKENS = 4096

_INTERPRETATION_SYSTEM_PROMPT = """\
You are analysing a code diff in isolation. You will not see any developer \
answer — do not speculate about one.

List the discrete, verifiable claims about what this diff changes, as a \
short bulleted list. Base every claim strictly on what the diff shows. Do \
not infer intent beyond what the diff makes visible."""

_COMPARISON_SYSTEM_PROMPT = """\
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
fine. Wrong content is not.

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

_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "reasoning": {"type": "string"},
    },
    "required": ["passed", "reasoning"],
    "additionalProperties": False,
}

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


@dataclass
class GradeResult:
    passed: bool
    reasoning: str
    model: str | None = None
    prompt_version: str | None = None


class Grader(Protocol):
    async def grade(self, diff: str, question: str, answer: str) -> GradeResult: ...


class GradingError(Exception):
    """Raised when the grader can't produce a verdict — a malformed model
    response, a refusal, or a failed call. The caller must treat the
    session as still pending and let the developer resubmit; it must never
    be treated as a pass or a fail."""


class FakeGrader:
    """Deliberately dumb: passes only if the answer contains the marker
    string. Proves the wiring end to end; it is not a real judgment."""

    MARKER = "looks-good"

    async def grade(self, diff: str, question: str, answer: str) -> GradeResult:
        if self.MARKER in answer:
            return GradeResult(passed=True, reasoning=f"answer contains '{self.MARKER}'")
        return GradeResult(passed=False, reasoning=f"answer is missing '{self.MARKER}'")


class RealGrader:
    """Two-call grader against the Anthropic Messages API.

    client_factory exists as a seam for evals/tests to replay recorded
    responses (via a custom httpx transport) instead of hitting the real
    API — it doesn't change what this class does, only how it gets an
    httpx.AsyncClient.
    """

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 120.0,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._api_key = api_key
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(timeout=timeout)
        )

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
            system=_COMPARISON_SYSTEM_PROMPT,
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

        response = await client.post(
            _API_URL,
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": _API_VERSION,
                "content-type": "application/json",
            },
            json=body,
        )
        response.raise_for_status()
        return response.json()


def _check_stop_reason(data: dict) -> None:
    if data.get("stop_reason") == "refusal":
        raise GradingError("model declined to respond (safety refusal)")


def _extract_text(data: dict) -> str:
    for block in data.get("content", []):
        if block.get("type") == "text":
            return block.get("text", "")
    raise GradingError(f"model response contained no text block: {data!r}")


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
