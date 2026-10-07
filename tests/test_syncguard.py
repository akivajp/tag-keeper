"""同期クライアントとの連携（大量削除で同期を止めない仕組み）のテスト。

onedrive と systemctl は、呼び出しを記録する偽のスクリプトに差し替える。
"""

from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

from conftest import write
from tag_keeper.config import HygieneConfig, SafetyConfig
from tag_keeper.plan import (
    apply_plan,
    build_plan,
    journal_path,
    read_journal,
    retry_sync,
    sync_paused,
)
from tag_keeper.report import build_report
from tag_keeper.scan import scan_root
from tag_keeper.syncguard import OneDriveGuard, SyncError, count_children

SAFETY = SafetyConfig(mass_missing_ratio=0.2, mass_missing_min=3)
HYGIENE = HygieneConfig(device_names=[], app_data_min_files=10_000)


def fake_commands(tmp_path: Path, sync_dir: Path, *, threshold: int = 5, sync_fails: bool = False) -> tuple[Path, Path, Path]:
    """onedrive と systemctl の偽物を作る。呼び出しは calls.log に1行ずつ記録する。"""
    calls = tmp_path / "calls.log"
    state = tmp_path / "service-state"
    state.write_text("active")
    onedrive = tmp_path / "onedrive"
    fail = "true" if sync_fails else "false"
    onedrive.write_text(
        f"""#!/bin/sh
echo "onedrive $*" >> {calls}
if [ "$1" = "--display-config" ]; then
  echo "Config option 'sync_dir'                     = {sync_dir}"
  echo "Config option 'classify_as_big_delete'       = {threshold}"
  exit 0
fi
if {fail}; then
  echo "ERROR: An attempt to remove a large volume of data from OneDrive has been detected. Exiting client"
  exit 1
fi
echo "Deleting item from Microsoft OneDrive: ./Documents/DMMGames"
echo "Sync with Microsoft OneDrive is complete"
"""
    )
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        f"""#!/bin/sh
echo "systemctl $*" >> {calls}
case "$2" in
  stop) echo inactive > {state} ;;
  start) echo active > {state} ;;
  is-active) cat {state} ;;
esac
"""
    )
    for f in (onedrive, systemctl):
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    return onedrive, systemctl, calls


@pytest.fixture
def game_tree(root: Path) -> Path:
    """子の多いアプリのデータのフォルダを含むツリー。"""
    for i in range(8):
        write(root / "Documents" / "DMMGames" / f"{i}.dat", str(i))
    write(root / "Documents" / "Thumbs.db", "junk")
    write(root / "Documents" / "letter.docx", "keep")
    return root


def make_guard(tmp_path: Path, sync_dir: Path, **kw: object) -> tuple[OneDriveGuard, Path]:
    onedrive, systemctl, calls = fake_commands(tmp_path, sync_dir, **kw)  # type: ignore[arg-type]
    return OneDriveGuard(command=str(onedrive), systemctl=str(systemctl)), calls


def run_apply(conn: sqlite3.Connection, root_id: int, root: Path, tmp_path: Path, guard: OneDriveGuard):
    scan_root(conn, root_id, root, SAFETY)
    plan = build_plan(build_report(conn, root_id, "test", HYGIENE), "test", root)
    result = apply_plan(
        conn,
        plan,
        quarantine_dir=tmp_path / "quarantine",
        journal_dir=tmp_path / "journal",
        snapshot=None,
        sync=guard,
    )
    return plan, result


def test_count_children(game_tree: Path) -> None:
    assert count_children(game_tree / "Documents" / "DMMGames") == 8
    assert count_children(game_tree / "Documents") == 11  # DMMGames 自身 + 8 + 2
    assert count_children(game_tree / "Documents" / "Thumbs.db") == 0


def test_guard_reads_onedrive_config(tmp_path: Path, root: Path) -> None:
    guard, _ = make_guard(tmp_path, root, threshold=1234)
    assert guard.threshold() == 1234
    assert guard.sync_dir() == root.resolve()
    assert guard.relpath(root / "a" / "b.txt") == "a/b.txt"
    assert guard.needs_pause([10, 1234]) and not guard.needs_pause([10, 1233])
    guard.mode = "always"
    assert guard.needs_pause([])


def test_big_delete_pauses_flushes_and_resumes(
    conn: sqlite3.Connection, root_id: int, game_tree: Path, tmp_path: Path
) -> None:
    guard, calls = make_guard(tmp_path, game_tree, threshold=5)
    plan, result = run_apply(conn, root_id, game_tree, tmp_path, guard)

    assert result.synced and result.sync_error is None
    log = calls.read_text().splitlines()
    stop = log.index("systemctl --user stop onedrive.service")
    sync = next(i for i, l in enumerate(log) if l.startswith("onedrive --sync"))
    start = log.index("systemctl --user start onedrive.service")
    assert stop < sync < start
    # しきい値は、消した子の数（8）に余裕を足した値まで上げる。--force は使わない
    assert "--force" not in log[sync]
    limit = int(log[sync].split("--classify-as-big-delete ")[1])
    assert limit >= 8
    events = [r["event"] for r in read_journal(journal_path(tmp_path / "journal", plan.id))]
    assert events.index("sync-pause") < events.index("quarantine") < events.index("sync-flush") < events.index("sync-resume")
    assert sync_paused(journal_path(tmp_path / "journal", plan.id)) is None


def test_small_delete_leaves_sync_alone(
    conn: sqlite3.Connection, root_id: int, game_tree: Path, tmp_path: Path
) -> None:
    guard, calls = make_guard(tmp_path, game_tree, threshold=1000)
    _, result = run_apply(conn, root_id, game_tree, tmp_path, guard)
    assert not result.synced
    assert all("stop" not in l and "--sync" not in l for l in calls.read_text().splitlines())


def test_failed_flush_keeps_sync_stopped_and_can_be_retried(
    conn: sqlite3.Connection, root_id: int, game_tree: Path, tmp_path: Path
) -> None:
    guard, calls = make_guard(tmp_path, game_tree, threshold=5, sync_fails=True)
    plan, result = run_apply(conn, root_id, game_tree, tmp_path, guard)

    # 反映に失敗したら、再開しない（再開しても大量削除で終了を繰り返すため）
    assert result.sync_error and "大量削除" in result.sync_error
    assert "systemctl --user start onedrive.service" not in calls.read_text()
    journal = journal_path(tmp_path / "journal", plan.id)
    assert sync_paused(journal)
    assert (tmp_path / "quarantine" / plan.id / "Documents" / "DMMGames").exists()

    # 原因が解消したら、再試行で反映して再開できる
    fixed, calls2 = make_guard(tmp_path, game_tree, threshold=5)
    assert retry_sync(plan.id, journal_dir=tmp_path / "journal", sync=fixed) is None
    assert "systemctl --user start onedrive.service" in calls2.read_text()
    assert sync_paused(journal) is None


def test_pause_failure_moves_nothing(
    conn: sqlite3.Connection, root_id: int, game_tree: Path, tmp_path: Path
) -> None:
    guard, _ = make_guard(tmp_path, game_tree, threshold=5)
    guard.systemctl = str(tmp_path / "missing-systemctl")
    with pytest.raises(SyncError):
        run_apply(conn, root_id, game_tree, tmp_path, guard)
    assert (game_tree / "Documents" / "DMMGames" / "0.dat").exists()
