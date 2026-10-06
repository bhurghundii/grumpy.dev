"""Checks that reject a PR outright at POST /sessions, before grumpy asks
its question.

Some PRs aren't worth questioning: nobody can explain a 15k-line change,
so asking only produces a confident-sounding answer to grade. A rejection
is a 422 carrying the reason (see app/main.py), which the workflow turns
into a red job and a PR comment. No session is created, so splitting the
PR and pushing again is a fresh start.

Configured by the EVALUATOR env var (app.config.EvaluatorConfig). Each
check is one function in _CHECKS returning a reason or None; the first
reason wins. A new check is a new function here plus a new key there.
"""

from __future__ import annotations

from collections.abc import Callable
from fnmatch import fnmatch
from pathlib import PurePosixPath

from app.config import Settings

_GIT_HEADER = "diff --git "


def _is_excluded(path: str | None, exclude: list[str]) -> bool:
    # Basename too, so "package-lock.json" matches web/package-lock.json
    # without the self-hoster having to write "*/package-lock.json".
    if path is None:
        return False
    name = PurePosixPath(path).name
    return any(fnmatch(path, pattern) or fnmatch(name, pattern) for pattern in exclude)


def count_changed_lines(diff: str, exclude: list[str]) -> int:
    """Added + removed lines across the diff, skipping files that match an
    `exclude` glob. Only lines inside a hunk (after a file's first `@@`)
    count, so the `---`/`+++` file headers never do, while an added line
    that happens to start with `++` still does. Rename-only and binary
    files have no hunks and count 0."""
    total = 0
    path: str | None = None
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith(_GIT_HEADER):
            # "diff --git a/X b/X": the b/ side is the path after the change.
            _, sep, after = line.rpartition(" b/")
            path = after if sep else None
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line[:1] in ("+", "-") and not _is_excluded(path, exclude):
            total += 1
    return total


def _check_diff_bytes(diff: str, settings: Settings) -> str | None:
    size = len(diff.encode("utf-8"))
    if size <= settings.max_diff_bytes:
        return None
    return (
        f"This diff is {size:,} bytes, over the {settings.max_diff_bytes:,}-byte limit. "
        "Split it into smaller PRs."
    )


def _check_changed_lines(diff: str, settings: Settings) -> str | None:
    size = settings.evaluator_config().size
    if size.max_changed_lines == 0:
        return None
    changed = count_changed_lines(diff, size.exclude)
    if changed <= size.max_changed_lines:
        return None
    excluded = f", not counting {', '.join(size.exclude)}" if size.exclude else ""
    return (
        f"This PR changes {changed:,} lines, over the {size.max_changed_lines:,}-line "
        f"limit{excluded}. Split it into smaller PRs."
    )


# Byte size first: it's the cheap check, and a diff past it is one the
# grader could never read anyway, whatever its line count.
_CHECKS: tuple[Callable[[str, Settings], str | None], ...] = (
    _check_diff_bytes,
    _check_changed_lines,
)


def evaluate(diff: str, settings: Settings) -> str | None:
    """The reason the first failing check rejects `diff`, or None if it
    passes them all."""
    for check in _CHECKS:
        reason = check(diff, settings)
        if reason is not None:
            return reason
    return None
