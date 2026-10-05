"""走査（差分更新・移動の追跡・大量消失の安全弁）のテスト。"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from conftest import live_paths, write
from tag_keeper.config import SafetyConfig
from tag_keeper.scan import RootUnavailableError, is_excluded, scan_root

SAFETY = SafetyConfig(mass_missing_ratio=0.2, mass_missing_min=3)


def test_first_scan_records_files_and_dirs(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    write(root / "a.txt")
    write(root / "sub" / "b.txt")
    res = scan_root(conn, root_id, root, SAFETY)
    assert (res.added, res.changed, res.moved, res.gone) == (2, 0, 0, 0)
    assert live_paths(conn, root_id) == {"a.txt", "sub/b.txt"}
    assert live_paths(conn, root_id, files_only=False) == {"a.txt", "sub", "sub/b.txt"}


def test_rescan_without_changes_is_quiet(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    write(root / "a.txt")
    scan_root(conn, root_id, root, SAFETY)
    res = scan_root(conn, root_id, root, SAFETY)
    assert (res.seen, res.added, res.changed, res.moved, res.gone) == (1, 0, 0, 0, 0)


def test_modified_file_is_counted_as_changed(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    f = write(root / "a.txt", "old")
    scan_root(conn, root_id, root, SAFETY)
    f.write_text("new content")
    res = scan_root(conn, root_id, root, SAFETY)
    assert res.changed == 1
    row = conn.execute("SELECT size FROM entries WHERE relpath = 'a.txt'").fetchone()
    assert row["size"] == len("new content")


def test_rename_keeps_the_same_row(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    """改名は inode で追跡し、行（=ハッシュなどの管理情報）を引き継ぐ。"""
    write(root / "a.txt")
    scan_root(conn, root_id, root, SAFETY)
    before = conn.execute("SELECT id FROM entries WHERE relpath = 'a.txt'").fetchone()["id"]
    conn.execute("UPDATE entries SET sha256 = 'abc' WHERE id = ?", (before,))
    conn.commit()

    (root / "a.txt").rename(root / "b.txt")
    res = scan_root(conn, root_id, root, SAFETY)

    assert (res.moved, res.added, res.gone) == (1, 0, 0)
    row = conn.execute("SELECT id, sha256 FROM entries WHERE relpath = 'b.txt'").fetchone()
    assert (row["id"], row["sha256"]) == (before, "abc")


def test_directory_rename_moves_every_descendant(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    write(root / "old" / "x.txt")
    write(root / "old" / "deep" / "y.txt")
    scan_root(conn, root_id, root, SAFETY)

    (root / "old").rename(root / "new")
    res = scan_root(conn, root_id, root, SAFETY)

    assert (res.moved, res.added, res.gone) == (2, 0, 0)
    assert live_paths(conn, root_id) == {"new/x.txt", "new/deep/y.txt"}


def test_few_deletions_are_committed(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    for i in range(20):
        write(root / f"f{i}.txt")
    scan_root(conn, root_id, root, SAFETY)
    (root / "f0.txt").unlink()
    res = scan_root(conn, root_id, root, SAFETY)
    assert (res.gone, res.held, res.status) == (1, 0, "committed")
    assert "f0.txt" not in live_paths(conn, root_id)


def test_mass_disappearance_is_held_until_seen_missing_twice(
    conn: sqlite3.Connection, root_id: int, root: Path
) -> None:
    """一度に大量に消えたら確定を保留し、2回続けて見えなければ確定する（F-SC-7）。"""
    for i in range(20):
        write(root / "big" / f"f{i}.txt")
    write(root / "keep.txt")
    scan_root(conn, root_id, root, SAFETY)

    for p in (root / "big").iterdir():
        p.unlink()
    first = scan_root(conn, root_id, root, SAFETY)
    assert (first.gone, first.held, first.status) == (0, 20, "held")
    assert len(live_paths(conn, root_id)) == 21  # まだ消えたと確定しない

    second = scan_root(conn, root_id, root, SAFETY)
    assert (second.gone, second.held, second.status) == (20, 0, "committed")
    assert live_paths(conn, root_id) == {"keep.txt"}


def test_held_files_that_come_back_are_restored(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    """保留中に戻ってきたら（マウントが戻った等）、何も失わない。"""
    files = [write(root / "big" / f"f{i}.txt") for i in range(20)]
    write(root / "keep.txt")
    scan_root(conn, root_id, root, SAFETY)
    contents = {p: p.read_bytes() for p in files}
    for p in files:
        p.unlink()
    assert scan_root(conn, root_id, root, SAFETY).held == 20
    for p, data in contents.items():
        p.write_bytes(data)
    res = scan_root(conn, root_id, root, SAFETY)
    assert (res.gone, res.held) == (0, 0)
    assert len(live_paths(conn, root_id)) == 21
    assert conn.execute("SELECT MAX(missing_count) FROM entries").fetchone()[0] == 0


def test_accept_missing_commits_mass_disappearance(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    for i in range(20):
        write(root / "big" / f"f{i}.txt")
    write(root / "keep.txt")
    scan_root(conn, root_id, root, SAFETY)
    for p in (root / "big").iterdir():
        p.unlink()
    res = scan_root(conn, root_id, root, SAFETY, accept_missing=True)
    assert (res.gone, res.held) == (20, 0)


def test_missing_root_aborts_without_touching_catalog(
    conn: sqlite3.Connection, root_id: int, root: Path
) -> None:
    write(root / "a.txt")
    scan_root(conn, root_id, root, SAFETY)
    (root / "a.txt").unlink()
    root.rmdir()
    with pytest.raises(RootUnavailableError):
        scan_root(conn, root_id, root, SAFETY)
    assert live_paths(conn, root_id) == {"a.txt"}


def test_empty_root_is_treated_as_unmounted(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    with pytest.raises(RootUnavailableError):
        scan_root(conn, root_id, root, SAFETY)


@pytest.mark.skipif(os.geteuid() == 0, reason="root は権限に関係なく読めてしまう")
def test_unreadable_directory_is_not_reported_as_gone(
    conn: sqlite3.Connection, root_id: int, root: Path
) -> None:
    write(root / "locked" / "secret.txt")
    write(root / "open.txt")
    scan_root(conn, root_id, root, SAFETY)
    (root / "locked").chmod(0)
    try:
        res = scan_root(conn, root_id, root, SAFETY)
    finally:
        (root / "locked").chmod(0o755)
    assert (res.gone, res.unreadable) == (0, 1)
    assert "locked/secret.txt" in live_paths(conn, root_id)


def test_symlinks_are_not_followed(conn: sqlite3.Connection, root_id: int, root: Path, tmp_path: Path) -> None:
    write(tmp_path / "outside" / "o.txt")
    write(root / "a.txt")
    (root / "link").symlink_to(tmp_path / "outside")
    scan_root(conn, root_id, root, SAFETY)
    assert live_paths(conn, root_id, files_only=False) == {"a.txt"}


def test_exclude_patterns(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    write(root / "a.txt")
    write(root / "dl" / "x.partial")
    write(root / "cache" / "c.bin")
    scan_root(conn, root_id, root, SAFETY, exclude=["*.partial", "cache"])
    assert live_paths(conn, root_id) == {"a.txt"}


@pytest.mark.parametrize(
    ("relpath", "pattern", "expected"),
    [
        ("archive/2015/a.pdf", "archive/**", True),
        ("x/node_modules", "node_modules", True),  # / を含まないパターンは名前にも照合
        ("x/node_modules/a.js", "x/node", False),
        ("a/b.tmp", "*.tmp", True),
    ],
)
def test_is_excluded(relpath: str, pattern: str, expected: bool) -> None:
    assert is_excluded(relpath, relpath.rsplit("/", 1)[-1], [pattern]) is expected
