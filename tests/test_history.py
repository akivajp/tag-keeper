"""移動の記録（走査・プラン）と、移動をまたいだ版の履歴のたどり方のテスト。

スナップショットそのものは btrfs が要るので、ここでは移動の記録とその当てはめ方だけを確かめる。
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from conftest import write
from tag_keeper.catalog import record_move
from tag_keeper.config import SafetyConfig
from tag_keeper.history import Segment, _in, lineage
from tag_keeper.scan import scan_root

SAFETY = SafetyConfig(mass_missing_ratio=0.9, mass_missing_min=100)


def moves(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return [(r["old_relpath"], r["new_relpath"], r["source"]) for r in conn.execute("SELECT * FROM moves ORDER BY id")]


def test_scan_records_only_the_top_of_a_moved_folder(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    write(root / "inbox" / "proj" / "a.pdf")
    write(root / "inbox" / "proj" / "sub" / "b.pdf")
    write(root / "inbox" / "c.pdf")
    scan_root(conn, root_id, root, SAFETY)
    (root / "done").mkdir()
    (root / "inbox" / "proj").rename(root / "done" / "proj")
    (root / "inbox" / "c.pdf").rename(root / "inbox" / "renamed.pdf")
    res = scan_root(conn, root_id, root, SAFETY)
    assert sorted(res.moves) == [("inbox/c.pdf", "inbox/renamed.pdf"), ("inbox/proj", "done/proj")]
    assert sorted(moves(conn)) == [("inbox/c.pdf", "inbox/renamed.pdf", "scan"), ("inbox/proj", "done/proj", "scan")]


def test_lineage_follows_folder_moves_and_renames(conn: sqlite3.Connection, root_id: int) -> None:
    t1 = "2026-01-01T00:00:00+00:00"
    t2 = "2026-02-01T00:00:00+00:00"
    record_move(conn, root_id, 1, "inbox/scan.pdf", "inbox/20260101_請求書.pdf", False, "plan", t1)
    record_move(conn, root_id, 2, "inbox", "archive/inbox", True, "scan", t2)
    conn.commit()
    segs = lineage(conn, root_id, "archive/inbox/20260101_請求書.pdf")
    assert [s.relpath for s in segs] == [
        "archive/inbox/20260101_請求書.pdf",
        "inbox/20260101_請求書.pdf",
        "inbox/scan.pdf",
    ]
    # 期間は新しい順につながっている
    assert segs[0].until is None and segs[0].since == segs[1].until
    assert segs[2].since is None


def test_snapshot_belongs_to_the_right_segment() -> None:
    t = dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc)
    old = Segment("old.pdf", None, t)
    new = Segment("new.pdf", t, None)
    before = t - dt.timedelta(hours=1)
    after = t + dt.timedelta(hours=1)
    assert _in(old, before) and not _in(old, after)
    assert _in(new, after) and not _in(new, before)
    assert _in(new, None) and not _in(old, None)
