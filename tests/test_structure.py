"""整理の支援の残り（検出の追加・ルールの無効化・残す・構造の見直し）のテスト。"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from conftest import write
from tag_keeper import rules, structure
from tag_keeper.config import HygieneConfig, SafetyConfig
from tag_keeper.hashing import hash_root
from tag_keeper.report import build_report
from tag_keeper.scan import scan_root

SAFETY = SafetyConfig(mass_missing_ratio=0.2, mass_missing_min=3)


def report_of(conn: sqlite3.Connection, root_id: int, **kw: object):
    hy = kw.pop("hygiene", None) or HygieneConfig(device_names=[], app_data_min_files=10_000)
    return build_report(conn, root_id, "test", hy, **kw)  # type: ignore[arg-type]


def cats(report) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in report.findings:
        out.setdefault(f.category, []).append(f.relpath)
    return out


def test_new_findings_extracted_archives_old_versions_and_stale_large(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    write(root / "dl" / "photos.zip", "zip")
    write(root / "dl" / "photos" / "a.jpg", "a")
    write(root / "dl" / "lonely.zip", "zip")  # 展開したフォルダが無い
    write(root / "docs" / "plan-v1.docx", "v1")
    write(root / "docs" / "plan-v2.docx", "v2")
    big = write(root / "media" / "old.mkv", b"\0" * 2048)
    os.utime(big, (1_500_000_000, 1_500_000_000))  # 2017 年
    scan_root(conn, root_id, root, SAFETY)
    hy = HygieneConfig(device_names=[], app_data_min_files=10_000, stale_min_mb=0, stale_days=365)
    r = report_of(conn, root_id, hygiene=hy, old_versions={"docs/plan-v1.docx": "docs/plan-v2.docx", "gone.docx": "docs/plan-v2.docx"})
    c = cats(r)
    assert c[rules.CAT_EXTRACTED] == ["dl/photos.zip"]
    assert c[rules.CAT_OLD_VERSION] == ["docs/plan-v1.docx"]
    assert "media/old.mkv" in c[rules.CAT_STALE_LARGE]
    by_path = {f.relpath: f for f in r.findings}
    assert by_path["dl/photos.zip"].needs_review and by_path["media/old.mkv"].needs_review
    assert not by_path["docs/plan-v1.docx"].needs_review


def test_disabled_rules_and_kept_items_are_not_reported(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    write(root / "a.tmp", "t")
    write(root / "b" / "Thumbs.db", "j")
    write(root / "keep" / "x.tmp", "k")
    scan_root(conn, root_id, root, SAFETY)
    assert set(cats(report_of(conn, root_id))) >= {rules.CAT_TEMP, rules.CAT_OS_JUNK}
    hy = HygieneConfig(device_names=[], app_data_min_files=10_000, disabled=["temp"])
    assert rules.CAT_TEMP not in cats(report_of(conn, root_id, hygiene=hy))
    r = report_of(conn, root_id, kept=["keep", "b/Thumbs.db"])
    assert cats(r) == {rules.CAT_TEMP: ["a.tmp"]}


def test_structure_review_finds_each_kind(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    for i in range(6):
        write(root / "flat" / f"f{i}.txt", str(i))
    write(root / "経理" / "請求書" / "a.pdf", "a")
    write(root / "経理" / "請求書類" / "b.pdf", "b")
    for name in ("01_仕事", "02_家", "02_趣味", "05_旅行"):
        write(root / "areas" / name / "x.txt", name)
    for name in ("2025-01", "202502", "2025_03"):
        write(root / "months" / name / "y.txt", name)
    write(root / "deep" / "one" / "two" / "z.txt", "z")
    for y in ("2023年_週報", "2024年_週報"):  # 年で分けたシリーズは似た名前とみなさない
        write(root / "reports" / y / "r.txt", y)
    write(root / "copyA" / "sub" / "s.txt", "same3")  # 親ごと同じなので、sub の組は出さない
    write(root / "copyB" / "sub" / "s.txt", "same3")
    write(root / "copyA" / "p.txt", "same1")
    write(root / "copyA" / "q.txt", "same2")
    write(root / "copyB" / "p.txt", "same1")
    write(root / "copyB" / "q.txt", "same2")
    write(root / "archive" / "x" / "y" / "z" / "w.txt", "w")  # 保管の型の中は見ない
    scan_root(conn, root_id, root, SAFETY)
    hash_root(conn, root_id, root)
    issues = structure.review(conn, root_id, types={"archive": "archive"}, fanout_max=5)
    by_kind: dict[str, list[structure.Issue]] = {}
    for i in issues:
        by_kind.setdefault(i.kind, []).append(i)
    assert [i.path for i in by_kind["fanout"]] == ["flat"]
    assert by_kind["similar"][0].paths == ["経理/請求書", "経理/請求書類"]
    numbering = by_kind["numbering"][0]
    assert numbering.path == "areas" and "重複: 2" in numbering.detail and "欠番: 3、4" in numbering.detail
    assert by_kind["dates"][0].path == "months"
    assert by_kind["chain"][0].path == "deep"
    assert [i.paths for i in by_kind["duplicate"]] == [["copyA", "copyB"]]
    assert all(i.path != "reports" for i in by_kind["similar"])
    assert not any(i.path.startswith("archive") for i in issues)
    # 残すと出なくなる
    again = structure.review(conn, root_id, types={"archive": "archive"}, fanout_max=5, kept=["flat", "deep"])
    assert not any(i.kind in ("fanout", "chain") for i in again)


def test_mixed_folder(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    for i in range(12):
        write(root / "misc" / f"d{i}.pdf", f"d{i}")
        write(root / "misc" / f"i{i}.jpg", f"i{i}")
        write(root / "misc" / f"v{i}.mp4", f"v{i}")
    scan_root(conn, root_id, root, SAFETY)
    issues = structure.review(conn, root_id)
    assert [i.path for i in issues if i.kind == "mixed"] == ["misc"]
