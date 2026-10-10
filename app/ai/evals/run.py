"""make eval: runs app/ai/evals/cases/ against RealGrader and prints pass/fail.

Model calls are recorded under app/ai/evals/cassettes/; delete one to
re-record. MODEL_API_KEY is only needed to record. Deliberately bypasses
get_settings() so it needs no other app config.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

from app.ai.evals.cassette import CassetteTransport, cassette_key
from app.ai.grading import GradingError, RealGrader

CASES_DIR = Path(__file__).resolve().parent / "cases"


async def run_case(case: dict) -> tuple[bool, str]:
    key = cassette_key(case["diff"], case["question"], case["answer"])
    grader = RealGrader(
        api_key=os.environ.get("MODEL_API_KEY", ""),
        client_factory=lambda: httpx.AsyncClient(transport=CassetteTransport(key)),
    )
    # The same two calls the app makes per screen: interpret, then grade.
    try:
        interpretation = await grader.interpret(case["diff"])
        mark = await grader.grade_answer(interpretation, case["question"], case["answer"])
    except GradingError as exc:
        return False, f"GRADING ERROR: {exc}"

    ok = mark.passed == case["expected_passed"]
    detail = f"passed={mark.passed} expected={case['expected_passed']} note={mark.note!r}"
    return ok, detail


async def main() -> int:
    case_paths = sorted(CASES_DIR.glob("*.json"))
    if not case_paths:
        print("no eval cases found in app/ai/evals/cases/", file=sys.stderr)
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
