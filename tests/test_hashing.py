"""内容ハッシュの計算のテスト。"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from conftest import write
from tag_keeper.config import SafetyConfig
from tag_keeper.hashing import hash_root, pending
from tag_keeper.scan import scan_root


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_hashes_are_computed_and_saved(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    write(root / "a.txt", b"hello")
    write(root / "sub" / "b.bin", b"world")
    scan_root(conn, root_id, root, SafetyConfig())
    res = hash_root(conn, root_id, root)
    assert (res.hashed, res.hashed_bytes) == (2, 10)
    got = {r["relpath"]: r["sha256"] for r in conn.execute("SELECT relpath, sha256 FROM entries WHERE is_dir = 0")}
    assert got == {"a.txt": sha(b"hello"), "sub/b.bin": sha(b"world")}


def test_second_run_hashes_nothing(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    write(root / "a.txt", b"hello")
    scan_root(conn, root_id, root, SafetyConfig())
    hash_root(conn, root_id, root)
    assert pending(conn, root_id) == []
    assert hash_root(conn, root_id, root).hashed == 0


def test_changed_file_is_rehashed_after_rescan(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    f = write(root / "a.txt", b"one")
    scan_root(conn, root_id, root, SafetyConfig())
    hash_root(conn, root_id, root)
    f.write_bytes(b"two!")
    scan_root(conn, root_id, root, SafetyConfig())
    assert hash_root(conn, root_id, root).hashed == 1
    row = conn.execute("SELECT sha256 FROM entries WHERE relpath = 'a.txt'").fetchone()
    assert row["sha256"] == sha(b"two!")


def test_file_changed_after_scan_is_skipped(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    """走査の後に変わったファイルは計算せず、次の走査を待つ（古い属性のハッシュを保存しない）。"""
    f = write(root / "a.txt", b"one")
    scan_root(conn, root_id, root, SafetyConfig())
    f.write_bytes(b"changed")
    res = hash_root(conn, root_id, root)
    assert (res.hashed, res.skipped_changed) == (0, 1)


def test_moved_file_keeps_its_hash(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    write(root / "a.txt", b"payload")
    scan_root(conn, root_id, root, SafetyConfig())
    hash_root(conn, root_id, root)
    (root / "a.txt").rename(root / "renamed.txt")
    scan_root(conn, root_id, root, SafetyConfig())
    assert hash_root(conn, root_id, root).hashed == 0
