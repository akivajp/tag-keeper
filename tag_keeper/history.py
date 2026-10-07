"""版の履歴（要件 F-VS-1・2・4）。

スナップショットの発見・版の判定・過去の時点のフォルダの一覧・復元は btrfs-timeline のコアに任せる（§6-5）。
ここで足すのは、カタログに記録した移動・改名（moves 表）をたどって、
「移動・改名の前のパスにあった版」も同じアイテムの履歴として並べること（パス単位の btrfs-timeline には無い部分）。
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from btrfs_timeline.core import browse as bt_browse
from btrfs_timeline.core import history as bt_history
from btrfs_timeline.core import mounts as bt_mounts
from btrfs_timeline.core import restore as bt_restore
from btrfs_timeline.core import snapshots as bt_snapshots

# スナップショットの一覧を使い回す時間（秒）。毎時のスナップショットに対して十分に短い
SNAPSHOT_CACHE_SECONDS = 60.0
# 移動をさかのぼる回数の上限（記録の誤りで循環しても止まるように）
MAX_LINEAGE = 32

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, Any, list[Any]]] = {}


class HistoryError(RuntimeError):
    """版の履歴を扱えないとき（btrfs でない、スナップショットが無い、など）の例外。"""


def snapshots_for(path: Path) -> tuple[Any, list[Any]]:
    """path を含む btrfs のマウントと、そこにあるスナップショットの一覧（古い順）を返す。"""
    mount = bt_mounts.find_containing_mount(str(path))
    if mount is None:
        raise HistoryError(f"btrfs のマウントの中にありません: {path}")
    key = mount.mount_point
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and time.time() - hit[0] < SNAPSHOT_CACHE_SECONDS:
            return hit[1], hit[2]
    found = bt_snapshots.discover(mount)
    with _cache_lock:
        _cache[key] = (time.time(), mount, found)
    return mount, found


def find_snapshot(path: Path, snapshot_id: str) -> tuple[Any, Any]:
    """ID でスナップショットを探す。見つからなければ HistoryError。"""
    mount, found = snapshots_for(path)
    for snap in found:
        if snap.id == snapshot_id:
            return mount, snap
    raise HistoryError(f"スナップショットがありません: {snapshot_id}")


@dataclass(frozen=True)
class Segment:
    """アイテムがあるパスにあった期間（since 以上、until 未満。None は端が無いことを表す）。"""

    relpath: str
    since: dt.datetime | None
    until: dt.datetime | None


def _parse(ts: str) -> dt.datetime:
    """カタログの日時（UTC の ISO 8601）を読む。"""
    t = dt.datetime.fromisoformat(ts)
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def lineage(conn: sqlite3.Connection, root_id: int, relpath: str) -> list[Segment]:
    """移動・改名の記録をさかのぼり、アイテムがあったパスと期間を新しい順に返す。

    フォルダごと動いた記録（a → b/a）も、配下のパス（b/a/x.pdf → a/x.pdf）に当てはめる。
    """
    moves = conn.execute(
        "SELECT old_relpath, new_relpath, moved_at FROM moves WHERE root_id = ? ORDER BY moved_at DESC, id DESC",
        (root_id,),
    ).fetchall()
    segments: list[Segment] = []
    current, until = relpath, None
    for _ in range(MAX_LINEAGE):
        found = None
        for m in moves:
            moved_at = _parse(m["moved_at"])
            if until is not None and moved_at >= until:
                continue
            new = m["new_relpath"]
            if current == new or current.startswith(new + "/"):
                found = (m["old_relpath"] + current[len(new):], moved_at)
                break
        if found is None:
            break
        segments.append(Segment(current, found[1], until))
        current, until = found
    segments.append(Segment(current, None, until))
    return segments


def _in(segment: Segment, taken_at: dt.datetime | None) -> bool:
    """スナップショットの日時が、その期間に入るか（日時の分からないものは現在のパスにだけ含める）。"""
    if taken_at is None:
        return segment.until is None
    if segment.since is not None and taken_at < segment.since:
        return False
    return segment.until is None or taken_at < segment.until


def _iso(t: dt.datetime | None) -> str | None:
    return t.isoformat() if t is not None else None


def versions(conn: sqlite3.Connection, root_id: int, root_path: Path, relpath: str) -> list[dict[str, Any]]:
    """アイテムの版の一覧を、古い順に返す（移動・改名の前の版を含む）。

    各版は、その版を読み出せるスナップショット（snapshot_id）と、その時点のパス（relpath）を持つ。
    今の版（is_live）は最後に1件だけ付ける。
    """
    target = root_path / relpath
    mount, found = snapshots_for(target)
    out: list[dict[str, Any]] = []
    for seg in reversed(lineage(conn, root_id, relpath)):
        snaps = [s for s in found if _in(seg, s.taken_at)]
        if not snaps:
            continue
        for v in bt_history.list_versions(str(root_path / seg.relpath), include_live=False, snapshot_list=snaps, mount=mount):
            # 移動の前のパスでは、まだ無かった期間は履歴の雑音なので省く
            if not v.exists and seg.until is not None:
                continue
            out.append(
                {
                    "relpath": seg.relpath,
                    "exists": v.exists,
                    "is_live": False,
                    "size": v.size,
                    "mtime": _iso(v.mtime),
                    "first_seen": _iso(v.first_seen),
                    "last_seen": _iso(v.last_seen),
                    "snapshot_id": v.last_snapshot_id,
                    "snapshot_count": v.snapshot_count,
                }
            )
    try:
        st = os.stat(target)
        live = {"exists": True, "size": st.st_size, "mtime": _iso(dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc))}
    except FileNotFoundError:
        live = {"exists": False, "size": None, "mtime": None}
    out.append(
        {"relpath": relpath, "is_live": True, "first_seen": None, "last_seen": None, "snapshot_id": None, "snapshot_count": 0, **live}
    )
    return out


def snapshot_path(root_path: Path, relpath: str, snapshot_id: str) -> Path:
    """スナップショットの中で、root_path/relpath に当たる実パス。"""
    target = root_path / relpath
    mount, snap = find_snapshot(target, snapshot_id)
    return Path(bt_history.path_in_snapshot(snap, str(target), mount=mount))


def list_directory_at(root_path: Path, relpath: str, snapshot_id: str) -> list[dict[str, Any]]:
    """スナップショットの時点のフォルダの中身。今は無いものは exists_now が False になる。"""
    target = root_path / relpath
    mount, snap = find_snapshot(target, snapshot_id)
    entries = bt_browse.list_directory_at(str(target), snap, mount=mount)
    return [
        {
            "name": e.name,
            "is_dir": e.is_directory,
            "size": None if e.is_directory else e.size,
            "mtime": _iso(e.mtime),
            "exists_now": e.exists_now,
        }
        for e in entries
        if not e.is_symlink
    ]


def restore_beside(root_path: Path, relpath: str, version_relpath: str, snapshot_id: str) -> Path:
    """過去の版を、今のファイルの隣に日時付きの別名で復元する（今のファイルは上書きしない。F-VS-2）。

    Args:
        root_path: ルートの絶対パス。
        relpath: 今のパス（復元先の名前の元になる）。
        version_relpath: その版があったときのパス（移動の前のパスのこともある）。
        snapshot_id: 版を読み出すスナップショット。

    Returns:
        復元したファイルのパス。
    """
    target = root_path / relpath
    mount, snap = find_snapshot(target, snapshot_id)
    source = bt_history.path_in_snapshot(snap, str(root_path / version_relpath), mount=mount)
    plan = bt_restore.plan_restore(str(target), source, moment=snap.taken_at)
    result = bt_restore.execute(plan)
    return Path(result.plan.destination)


def snapshot_summaries(path: Path) -> list[dict[str, Any]]:
    """スナップショットの一覧（画面の「この時点を見る」の選択肢）。新しい順。"""
    _mount, found = snapshots_for(path)
    return [
        {"id": s.id, "taken_at": _iso(s.taken_at), "description": s.description}
        for s in reversed(found)
    ]
