"""cloud-sync（rclone bisync の定期実行）との連携のテスト。

cloud-sync と systemctl は、呼び出しを記録する偽のスクリプトに差し替える。
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from webtest import TestApp

from conftest import write
from tag_keeper.jobs import JobManager
from tag_keeper.syncguard import CloudSyncGuard, SyncError, make_guard
from tag_keeper.web import server
from tag_keeper.web.server import Settings, create_app

HOST = "127.0.0.1:8090"


def make_exe(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def guard(tmp_path: Path) -> tuple[CloudSyncGuard, Path, Path]:
    """偽の cloud-sync・systemctl で動く窓口と、呼び出しの記録、状態フォルダ。"""
    calls = tmp_path / "calls.log"
    state = tmp_path / "state"
    state.mkdir()
    log = tmp_path / "onedrive.log"
    log.write_text("old line\n")
    sync_dir = tmp_path / "OneDrive"
    sync_dir.mkdir()
    # 偽の cloud-sync: 削除を2件ログに書いて、状態を idle にする
    fake = make_exe(
        tmp_path / "cloud-sync",
        f"""echo "cloud-sync $*" >> {calls}
echo "2026/10/18 19:00:00 INFO  : Documents/old.dat: Deleted" >> {log}
echo "2026/10/18 19:00:00 INFO  : Documents/other.dat: Deleted" >> {log}
echo "同期を開始します"
""",
    )
    systemctl = make_exe(tmp_path / "systemctl", f'echo "systemctl $*" >> {calls}\ncase "$2" in is-active) echo active;; esac\n')
    cfg = write(tmp_path / "cloud-sync.toml", f'[remotes.onedrive]\nremote = "onedrive:"\nlocal = "{sync_dir}"\nflush_max_delete = 30\n')
    g = CloudSyncGuard(
        service="cloud-sync-onedrive.service", command=str(fake), systemctl=str(systemctl),
        state_dir=state, config_file=cfg, wait_seconds=0.3, poll_seconds=0.05,
    )
    (state / "onedrive.json").write_text(json.dumps({"state": "idle", "log": str(log), "last_ok": "2026-10-18T19:00:00+09:00"}))
    return g, calls, state


def test_entry_name_from_service() -> None:
    assert CloudSyncGuard(service="cloud-sync-onedrive.service").entry == "onedrive"
    assert CloudSyncGuard(service="pcloud").entry == "pcloud"
    assert CloudSyncGuard(service="cloud-sync-pcloud.timer").timer_unit == "cloud-sync-pcloud.timer"
    assert isinstance(make_guard("cloud-sync", "onedrive", "auto"), CloudSyncGuard)


def test_pause_stops_timer_and_flush_reflects_deletions(guard: tuple[CloudSyncGuard, Path, Path]) -> None:
    g, calls, _state = guard
    assert g.needs_pause([0])  # 小さな削除でも、timer の同期と重ならないよう常に連携する
    g.pause()
    deleted: list[str] = []
    lines: list[str] = []
    g.flush(10, on_deleted=deleted.append, on_line=lines.append)
    g.resume()
    log = calls.read_text()
    assert "systemctl --user stop cloud-sync-onedrive.timer" in log
    # 削除の上限は設定の flush_max_delete に従い、この実行だけに適用される
    assert "cloud-sync run onedrive --max-delete 30" in log
    assert "systemctl --user start cloud-sync-onedrive.timer" in log
    # 実行中に増えたログの分だけを拾う（実行前の行は拾わない）
    assert deleted == ["Documents/old.dat", "Documents/other.dat"]
    assert lines == ["同期を開始します"]


def test_pause_waits_for_running_sync_and_gives_up(guard: tuple[CloudSyncGuard, Path, Path]) -> None:
    g, _calls, state = guard
    (state / "onedrive.json").write_text(json.dumps({"state": "running", "pid": os.getpid()}))
    with pytest.raises(SyncError, match="終わりません"):
        g.pause()


def test_flush_failure_is_reported(guard: tuple[CloudSyncGuard, Path, Path], tmp_path: Path) -> None:
    g, _calls, _state = guard
    g.command = str(make_exe(tmp_path / "bad", "exit 1\n"))
    with pytest.raises(SyncError, match="失敗"):
        g.flush(1)
    g.command = str(make_exe(tmp_path / "busy", "exit 75\n"))
    with pytest.raises(SyncError, match="別の同期"):
        g.flush(1)


def test_status_and_interrupted_detection(guard: tuple[CloudSyncGuard, Path, Path]) -> None:
    g, _calls, state = guard
    st = g.status()
    assert st["state"] == "idle" and st["timer"] == "active" and st["can_trigger"] is True
    assert st["last_ok"] == "2026-10-18T19:00:00+09:00"
    # 動いているはずなのにプロセスが無ければ「中断」
    (state / "onedrive.json").write_text(json.dumps({"state": "running", "pid": 2**22 + 12345}))
    assert g.status()["state"] == "interrupted"
    assert not g.is_running()
    # 状態ファイルが無ければ unknown
    (state / "onedrive.json").unlink()
    assert g.status()["state"] == "unknown"


def test_trigger_refuses_while_running(guard: tuple[CloudSyncGuard, Path, Path]) -> None:
    g, calls, state = guard
    g.trigger()
    assert "systemctl --user start --no-block cloud-sync-onedrive.service" in calls.read_text()
    (state / "onedrive.json").write_text(json.dumps({"state": "running", "pid": os.getpid()}))
    with pytest.raises(SyncError, match="すでに"):
        g.trigger()


def test_relpath(guard: tuple[CloudSyncGuard, Path, Path], tmp_path: Path) -> None:
    g, _calls, _state = guard
    assert g.relpath(tmp_path / "OneDrive" / "a" / "b.txt") == "a/b.txt"


@pytest.fixture
def web(guard: tuple[CloudSyncGuard, Path, Path], tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestApp, CloudSyncGuard, Path]:
    """cloud-sync と連携するルートを持つ Web アプリ。"""
    g, calls, _state = guard
    cfg = write(
        tmp_path / "config.toml",
        f'[[roots]]\nname = "data"\npath = "{root}"\nsync_client = "cloud-sync"\nsync_service = "onedrive"\n'
        f'[[roots]]\nname = "plain"\npath = "{root}"\n',
    )
    server._guards.clear()
    monkeypatch.setattr(server, "make_guard", lambda client, *_a: g if client == "cloud-sync" else None)
    jobs = JobManager(tmp_path / "logs")
    app = TestApp(create_app(Settings(cfg, tmp_path / "data" / "catalog.db"), jobs=jobs), extra_environ={"HTTP_HOST": HOST})
    return app, g, calls


def test_api_sync_status_and_manual_start(web: tuple[TestApp, CloudSyncGuard, Path]) -> None:
    app, g, calls = web
    items = app.get("/api/sync").json["items"]
    assert [i["root"] for i in items] == ["data"]  # 連携していないルートは出ない
    assert items[0]["state"] == "idle"
    res = app.post_json("/api/sync/data/start", {}, headers={"Origin": f"http://{HOST}"})
    assert res.json["root"] == "data"
    assert "start --no-block cloud-sync-onedrive.service" in calls.read_text()
    # 連携していないルートでは始められない
    app.post_json("/api/sync/plain/start", {}, headers={"Origin": f"http://{HOST}"}, status=409)
    app.post_json("/api/sync/nope/start", {}, headers={"Origin": f"http://{HOST}"}, status=404)
    # 動いている間は二重に始めない
    (g._state_file()).write_text(json.dumps({"state": "running", "pid": os.getpid()}))
    app.post_json("/api/sync/data/start", {}, headers={"Origin": f"http://{HOST}"}, status=409)
    assert app.get("/api/sync").json["items"][0]["state"] == "running"


# 前回の実行（数えない）と、今回の実行の途中までのログ。rclone -v --stats の出力の形
_PREV_RUN = """
===== 2026-10-30T22:00:00+09:00 cloud-sync onedrive =====
2026/10/30 22:00:01 INFO  : old/a.txt: Copied (new)
"""
_CUR_RUN = """
===== 2026-10-30T22:33:29+09:00 cloud-sync onedrive =====
2026/10/30 22:33:40 INFO  : Building Path1 and Path2 listings
2026/10/30 22:34:01 INFO  : 
Transferred:   	          0 B / 0 B, -, 0 B/s, ETA -
Checks:                 0 / 0, -, Listed 3007
Elapsed time:        20.2s

2026/10/30 22:35:09 INFO  : Path1 checking for diffs
2026/10/30 22:35:09 INFO  : Path1: 3 changes: 2 new,    0 modified, 1 deleted
2026/10/30 22:35:09 INFO  : Applying changes
2026/10/30 22:35:09 INFO  : - Path1             Queue copy to Path2                         - /home/u/OneDrive/new/a.txt
2026/10/30 22:35:09 INFO  : - Path1             Queue copy to Path2                         - /home/u/OneDrive/new/b.txt
2026/10/30 22:35:09 INFO  : - Path2             Queue delete                                - /home/u/OneDrive/old/c.txt
2026/10/30 22:35:09 INFO  : - Path1             Do queued copies to                         - Path2
2026/10/30 22:36:00 INFO  : new/a.txt: Copied (new)
2026/10/30 22:36:01 ERROR : new/b.sys: Failed to copy: malwareDetected
2026/10/30 22:36:31 INFO  : 
Transferred:   	    1.172 GiB / 3.554 GiB, 33%, 4.131 MiB/s, ETA 9m50s
Checks:             28164 / 28164, 100%, Listed 45973
Transferred:           98 / 5376, 2%
Elapsed time:      3m50.2s
"""


def test_progress_reads_only_the_current_run_incrementally(guard: tuple[CloudSyncGuard, Path, Path], tmp_path: Path) -> None:
    g, _calls, state = guard
    log = tmp_path / "onedrive.log"
    log.write_text(_PREV_RUN + _CUR_RUN + "2026/10/30 22:36:40 INFO  : old/c.txt: Moved into b")  # 最後の行は書きかけ
    (state / "onedrive.json").write_text(json.dumps(
        {"state": "running", "pid": os.getpid(), "started": "2026-10-30T22:33:29+09:00", "log": str(log)}))
    p = g.status()["progress"]
    assert p["phase"] == "download"
    assert p["changes"] == {"Path1": {"new": 2, "modified": 0, "deleted": 1}}
    assert (p["queued_copies"], p["queued_deletes"], p["copied"], p["deleted"]) == (2, 1, 1, 0)  # 前回の実行の行は数えない
    assert p["errors"] == 1 and "malwareDetected" in p["last_error"]
    assert p["stats"] == {
        "bytes_done": "1.172 GiB", "bytes_total": "3.554 GiB", "bytes_pct": "33%", "speed": "4.131 MiB/s", "eta": "9m50s",
        "checks": 28164, "listed": 45973, "files_done": 98, "files_total": 5376, "elapsed": "3m50.2s",
    }
    # 書きかけだった行が書き終わると、次の問い合わせで増えた分だけ数える
    with open(log, "a") as f:
        f.write("ackup dir\n2026/10/30 22:37:00 INFO  : Updating listings\n2026/10/30 22:37:01 INFO  : Bisync successful\n")
    p = g.progress()
    assert (p["copied"], p["deleted"], p["last_file"], p["phase"]) == (1, 1, "old/c.txt", "finished")


def test_progress_before_the_run_heading_is_written(guard: tuple[CloudSyncGuard, Path, Path], tmp_path: Path) -> None:
    g, _calls, state = guard
    log = tmp_path / "onedrive.log"
    log.write_text(_PREV_RUN)
    # 状態は「実行中」になったが、ログに今回の見出しがまだ無い
    (state / "onedrive.json").write_text(json.dumps(
        {"state": "running", "pid": os.getpid(), "started": "2026-10-30T22:33:29+09:00", "log": str(log)}))
    p = g.progress()
    assert p is not None and p["phase"] == "starting" and p["copied"] == 0
    # 見出しが書かれた後は、今回の分を数える
    with open(log, "a") as f:
        f.write(_CUR_RUN)
    assert g.progress()["copied"] == 1  # type: ignore[index]


def test_find_run_start_across_blocks(tmp_path: Path) -> None:
    from tag_keeper import syncguard

    log = tmp_path / "big.log"
    # 見出しの後ろに 1MB（探すブロックの大きさ）を超える行を置き、見出しが前のブロックにあっても見つかることを確かめる
    filler = "".join(f"2026/10/30 22:00:00 INFO  : - Path1 File is new - dir/{i:07d}.txt\n" for i in range(30000))
    log.write_text("===== 2026-10-30T21:00:00+09:00 cloud-sync pcloud =====\n" + "x\n" * 10
                   + "===== 2026-10-30T22:00:00+09:00 cloud-sync onedrive =====\n" + filler
                   + "===== 2026-10-30T23:00:00+09:00 cloud-sync pcloud =====\n")
    found = syncguard._find_run_start(log, "onedrive")
    assert found is not None
    offset, iso = found
    assert iso == "2026-10-30T22:00:00+09:00"
    with open(log, "rb") as f:
        f.seek(offset)
        assert f.readline().startswith(b"===== 2026-10-30T22:00:00+09:00 cloud-sync onedrive")
    assert syncguard._find_run_start(log, "nope") is None
