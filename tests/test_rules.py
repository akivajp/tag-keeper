"""名前とサイズによる分類ルールのテスト。"""

from __future__ import annotations

import pytest

from tag_keeper import rules
from tag_keeper.rules import classify_file


@pytest.mark.parametrize(
    ("relpath", "size", "category"),
    [
        ("docs/~$report.docx", 162, rules.CAT_OFFICE_LOCK),
        ("docs/.~lock.report.odt#", 80, rules.CAT_OFFICE_LOCK),
        ("photos/Thumbs.db", 1000, rules.CAT_OS_JUNK),
        ("mac/.DS_Store", 6000, rules.CAT_OS_JUNK),
        ("vm/win10.vdi", 10**9, rules.CAT_VM_IMAGE),
        ("images/Win10_22H2.iso", 5 * 10**9, rules.CAT_INSTALLER),
        ("Downloads/tool-1.2.exe", 10**6, rules.CAT_INSTALLER),
        ("apps/foo/setup.exe", 10**6, rules.CAT_INSTALLER),
        ("dl/movie.mp4.crdownload", 10**6, rules.CAT_TEMP),
        ("note-zefat-safeBackup-0001.one", 500, rules.CAT_CONFLICT),
        ("assets/abc [conflicted].unity3d", 5000, rules.CAT_CONFLICT),
        ("a/file.txt.conflict1", 10, rules.CAT_CONFLICT),
        ("video_2024-12-27_conflict (1).mp4", 10**9, rules.CAT_CONFLICT),
        ("tax/form (1).pdf", 1000, rules.CAT_CONFLICT),
        ("game/FFXIV - コピー.cfg", 4000, rules.CAT_CONFLICT),
        ("empty.txt", 0, rules.CAT_EMPTY_FILE),
    ],
)
def test_classify_file(relpath: str, size: int, category: str) -> None:
    match = classify_file(relpath, size)
    assert match is not None and match.category == category


@pytest.mark.parametrize(
    "relpath",
    [
        "Windows/opt/ACT/Advanced Combat Tracker.exe",  # ポータブルアプリの本体
        "docs/report.pdf",
        "notes/2026-10-05.md",
        "code/build.gradle",
    ],
)
def test_ordinary_files_are_not_flagged(relpath: str) -> None:
    assert classify_file(relpath, 1234) is None


def test_device_name_suffix_is_a_conflict_copy() -> None:
    match = classify_file("money/plan-hermon.xlsx", 10_000, device_names=["zefat", "hermon"])
    assert match is not None and match.category == rules.CAT_CONFLICT
    assert classify_file("money/plan-hermon.xlsx", 10_000, device_names=["zefat"]) is None


@pytest.mark.parametrize(
    ("name", "original"),
    [
        ("report (1).pdf", "report.pdf"),
        ("plan-zefat.xlsx", "plan.xlsx"),
        ("note-safeBackup-0001.one", "note.one"),
        ("note-zefat-safeBackup-0001.one", "note.one"),
        ("abc [conflicted].unity3d", "abc.unity3d"),
        ("file.txt.conflict1", "file.txt"),
        ("FFXIV - コピー.cfg", "FFXIV.cfg"),
        ("zefat.txt", None),
    ],
)
def test_conflict_original(name: str, original: str | None) -> None:
    assert rules.conflict_original(name, ["zefat"]) == original
