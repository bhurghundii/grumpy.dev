"""app/evaluator.py: changed-line counting and the checks that reject a PR
before it's questioned. Pure functions over a diff string, no DB."""

from __future__ import annotations

import secrets

import pytest

from app.config import Settings
from app.evaluator import count_changed_lines, evaluate, question_count_for


def _file(path: str, added: int, removed: int = 0, context: int = 0) -> str:
    body = ["+line"] * added + ["-line"] * removed + [" line"] * context
    return "\n".join(
        [
            f"diff --git a/{path} b/{path}",
            "index 1111111..2222222 100644",
            f"--- a/{path}",
            f"+++ b/{path}",
            f"@@ -1,{removed + context} +1,{added + context} @@",
            *body,
        ]
    ) + "\n"


@pytest.fixture
def settings(monkeypatch) -> Settings:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/db")
    monkeypatch.setenv("GRUMPY_BASE_URL", "https://grumpy.example.com")
    monkeypatch.setenv("GRUMPY_TOKEN", secrets.token_urlsafe(16))
    monkeypatch.setenv("FAKE_GRADER", "true")
    return Settings()


def test_counts_added_and_removed_not_headers_or_context() -> None:
    assert count_changed_lines(_file("app/x.py", added=3, removed=2, context=5), []) == 5


def test_sums_across_files() -> None:
    diff = _file("a.py", added=4) + _file("b.py", added=0, removed=6)
    assert count_changed_lines(diff, []) == 10


def test_added_line_starting_with_plus_plus_counts() -> None:
    diff = _file("a.py", added=0).rstrip("\n") + "\n+++ not a header\n--- nor this\n"
    assert count_changed_lines(diff, []) == 2


def test_no_newline_marker_does_not_count() -> None:
    diff = _file("a.py", added=1) + "\\ No newline at end of file\n"
    assert count_changed_lines(diff, []) == 1


def test_excludes_match_full_path_and_basename() -> None:
    diff = _file("uv.lock", added=500) + _file("web/package-lock.json", added=500) + _file(
        "app/x.py", added=7
    )
    assert count_changed_lines(diff, ["*.lock", "package-lock.json"]) == 7


def test_rename_only_and_binary_count_zero() -> None:
    diff = (
        "diff --git a/old.py b/new.py\n"
        "similarity index 100%\n"
        "rename from old.py\n"
        "rename to new.py\n"
        "diff --git a/img.png b/img.png\n"
        "Binary files a/img.png and b/img.png differ\n"
    )
    assert count_changed_lines(diff, []) == 0


def test_at_limit_passes_over_limit_rejects(settings) -> None:
    assert evaluate(_file("a.py", added=1000), settings) is None
    reason = evaluate(_file("a.py", added=1001), settings)
    assert reason is not None
    assert "1,001 lines" in reason and "1,000-line limit" in reason


def test_lockfile_only_diff_passes(settings) -> None:
    assert evaluate(_file("uv.lock", added=5000), settings) is None


def test_zero_disables_line_check(settings) -> None:
    settings.evaluator = '{"size": {"max_changed_lines": 0}}'
    assert evaluate(_file("a.py", added=50_000), settings) is None


def test_empty_exclude_counts_lockfiles(settings) -> None:
    settings.evaluator = '{"size": {"exclude": []}}'
    assert evaluate(_file("uv.lock", added=5000), settings) is not None


def test_byte_check_runs_first(settings) -> None:
    settings.max_diff_bytes = 100
    reason = evaluate(_file("a.py", added=1001), settings)
    assert reason is not None and "bytes" in reason


# --- question_count_for ---------------------------------------------------


def _diff_with_changed_lines(n: int) -> str:
    return _file("x.py", added=n)


def test_a_one_line_change_gets_only_the_high_level_question(settings) -> None:
    assert question_count_for(_diff_with_changed_lines(1), settings) == 1


def test_questions_grow_with_the_size_of_the_change(settings) -> None:
    assert question_count_for(_diff_with_changed_lines(15), settings) == 2
    assert question_count_for(_diff_with_changed_lines(45), settings) == 4


def test_question_count_never_exceeds_exam_question_count(settings) -> None:
    assert question_count_for(_diff_with_changed_lines(900), settings) == settings.exam_question_count
