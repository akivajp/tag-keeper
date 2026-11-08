"""スナップショットの見張り（比較・警告・守る・スナップショットにだけ残るファイル）と定期レポートのテスト。

btrfs のスナップショットは、フォルダを並べた偽物で代用する（history.snapshots_for を差し替える）。
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import stat
from pathlib import Path

import pytest

from conftest import write
from tag_keeper import digest, history, snapwatch
from tag_keeper.config import Config, RootConfig, SafetyConfig
from tag_keeper.scan import scan_root
from tag_keeper.tags import TagStore

SAFETY = SafetyConfig(mass_missing_ratio=0.2, mass_missing_min=3)


@pytest.fixture
def snaps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """root の偽のスナップショットを作る部品。make(id, files) で1つ足す。"""
    from btrfs_timeline.core.mounts import MountPoint
    from btrfs_timeline.core.snapshots import Snapshot

    mnt = tmp_path / "mnt"
    root = mnt / "root"
    root.mkdir(parents=True)
    mount = MountPoint(mount_point=str(mnt), fs_type="btrfs", device="fake", subvol="/", root="/")
    made: list[Snapshot] = []

    def make(sid: int, files: dict[str, str]) -> None:
        base = mnt / ".snapshots" / str(sid) / "snapshot"
        for rel, content in files.items():
            write(base / "root" / rel, content)
        (base / "root").mkdir(parents=True, exist_ok=True)
        made.append(Snapshot(id=str(sid), root=str(base), taken_at=dt.datetime(2026, 11, 1, sid, tzinfo=dt.timezone.utc), description="", layout="snapper"))

    monkeypatch.setattr(history, "snapshots_for", lambda path: (mount, list(made)))
    return root, make


def test_comparison_alerts_on_mass_deletion_and_is_cached(snaps, tmp_path: Path) -> None:
    root, make = snaps
    files = {f"d/f{i}.txt": str(i) for i in range(300)}
    make(1, files)
    make(2, {k: v for k, v in files.items() if int(k[3:-4]) < 50})  # 250 件が消えた
    watch = snapwatch.Watch(tmp_path / "w.json")
    c = watch.check("r", root)
    assert c is not None and (c["deleted"], c["files_before"], c["files_after"]) == (250, 300, 50)
    assert "250 件" in c["alert"] and watch.alerts("r")[0]["newer"] == "2"
    assert watch.check("r", root) == c  # 同じ組は比べ直さない
    watch.dismiss("r", "2")
    assert watch.alerts("r") == []
    make(3, {k: v for k, v in files.items() if int(k[3:-4]) < 50})
    assert watch.check("r", root)["alert"] == ""  # 変化の無い組


def test_protect_runs_snapper_modify(tmp_path: Path) -> None:
    calls = tmp_path / "calls"
    fake = write(tmp_path / "snapper", f'#!/bin/sh\necho "$@" > {calls}\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    snapwatch.protect("home", "515", command=str(fake))
    assert calls.read_text().split() == ["-c", "home", "modify", "--cleanup-algorithm", "--userdata", "important=yes,tag-keeper=protected", "515"]
    with pytest.raises(RuntimeError):
        snapwatch.protect("", "515")


def test_snapshot_only_files(snaps) -> None:
    root, make = snaps
    make(1, {"big/old.iso": "x" * 2000, "keep.txt": "k"})
    make(2, {"big/old.iso": "x" * 2000, "keep.txt": "k", "big/new.bin": "y" * 3000})
    live = {"keep.txt": 1, "big/new.bin": 3000}  # new.bin は今もある
    res = snapwatch.snapshot_only_files(root, live, min_bytes=1000)
    assert [(x.relpath, x.snapshots, x.newest) for x in res] == [("big/old.iso", 2, "2")]


def test_digest(conn: sqlite3.Connection, root_id: int, root: Path, tmp_path: Path) -> None:
    write(root / "99_Inbox" / "new.pdf", "n")
    write(root / "docs" / "Thumbs.db", "j")
    scan_root(conn, root_id, root, SAFETY)
    config = Config(roots=[RootConfig(name="test", path=root)])
    config.tags.dir = tmp_path / "tags"
    text = digest.build(conn, config, TagStore(tmp_path / "tags"), plans_dir=tmp_path / "plans", journal_dir=tmp_path / "journal")
    assert "## test" in text and "99_Inbox/new.pdf" in text and "OS・アプリが作るゴミ" in text and "## 隔離フォルダ" in text
    out = tmp_path / "vault" / "tag-keeper.md"
    digest.write(out, text)
    assert out.read_text(encoding="utf-8") == text
