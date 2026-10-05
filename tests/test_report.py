"""報告（フォルダ単位の候補・重複・報告しないパス）のテスト。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from conftest import write
from tag_keeper import rules
from tag_keeper.config import HygieneConfig, SafetyConfig
from tag_keeper.hashing import hash_root
from tag_keeper.report import build_report
from tag_keeper.scan import scan_root

HYGIENE = HygieneConfig(device_names=[], app_data_min_files=10)


def make_report(conn: sqlite3.Connection, root_id: int, root: Path, hygiene: HygieneConfig = HYGIENE, *, hashed: bool = False):
    scan_root(conn, root_id, root, SafetyConfig())
    if hashed:
        hash_root(conn, root_id, root)
    return build_report(conn, root_id, "test", hygiene)


def findings(report, category: str) -> dict[str, object]:
    return {f.relpath: f for f in report.findings if f.category == category}


def test_known_app_data_folder_is_reported_once_as_a_whole(conn, root_id, root) -> None:
    for i in range(30):
        write(root / "Documents" / "DMMGames" / "game" / "cache" / f"{i:04x}", "data")
    write(root / "Documents" / "DMMGames" / "game" / "~$x.docx")  # 配下はファイル単位で数えない
    write(root / "Documents" / "letter.docx", "important")
    report = make_report(conn, root_id, root)

    app = findings(report, rules.CAT_APP_DATA)
    assert list(app) == ["Documents/DMMGames"]
    assert app["Documents/DMMGames"].files == 31
    assert rules.CAT_OFFICE_LOCK not in {f.category for f in report.findings}


def test_noext_heuristic_reports_the_deepest_folder_not_the_parent(conn, root_id, root) -> None:
    """拡張子の無いファイルが多くても、利用者の書類を含む親フォルダ全体は候補にしない。"""
    for i in range(30):
        write(root / "Documents" / "SomeApp" / "Data" / f"{i:04x}", "blob")
    for i in range(5):
        write(root / "Documents" / f"letter{i}.docx", "doc")
    report = make_report(conn, root_id, root)
    assert list(findings(report, rules.CAT_APP_DATA)) == ["Documents/SomeApp/Data"]


def test_noext_heuristic_ignores_folders_already_flagged_by_name(conn, root_id, root) -> None:
    for i in range(40):
        write(root / "Documents" / "DMMGames" / f"{i:04x}", "blob")
    for i in range(12):
        write(root / "Documents" / "notes" / f"n{i}.md", "note")
    report = make_report(conn, root_id, root)
    # DMMGames を除くと Documents は拡張子付きのファイルばかりなので、候補にならない
    assert list(findings(report, rules.CAT_APP_DATA)) == ["Documents/DMMGames"]


def test_git_objects_do_not_count_as_app_data(conn, root_id, root) -> None:
    """Git の内部ファイルは拡張子が無いが、リポジトリをアプリのデータと誤認しない。"""
    for i in range(30):
        write(root / "repo" / ".git" / "objects" / f"{i:02x}" / f"{i:038x}", "obj")
    write(root / "repo" / "main.py")
    report = make_report(conn, root_id, root)
    assert findings(report, rules.CAT_APP_DATA) == {}


def test_cache_and_regenerable_folders(conn, root_id, root) -> None:
    write(root / "proj" / "node_modules" / "lib" / "index.js")
    write(root / "proj" / "src" / "__pycache__" / "m.cpython-312.pyc")
    write(root / "app" / "Temp" / "Code Cache" / "js" / "f1")
    write(root / "proj" / "src" / "main.py")
    report = make_report(conn, root_id, root)
    assert set(findings(report, rules.CAT_REGENERABLE)) == {"proj/node_modules", "proj/src/__pycache__"}
    assert set(findings(report, rules.CAT_APP_DATA)) == {"app/Temp/Code Cache"}


def test_empty_folders(conn, root_id, root) -> None:
    (root / "empty" / "nested").mkdir(parents=True)
    write(root / "full" / "a.txt")
    report = make_report(conn, root_id, root)
    assert set(findings(report, rules.CAT_EMPTY_DIR)) == {"empty/nested"}


def test_duplicates_require_hashes(conn, root_id, root) -> None:
    write(root / "a" / "video.mp4", b"same-bytes" * 100)
    write(root / "b" / "copy.mp4", b"same-bytes" * 100)
    write(root / "c" / "other.mp4", b"different" * 100)

    unhashed = make_report(conn, root_id, root)
    assert unhashed.duplicates == []

    hashed = make_report(conn, root_id, root, hashed=True)
    assert len(hashed.duplicates) == 1
    group = hashed.duplicates[0]
    assert group.paths == ["a/video.mp4", "b/copy.mp4"]
    assert group.waste == 1000


def test_allow_patterns_suppress_findings(conn, root_id, root) -> None:
    write(root / "archive" / "old" / "Thumbs.db")
    write(root / "archive" / "vm.vdi")
    write(root / "live" / "Thumbs.db")
    hygiene = HygieneConfig(device_names=[], allow=["archive/**"], app_data_min_files=10)
    report = make_report(conn, root_id, root, hygiene)
    assert {f.relpath for f in report.findings} == {"live/Thumbs.db"}


def test_fanout_counts_direct_files(conn, root_id, root) -> None:
    for i in range(5):
        write(root / "scans" / f"s{i}.pdf")
    write(root / "scans" / "sub" / "x.pdf")
    report = make_report(conn, root_id, root)
    assert report.fanout[0] == ("scans", 5)


def test_to_dict_includes_waste(conn, root_id, root) -> None:
    write(root / "a.bin", b"z" * 50)
    write(root / "b.bin", b"z" * 50)
    report = make_report(conn, root_id, root, hashed=True)
    assert report.to_dict()["duplicates"][0]["waste"] == 50


def test_conflict_copies_need_an_original(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    """元のファイルが無い「競合コピーらしい名前」は、唯一の版かもしれないので報告しない。"""
    write(root / "tax" / "form.pdf", "v1")
    write(root / "tax" / "form (1).pdf", "v1")
    write(root / "tax" / "plan.xlsx", "mine")
    write(root / "tax" / "plan-zefat.xlsx", "theirs")
    write(root / "bills" / "invoice-zefat.pdf", "only copy")
    scan_root(conn, root_id, root, SafetyConfig())
    hash_root(conn, root_id, root)
    report = build_report(conn, root_id, "test", HygieneConfig(device_names=["zefat"]))

    conflicts = {f.relpath: f.reason for f in report.findings if f.category == rules.CAT_CONFLICT}
    assert set(conflicts) == {"tax/form (1).pdf", "tax/plan-zefat.xlsx"}
    assert conflicts["tax/form (1).pdf"].endswith("元のファイルと内容が同じ")
    assert conflicts["tax/plan-zefat.xlsx"].endswith("中身の確認が必要")
    review = {f.relpath for f in report.findings if f.needs_review}
    assert review == {"tax/plan-zefat.xlsx"}
