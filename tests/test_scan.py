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


# ---------- 内容ハッシュによる移動の結び付け（F-ID-1・F-ID-2） ----------


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """走査の日時を1回ごとに1分ずつ進める（同じ秒の走査どうしでは、前後関係を判定できないため）。"""
    import datetime as dt

    import tag_keeper.scan as scan_mod

    base = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
    ticks = [0]

    def fake_now() -> str:
        ticks[0] += 1
        return (base + dt.timedelta(minutes=ticks[0])).isoformat(timespec="seconds")

    monkeypatch.setattr(scan_mod, "utcnow", fake_now)
    return ticks


def refresh(conn: sqlite3.Connection, root_id: int, root: Path):
    """走査とハッシュ計算を続けて行い（画面の「最新にする」と同じ）、内容ハッシュで移動を結び付ける。"""
    from tag_keeper.hashing import hash_root
    from tag_keeper.scan import relink_by_hash

    scan_root(conn, root_id, root, SAFETY)
    hash_root(conn, root_id, root)
    return relink_by_hash(conn, root_id)


def copy_then_remove(src: Path, dst: Path) -> None:
    """inode の変わる移動（同期が「削除と新規」として届けたもの）を再現する。

    先に複製を作ってから元を消すので、消した inode が新しいファイルに使い回されることはない。
    """
    import shutil

    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
        shutil.rmtree(src)
    else:
        shutil.copy2(src, dst)
        src.unlink()


def test_move_with_a_new_inode_is_linked_by_hash(
    conn: sqlite3.Connection, root_id: int, root: Path, clock: list[int]
) -> None:
    write(root / "a.pdf", "請求書の中身")
    write(root / "keep.txt", "keep")
    refresh(conn, root_id, root)
    before = conn.execute("SELECT id, first_seen FROM entries WHERE relpath = 'a.pdf'").fetchone()

    copy_then_remove(root / "a.pdf", root / "docs" / "a-renamed.pdf")
    res = refresh(conn, root_id, root)

    assert (res.files, res.ambiguous, res.moves) == (1, 0, [("a.pdf", "docs/a-renamed.pdf")])
    # 同じ行が新しいパスで生き返り、初めて見た日時などを引き継ぐ。新しく足された行は残らない
    rows = conn.execute("SELECT id, relpath, first_seen, gone_at FROM entries WHERE is_dir = 0 ORDER BY relpath").fetchall()
    assert [(r["relpath"], r["gone_at"]) for r in rows] == [("docs/a-renamed.pdf", None), ("keep.txt", None)]
    assert (rows[0]["id"], rows[0]["first_seen"]) == (before["id"], before["first_seen"])
    move = conn.execute("SELECT old_relpath, new_relpath, source FROM moves").fetchone()
    assert tuple(move) == ("a.pdf", "docs/a-renamed.pdf", "hash")
    # もう一度結び付けても、何も起きない
    assert refresh(conn, root_id, root).files == 0


def test_folder_moved_with_new_inodes_is_recorded_as_one_folder_move(
    conn: sqlite3.Connection, root_id: int, root: Path, clock: list[int], tmp_path: Path
) -> None:
    """配下のファイルがそろって動いたら、フォルダ1件の移動として記録し、フォルダのタグも追従する。"""
    from tag_keeper.tags import TagStore

    write(root / "案件A" / "見積.pdf", "1")
    write(root / "案件A" / "sub" / "図.png", "2")
    write(root / "keep.txt", "keep")
    refresh(conn, root_id, root)
    store = TagStore(tmp_path / "tags", host="t")
    store.add("test", ["案件A"], "会社:アティード")

    copy_then_remove(root / "案件A", root / "完了" / "案件A-2026")
    res = refresh(conn, root_id, root)
    store.follow_moves("test", res.moves)

    assert res.files == 2
    assert res.moves == [("案件A", "完了/案件A-2026")]
    assert store.tags_of("test", "完了/案件A-2026/sub/図.png")["inherited"] == [
        {"tag": "会社:アティード", "from": "完了/案件A-2026"}
    ]
    sources = conn.execute("SELECT old_relpath, new_relpath, is_dir, source FROM moves").fetchall()
    assert [tuple(r) for r in sources] == [("案件A", "完了/案件A-2026", 1, "hash")]


def test_partial_moves_out_of_a_folder_stay_file_moves(
    conn: sqlite3.Connection, root_id: int, root: Path, clock: list[int]
) -> None:
    """元のフォルダが残っていれば、フォルダの移動とはみなさない。"""
    write(root / "src" / "a.pdf", "1")
    write(root / "src" / "b.pdf", "2")
    refresh(conn, root_id, root)
    copy_then_remove(root / "src" / "a.pdf", root / "dst" / "a.pdf")
    res = refresh(conn, root_id, root)
    assert res.moves == [("src/a.pdf", "dst/a.pdf")]


def test_identical_candidates_are_not_linked(
    conn: sqlite3.Connection, root_id: int, root: Path, clock: list[int]
) -> None:
    """同じ内容のものが2か所から消えて1つ現れたら、どちらが動いたか決められないので結び付けない。"""
    write(root / "x" / "f.pdf", "same")
    write(root / "y" / "f.pdf", "same")
    write(root / "keep.txt", "keep")
    refresh(conn, root_id, root)
    write(root / "z" / "f.pdf", "same")
    (root / "x" / "f.pdf").unlink()
    (root / "y" / "f.pdf").unlink()
    res = refresh(conn, root_id, root)
    assert (res.files, res.ambiguous, res.moves) == (0, 1, [])


def test_copy_that_existed_before_is_not_a_move(
    conn: sqlite3.Connection, root_id: int, root: Path, clock: list[int]
) -> None:
    """元と同時に見えていた複製は、元が消えても移動先とはみなさない（並んでいたことがあるため）。"""
    write(root / "a.pdf", "same")
    write(root / "copy.pdf", "same")
    refresh(conn, root_id, root)
    (root / "a.pdf").unlink()
    res = refresh(conn, root_id, root)
    assert (res.files, res.moves) == (0, [])


def test_quarantined_and_old_disappearances_are_not_linked(
    conn: sqlite3.Connection, root_id: int, root: Path, clock: list[int]
) -> None:
    """ツールが隔離したもの（取り消しで戻す）と、期間より前に消えたものは相手にしない。"""
    write(root / "q.pdf", "quarantined")
    write(root / "old.pdf", "old")
    write(root / "keep.txt", "keep")
    refresh(conn, root_id, root)
    (root / "q.pdf").unlink()
    (root / "old.pdf").unlink()
    scan_root(conn, root_id, root, SAFETY)
    conn.execute("UPDATE entries SET gone_by = 'plan' WHERE relpath = 'q.pdf'")
    conn.execute("UPDATE entries SET gone_at = '2000-01-01T00:00:00+00:00' WHERE relpath = 'old.pdf'")
    conn.commit()
    write(root / "back" / "q.pdf", "quarantined")
    write(root / "back" / "old.pdf", "old")
    res = refresh(conn, root_id, root)
    assert (res.files, res.moves) == (0, [])


def test_relink_can_be_turned_off(conn: sqlite3.Connection, root_id: int, root: Path, clock: list[int]) -> None:
    from tag_keeper.hashing import hash_root
    from tag_keeper.scan import relink_by_hash

    write(root / "a.pdf", "1")
    write(root / "keep.txt", "keep")
    refresh(conn, root_id, root)
    copy_then_remove(root / "a.pdf", root / "b.pdf")
    scan_root(conn, root_id, root, SAFETY)
    hash_root(conn, root_id, root)
    assert relink_by_hash(conn, root_id, days=0).files == 0


def test_mass_move_held_by_the_safety_valve_is_linked(
    conn: sqlite3.Connection, root_id: int, root: Path, clock: list[int]
) -> None:
    """大量の移動で消失の確定が保留になっても（F-SC-7）、内容ハッシュで移動と分かれば保留が解ける。"""
    for i in range(20):
        write(root / "big" / f"f{i}.txt", f"content {i}")
    write(root / "keep.txt", "keep")
    refresh(conn, root_id, root)

    copy_then_remove(root / "big", root / "moved" / "big")
    scan = scan_root(conn, root_id, root, SAFETY)
    assert scan.held == 20
    from tag_keeper.hashing import hash_root
    from tag_keeper.scan import relink_by_hash

    hash_root(conn, root_id, root)
    res = relink_by_hash(conn, root_id)
    assert (res.files, res.moves) == (20, [("big", "moved/big")])
    assert live_paths(conn, root_id) == {f"moved/big/f{i}.txt" for i in range(20)} | {"keep.txt"}
    assert conn.execute("SELECT MAX(missing_count) FROM entries WHERE is_dir = 0").fetchone()[0] == 0
    # 次の走査で、消えたものは出ない
    again = scan_root(conn, root_id, root, SAFETY)
    assert (again.gone, again.held) == (0, 0)


def test_names_that_are_not_utf8_are_skipped_without_failing_the_scan(
    conn: sqlite3.Connection, root_id: int, root: Path
) -> None:
    """UTF-8 として読めない名前（CP932 の名前のまま展開した zip の中身など）があっても、走査全体は止めない。"""
    write(root / "ok.txt", "ok")
    bad = os.fsencode(root) + b"/" + "請求書.pdf".encode("cp932")
    with open(bad, "wb") as f:
        f.write(b"x")
    res = scan_root(conn, root_id, root, SAFETY)
    assert res.added == 1 and live_paths(conn, root_id) == {"ok.txt"}


def test_identical_files_inside_a_moved_folder_are_linked_by_their_place(
    conn: sqlite3.Connection, root_id: int, root: Path, clock: list[int]
) -> None:
    """動いたフォルダの中に同じ内容の写しが複数あっても、フォルダの移動が分かれば、同じ相対パスどうしを結び付ける。"""
    write(root / "Windows" / "chrome" / "ext" / "LICENSE", "MIT")
    write(root / "Windows" / "opt" / "ext" / "LICENSE", "MIT")  # 同じ内容の写し
    write(root / "Windows" / "unique.txt", "only one")
    write(root / "keep.txt", "keep")
    refresh(conn, root_id, root)
    before = {r["relpath"]: r["id"] for r in conn.execute("SELECT id, relpath FROM entries WHERE is_dir = 0")}

    copy_then_remove(root / "Windows", root / "90_System" / "Windows")
    res = refresh(conn, root_id, root)

    assert (res.files, res.ambiguous, res.moves) == (3, 0, [("Windows", "90_System/Windows")])
    after = {r["relpath"]: r["id"] for r in conn.execute("SELECT id, relpath FROM entries WHERE is_dir = 0 AND gone_at IS NULL")}
    assert after["90_System/Windows/chrome/ext/LICENSE"] == before["Windows/chrome/ext/LICENSE"]
    assert after["90_System/Windows/opt/ext/LICENSE"] == before["Windows/opt/ext/LICENSE"]
