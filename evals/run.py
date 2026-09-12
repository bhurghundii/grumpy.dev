"""make eval — runs the eval cases in evals/cases/ against RealGrader and
prints pass/fail per case.

Model calls are recorded to disk under evals/cassettes/, keyed on a hash
of (diff, question, answer) — delete a cassette file to force a
re-record against the real API for that case. Requires MODEL_API_KEY to
be set to a real key the first time a case is recorded; replayed runs
need no key.

This intentionally does not go through app.config.get_settings() — it
only needs MODEL_API_KEY, not the full app config (DATABASE_URL,
GRUMPY_TOKEN, ...), so it stays usable standalone.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

from app.grading import GradingError, RealGrader
from evals.cassette import CassetteTransport, cassette_key

CASES_DIR = Path(__file__).resolve().parent / "cases"


async def run_case(case: dict) -> tuple[bool, str]:
    key = cassette_key(case["diff"], case["question"], case["answer"])
    grader = RealGrader(
        api_key=os.environ.get("MODEL_API_KEY", ""),
        client_factory=lambda: httpx.AsyncClient(transport=CassetteTransport(key)),
    )
    try:
        result = await grader.grade(case["diff"], case["question"], case["answer"])
    except GradingError as exc:
        return False, f"GRADING ERROR: {exc}"

    ok = result.passed == case["expected_passed"]
    detail = f"passed={result.passed} expected={case['expected_passed']} reasoning={result.reasoning!r}"
    return ok, detail


async def main() -> int:
    case_paths = sorted(CASES_DIR.glob("*.json"))
    if not case_paths:
        print("no eval cases found in evals/cases/", file=sys.stderr)
        return 1

    if not os.environ.get("MODEL_API_KEY"):
        cassettes_exist = any((Path(__file__).resolve().parent / "cassettes").glob("*.json"))
        if not cassettes_exist:
            print(
                "MODEL_API_KEY is not set and no cassettes exist yet — "
                "set MODEL_API_KEY to a real Anthropic API key to record them.",
                file=sys.stderr,
            )
            return 1

    all_ok = True
    for path in case_paths:
        case = json.loads(path.read_text())
        ok, detail = await run_case(case)
        all_ok = all_ok and ok
        print(f"[{'PASS' if ok else 'FAIL'}] {path.name}: {case['note']}")
        print(f"        {detail}")

    print()
    print("all cases passed" if all_ok else "one or more cases failed")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
