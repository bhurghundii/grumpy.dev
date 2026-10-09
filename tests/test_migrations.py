"""The migration runner orders by numeric version, not raw filename."""

from __future__ import annotations

from pathlib import Path

from app.db.migrations import _version_key


def test_v10_sorts_after_v9_not_after_v1() -> None:
    names = ["V10__x.sql", "V2__x.sql", "V1__x.sql", "V9__x.sql"]
    ordered = [p.name for p in sorted((Path(n) for n in names), key=_version_key)]
    assert ordered == ["V1__x.sql", "V2__x.sql", "V9__x.sql", "V10__x.sql"]


def test_non_conforming_names_sort_first_and_stably() -> None:
    names = ["V1__a.sql", "zzz.sql", "aaa.sql"]
    ordered = [p.name for p in sorted((Path(n) for n in names), key=_version_key)]
    # Unversioned files collate before V1 (key -1), among themselves by name.
    assert ordered == ["aaa.sql", "zzz.sql", "V1__a.sql"]
