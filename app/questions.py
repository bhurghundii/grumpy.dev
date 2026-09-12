"""Question generation, behind a protocol so a future phase can swap in a
model-backed implementation without touching any call site. Phase 4
explicitly keeps the question fixed ("There is no question generation in
this phase") — only the grader became real.

generate() is async even though FixedQuestionGenerator has nothing to await:
the eventual replacement will call out to a model over HTTP (async httpx,
per the project's conventions), and changing the protocol's signature later
would mean changing every call site too — exactly what this seam is meant
to avoid.
"""

from __future__ import annotations

from typing import Protocol


class QuestionGenerator(Protocol):
    async def generate(self, diff: str) -> str: ...


class FixedQuestionGenerator:
    """Returns the same question for every diff. No model, no HTTP client."""

    # Phase 4 gives this exact wording — updated from phase 2's placeholder
    # text now that grading (and therefore the eval set) depends on it
    # being the real, stable question.
    _QUESTION = "What does this change do, and what breaks if it's wrong?"

    async def generate(self, diff: str) -> str:
        return self._QUESTION
