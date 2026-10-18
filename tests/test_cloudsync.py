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
