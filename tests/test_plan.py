"""整理プラン（作成・実行・取り消し）のテスト。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from conftest import live_paths, write
from tag_keeper import rules
from tag_keeper.cli import main
from tag_keeper.config import HygieneConfig, SafetyConfig
from tag_keeper.plan import (
    Plan,
    PlanError,
    SnapshotError,
    apply_plan,
    build_plan,
    journal_path,
    load_plan,
    undo_plan,
    write_plan,
)
from tag_keeper.hashing import hash_root
from tag_keeper.report import build_report
from tag_keeper.scan import scan_root

SAFETY = SafetyConfig(mass_missing_ratio=0.2, mass_missing_min=3)
HYGIENE = HygieneConfig(device_names=[], app_data_min_files=10_000)


class FakeSnapshots:
    """snapper の代わりに、撮影の呼び出しを記録する。"""

    def __init__(self, fail_pre: bool = False) -> None:
        self.calls: list[tuple[str, int | None, str]] = []
        self.fail_pre = fail_pre
        self.number = 100

    def pre(self, description: str) -> int:
        if self.fail_pre:
            raise SnapshotError("許可がありません")
        self.number += 1
        self.calls.append(("pre", None, description))
        return self.number

    def post(self, pre_number: int, description: str) -> int:
        self.number += 1
        self.calls.append(("post", pre_number, description))
        return self.number


@pytest.fixture
def tree(root: Path) -> Path:
    """整理候補と、残すべき書類が混ざったツリー。"""
    write(root / "docs" / "report.pdf", "keep me")
    write(root / "docs" / "~$report.docx", "lock")
    write(root / "docs" / "Thumbs.db", "junk")
    write(root / "Documents" / "DMMGames" / "a.dat", "game data")
    write(root / "Documents" / "DMMGames" / "sub" / "b.dat", "more game data")
    write(root / "Documents" / "letter.docx", "keep me too")
    (root / "empty").mkdir()
    return root


def make_plan(conn: sqlite3.Connection, root_id: int, root: Path, tmp_path: Path) -> tuple[Plan, Path]:
    """走査してからプランを作り、ファイルに書いて読み直す（実際の使い方と同じ経路）。"""
    scan_root(conn, root_id, root, SAFETY)
    report = build_report(conn, root_id, "test", HYGIENE)
    plan_file = tmp_path / "plans" / "p.toml"
    write_plan(build_plan(report, "test", root), plan_file)
    return load_plan(plan_file), plan_file


def run_apply(conn: sqlite3.Connection, plan: Plan, tmp_path: Path, snapshot: FakeSnapshots | None = None):
    """隔離先と実行記録の置き場所をテスト用にして実行する。"""
    return apply_plan(
        conn,
        plan,
        quarantine_dir=tmp_path / "quarantine",
        journal_dir=tmp_path / "journal",
        snapshot=snapshot,
    )


def test_plan_lists_findings_and_round_trips(
    conn: sqlite3.Connection, root_id: int, tree: Path, tmp_path: Path
) -> None:
    plan, plan_file = make_plan(conn, root_id, tree, tmp_path)
    paths = {op.path for op in plan.ops}
    assert paths == {"docs/~$report.docx", "docs/Thumbs.db", "Documents/DMMGames", "empty"}
    game = next(op for op in plan.ops if op.path == "Documents/DMMGames")
    assert (game.is_dir, game.files, game.size) == (True, 2, len("game data") + len("more game data"))
    # 1操作が1行なので、行を消せばプランから外せる
    lines = plan_file.read_text(encoding="utf-8").splitlines()
    plan_file.write_text("\n".join(l for l in lines if "Thumbs.db" not in l) + "\n", encoding="utf-8")
    assert "docs/Thumbs.db" not in {op.path for op in load_plan(plan_file).ops}


def test_plan_can_filter_categories(conn: sqlite3.Connection, root_id: int, tree: Path) -> None:
    scan_root(conn, root_id, tree, SAFETY)
    report = build_report(conn, root_id, "test", HYGIENE)
    plan = build_plan(report, "test", tree, [rules.CAT_OS_JUNK])
    assert [op.path for op in plan.ops] == ["docs/Thumbs.db"]


def test_plan_leaves_out_items_that_need_review(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    write(root / "plan.xlsx", "mine")
    write(root / "plan (1).xlsx", "theirs")
    scan_root(conn, root_id, root, SAFETY)
    hash_root(conn, root_id, root)
    report = build_report(conn, root_id, "test", HYGIENE)
    assert build_plan(report, "test", root).ops == []
    assert [op.path for op in build_plan(report, "test", root, include_review=True).ops] == ["plan (1).xlsx"]


def test_plan_file_survives_awkward_names(conn: sqlite3.Connection, root_id: int, root: Path, tmp_path: Path) -> None:
    name = 'odd "name" \\ with\ttab.tmp'
    write(root / "dir" / name)
    plan, _ = make_plan(conn, root_id, root, tmp_path)
    assert [op.path for op in plan.ops] == [f"dir/{name}"]


def test_load_plan_rejects_paths_outside_the_root(tmp_path: Path) -> None:
    bad = write(
        tmp_path / "bad.toml",
        'id = "x"\nroot = "r"\nroot_path = "/tmp/r"\ncreated_at = "t"\n'
        'ops = [{ action = "quarantine", path = "../etc", is_dir = true, files = 0, size = 0, inode = 1, mtime_ns = 1 }]\n',
    )
    with pytest.raises(PlanError):
        load_plan(bad)


def test_apply_moves_to_quarantine_and_keeps_the_rest(
    conn: sqlite3.Connection, root_id: int, tree: Path, tmp_path: Path
) -> None:
    plan, _ = make_plan(conn, root_id, tree, tmp_path)
    snaps = FakeSnapshots()
    result = run_apply(conn, plan, tmp_path, snaps)

    assert len(result.done) == 4 and not result.skipped
    q = tmp_path / "quarantine" / plan.id
    assert (q / "Documents" / "DMMGames" / "sub" / "b.dat").read_text() == "more game data"
    assert (q / "docs" / "Thumbs.db").exists()
    assert (tree / "docs" / "report.pdf").exists() and (tree / "Documents" / "letter.docx").exists()
    assert not (tree / "Documents" / "DMMGames").exists()
    # 前後のスナップショットが対になっている
    assert [c[0] for c in snaps.calls] == ["pre", "post"]
    assert snaps.calls[1][1] == result.pre_snapshot
    # カタログも更新済みなので、次の走査で「消えた」ものは出ない
    assert live_paths(conn, root_id) == {"docs/report.pdf", "Documents/letter.docx"}
    res = scan_root(conn, root_id, tree, SAFETY)
    assert (res.added, res.gone, res.held) == (0, 0, 0)


def test_quarantining_most_files_does_not_trip_the_safety_valve(
    conn: sqlite3.Connection, root_id: int, root: Path, tmp_path: Path
) -> None:
    for i in range(20):
        write(root / "Documents" / "DMMGames" / f"{i}.dat", str(i))
    write(root / "keep.pdf")
    plan, _ = make_plan(conn, root_id, root, tmp_path)
    run_apply(conn, plan, tmp_path)
    res = scan_root(conn, root_id, root, SAFETY)
    assert (res.gone, res.held) == (0, 0)


def test_apply_skips_items_changed_since_the_plan(
    conn: sqlite3.Connection, root_id: int, tree: Path, tmp_path: Path
) -> None:
    plan, _ = make_plan(conn, root_id, tree, tmp_path)
    # フォルダの中にファイルが増えた / ファイルが別のものに置き換わった
    write(tree / "Documents" / "DMMGames" / "new.sav", "saved game")
    (tree / "docs" / "Thumbs.db").unlink()
    write(tree / "docs" / "Thumbs.db", "junk")
    result = run_apply(conn, plan, tmp_path)

    skipped = {o.path: o.detail for o in result.skipped}
    assert skipped == {
        "Documents/DMMGames": "プランの作成後に変更された",
        "docs/Thumbs.db": "別のものに置き換わった",
    }
    assert (tree / "Documents" / "DMMGames" / "new.sav").exists()


def test_snapshot_failure_moves_nothing(
    conn: sqlite3.Connection, root_id: int, tree: Path, tmp_path: Path
) -> None:
    plan, _ = make_plan(conn, root_id, tree, tmp_path)
    with pytest.raises(SnapshotError):
        run_apply(conn, plan, tmp_path, FakeSnapshots(fail_pre=True))
    assert (tree / "Documents" / "DMMGames" / "a.dat").exists()
    assert not journal_path(tmp_path / "journal", plan.id).exists()


def test_undo_restores_files_and_catalog_records(
    conn: sqlite3.Connection, root_id: int, tree: Path, tmp_path: Path
) -> None:
    plan, _ = make_plan(conn, root_id, tree, tmp_path)
    conn.execute("UPDATE entries SET sha256 = 'abc' WHERE relpath = 'Documents/DMMGames/a.dat'")
    conn.commit()
    before = live_paths(conn, root_id, files_only=False)
    run_apply(conn, plan, tmp_path)

    snaps = FakeSnapshots()
    result = undo_plan(conn, plan.id, journal_dir=tmp_path / "journal", snapshot=snaps)
    assert len(result.done) == 4 and not result.skipped
    assert (tree / "Documents" / "DMMGames" / "sub" / "b.dat").read_text() == "more game data"
    assert [c[0] for c in snaps.calls] == ["pre", "post"]
    assert not (tmp_path / "quarantine" / plan.id).exists()  # 空になった隔離先は片付ける
    # 隔離前の行が生き返り、ハッシュなどの記録を引き継いでいる
    assert live_paths(conn, root_id, files_only=False) == before
    row = conn.execute("SELECT sha256 FROM entries WHERE relpath = 'Documents/DMMGames/a.dat' AND gone_at IS NULL").fetchone()
    assert row["sha256"] == "abc"
    res = scan_root(conn, root_id, tree, SAFETY)
    assert (res.added, res.moved, res.gone) == (0, 0, 0)
    # 2回目の取り消しでは、戻すものが無い
    with pytest.raises(PlanError):
        undo_plan(conn, plan.id, journal_dir=tmp_path / "journal", snapshot=None)


def test_undo_does_not_overwrite_new_files(
    conn: sqlite3.Connection, root_id: int, tree: Path, tmp_path: Path
) -> None:
    plan, _ = make_plan(conn, root_id, tree, tmp_path)
    run_apply(conn, plan, tmp_path)
    write(tree / "docs" / "Thumbs.db", "regenerated by Windows")
    result = undo_plan(conn, plan.id, journal_dir=tmp_path / "journal", snapshot=None)
    assert {o.path: o.detail for o in result.skipped} == {"docs/Thumbs.db": "元の場所に別のものがある"}
    assert (tree / "docs" / "Thumbs.db").read_text() == "regenerated by Windows"
    assert (tmp_path / "quarantine" / plan.id / "docs" / "Thumbs.db").read_text() == "junk"


def test_cli_plan_apply_undo(tmp_path: Path, tree: Path) -> None:
    quarantine = tmp_path / "quarantine"
    cfg = write(
        tmp_path / "c.toml",
        f'[[roots]]\nname = "data"\npath = "{tree}"\n'
        f'[hygiene]\ndevice_names = []\n[plan]\nquarantine_dir = "{quarantine}"\nsnapper_config = ""\n',
    )
    db = tmp_path / "catalog.db"
    common = ["--config", str(cfg), "--db", str(db)]
    plan_file = tmp_path / "plan.toml"

    assert main([*common, "scan"]) == 0
    assert main([*common, "plan", "data", "--category", "os-junk", "-o", str(plan_file)]) == 0
    # --yes が無ければ確認だけで、何も動かさない
    assert main([*common, "apply", str(plan_file)]) == 0
    assert (tree / "docs" / "Thumbs.db").exists()

    assert main([*common, "apply", str(plan_file), "--yes"]) == 0
    assert not (tree / "docs" / "Thumbs.db").exists()
    assert (tree / "Documents" / "DMMGames" / "a.dat").exists()  # 指定しなかったカテゴリは残る

    assert main([*common, "undo", str(plan_file)]) == 0
    assert not (tree / "docs" / "Thumbs.db").exists()
    assert main([*common, "undo", str(plan_file), "--yes"]) == 0
    assert (tree / "docs" / "Thumbs.db").read_text() == "junk"


def test_cli_unknown_category_fails(tmp_path: Path, tree: Path) -> None:
    common = ["--config", str(tmp_path / "none.toml"), "--db", str(tmp_path / "c.db")]
    assert main([*common, "scan", str(tree)]) == 0
    assert main([*common, "plan", str(tree), "--category", "nope"]) == 2



# --- 移動・改名（整理の提案から作るプラン） ---


def make_move_plan(conn: sqlite3.Connection, root_id: int, root: Path, tmp_path: Path, moves: list[tuple[str, str]]) -> Plan:
    """移動・改名のプランを作り、書いて読み直す。"""
    from tag_keeper.plan import ACTION_MOVE, CAT_MOVE, PlanOp, fingerprint

    scan_root(conn, root_id, root, SAFETY)
    plan = Plan("20260101-000000-test-organize", "test", root, "t")
    for src, dest in moves:
        fp = fingerprint(root / src)
        plan.ops.append(PlanOp(ACTION_MOVE, src, False, fp.files, fp.size, fp.inode, fp.mtime_ns, CAT_MOVE, "", dest=dest))
    write_plan(plan, tmp_path / "plans" / "m.toml")
    return load_plan(tmp_path / "plans" / "m.toml")


def test_move_plan_renames_and_follows_catalog_and_tags(
    conn: sqlite3.Connection, root_id: int, root: Path, tmp_path: Path
) -> None:
    from tag_keeper.tags import TagStore

    write(root / "inbox" / "downloadfile.pdf", "statement")
    write(root / "bank" / "old.pdf", "old")
    conn.execute("UPDATE entries SET sha256 = 'abc'")  # 走査前なので何も起きない
    plan = make_move_plan(conn, root_id, root, tmp_path, [("inbox/downloadfile.pdf", "bank/2025/20250414_明細書.pdf")])
    conn.execute("UPDATE entries SET sha256 = 'abc' WHERE relpath = 'inbox/downloadfile.pdf'")
    conn.commit()
    tags = TagStore(tmp_path / "tags", host="t")
    tags.add("test", ["inbox/downloadfile.pdf"], "相手:Revolut")

    result = apply_plan(conn, plan, quarantine_dir=tmp_path / "q", journal_dir=tmp_path / "journal", snapshot=None, tags=tags)
    assert len(result.done) == 1
    assert (root / "bank" / "2025" / "20250414_明細書.pdf").read_text() == "statement"  # 無いフォルダは作る
    # カタログの行（ハッシュ）とタグが新しいパスへ移り、移動の履歴も残る
    row = conn.execute("SELECT sha256 FROM entries WHERE relpath = 'bank/2025/20250414_明細書.pdf' AND gone_at IS NULL").fetchone()
    assert row["sha256"] == "abc"
    assert conn.execute("SELECT old_relpath, source FROM moves").fetchone()[:] == ("inbox/downloadfile.pdf", "plan")
    assert tags.tags_of("test", "bank/2025/20250414_明細書.pdf")["direct"][0]["tag"] == "相手:Revolut"
    res = scan_root(conn, root_id, root, SAFETY)
    assert (res.added, res.moved, res.gone) == (0, 0, 0)  # カタログは更新済みなので、走査で何も起きない

    undone = undo_plan(conn, plan.id, journal_dir=tmp_path / "journal", snapshot=None, tags=tags)
    assert len(undone.done) == 1
    assert (root / "inbox" / "downloadfile.pdf").exists()
    assert (root / "bank" / "old.pdf").exists()  # 移動先のフォルダは片付けない（利用者のフォルダ）
    assert tags.tags_of("test", "inbox/downloadfile.pdf")["direct"][0]["tag"] == "相手:Revolut"
    assert conn.execute("SELECT sha256 FROM entries WHERE relpath = 'inbox/downloadfile.pdf' AND gone_at IS NULL").fetchone()["sha256"] == "abc"


def test_move_never_overwrites(conn: sqlite3.Connection, root_id: int, root: Path, tmp_path: Path) -> None:
    write(root / "inbox" / "a.pdf", "new")
    plan = make_move_plan(conn, root_id, root, tmp_path, [("inbox/a.pdf", "done/a.pdf")])
    write(root / "done" / "a.pdf", "existing")
    result = apply_plan(conn, plan, quarantine_dir=tmp_path / "q", journal_dir=tmp_path / "journal", snapshot=None)
    assert [o.detail for o in result.skipped] == ["移動先に同じ名前のものがある"]
    assert (root / "done" / "a.pdf").read_text() == "existing"


def test_move_plan_rejects_names_the_cloud_cannot_store(tmp_path: Path) -> None:
    bad = write(
        tmp_path / "bad.toml",
        'id = "x"\nroot = "r"\nroot_path = "/tmp/r"\ncreated_at = "t"\n'
        'ops = [{ action = "move", path = "a.pdf", dest = "b?.pdf", is_dir = false, files = 1, size = 1, inode = 1, mtime_ns = 1 }]\n',
    )
    with pytest.raises(PlanError, match="使えない文字"):
        load_plan(bad)
