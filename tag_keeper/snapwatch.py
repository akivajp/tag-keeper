"""スナップショットの見張り（要件 F-VS-5・F-VS-6）。

- 比べる（F-VS-5）: 最新のスナップショットと1つ前のものとで、ルートの中のファイルを比べ、消えた・変わった数を数える。
  多すぎれば（同期の事故・ランサムウェアなど）警告する。結果は、スナップショットの組ごとにデータのフォルダへ保存して使い回す
- 守る: 警告したスナップショットを、snapper の自動削除の対象から外す（利用者が選んだときだけ。snapper modify）
- スナップショットにだけ残る大きなファイル（F-VS-6）: 今のツリーに無い（パスと大きさで比べて）大きなファイルを、
  スナップショットをさかのぼって一覧する。保持期間の見直しの材料

スナップショットの発見と、その中のパスの求め方は history（btrfs-timeline）に任せる。読むだけで、何も書き込まない
（守るときの snapper を除く）。
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tag_keeper import history
from tag_keeper.scan import walk_tree

log = logging.getLogger(__name__)

# 消えたファイルがこの割合を超え、かつこの数を超えたら警告する
DELETE_RATIO = 0.1
DELETE_MIN = 200
# 変わったファイル（同じパスで大きさか更新日時が違う）がこの割合を超え、かつこの数を超えたら警告する（暗号化されたなど）
CHANGE_RATIO = 0.3
CHANGE_MIN = 300


@dataclass
class Comparison:
    """2つのスナップショットのあいだの変化。"""

    root: str
    older: str
    newer: str
    older_at: str
    newer_at: str
    files_before: int = 0
    files_after: int = 0
    deleted: int = 0
    changed: int = 0
    added: int = 0
    deleted_bytes: int = 0
    alert: str = ""  # 警告の理由（無ければ空）
    examples: list[str] = field(default_factory=list)  # 消えたもの・変わったものの例

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _files(base: Path, exclude: Iterable[str], exclude_paths: Iterable[str]) -> dict[str, tuple[int, int]]:
    """フォルダの配下のファイル（相対パス → (大きさ, 更新日時)）。読めなければ空。"""
    if not base.is_dir():
        return {}
    walk = walk_tree(base, exclude, exclude_paths=exclude_paths)
    return {p: (ob.size, ob.mtime_ns) for p, ob in walk.observed.items() if not ob.is_dir}


def compare(root_name: str, root_path: Path, older: Any, newer: Any, *, exclude: Iterable[str] = (), exclude_paths: Iterable[str] = ()) -> Comparison:
    """2つのスナップショット（history のスナップショット）のあいだで、ルートの中のファイルを比べる。"""
    a = _files(history.snapshot_path(root_path, "", older.id), exclude, exclude_paths)
    b = _files(history.snapshot_path(root_path, "", newer.id), exclude, exclude_paths)
    deleted = [p for p in a if p not in b]
    changed = [p for p in a if p in b and a[p] != b[p]]
    c = Comparison(
        root_name, older.id, newer.id, older.taken_at.isoformat() if older.taken_at else "", newer.taken_at.isoformat() if newer.taken_at else "",
        len(a), len(b), len(deleted), len(changed), sum(1 for p in b if p not in a), sum(a[p][0] for p in deleted),
    )
    reasons = []
    if c.deleted > DELETE_MIN and c.deleted > DELETE_RATIO * max(c.files_before, 1):
        reasons.append(f"{c.deleted:,} 件（{c.deleted / max(c.files_before, 1):.0%}）が消えた")
    if c.changed > CHANGE_MIN and c.changed > CHANGE_RATIO * max(c.files_before, 1):
        reasons.append(f"{c.changed:,} 件（{c.changed / max(c.files_before, 1):.0%}）が書き換わった")
    c.alert = "、".join(reasons)
    c.examples = sorted(deleted)[:5] + sorted(changed)[:5]
    return c


class Watch:
    """ルートごとに、最新のスナップショットの組の比べた結果を保存して使い回す（データのフォルダの snapwatch.json）。"""

    def __init__(self, state_file: Path) -> None:
        self.state_file = state_file

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.state_file)

    def latest(self, root_name: str) -> dict[str, Any] | None:
        """保存してある最新の結果（無ければ None）。"""
        return self._load().get(root_name, {}).get("latest")

    def check(self, root_name: str, root_path: Path, *, exclude: Iterable[str] = (), exclude_paths: Iterable[str] = (), force: bool = False) -> dict[str, Any] | None:
        """最新の2つのスナップショットを比べる（同じ組を比べたことがあれば、保存した結果を返す）。btrfs でなければ None。"""
        try:
            _mount, snaps = history.snapshots_for(root_path)
        except history.HistoryError:
            return None
        snaps = [s for s in snaps if history.snapshot_path(root_path, "", s.id).is_dir()]
        if len(snaps) < 2:
            return None
        older, newer = snaps[-2], snaps[-1]
        data = self._load()
        cur = data.get(root_name, {}).get("latest")
        if not force and cur and cur.get("older") == older.id and cur.get("newer") == newer.id:
            return cur
        c = compare(root_name, root_path, older, newer, exclude=exclude, exclude_paths=exclude_paths).to_dict()
        entry = data.setdefault(root_name, {})
        entry["latest"] = c
        if c["alert"]:
            # 警告は消さずに残す（次の組を比べた後も、守るかどうかを決められるように）
            alerts = entry.setdefault("alerts", [])
            if not any(a.get("newer") == c["newer"] for a in alerts):
                alerts.append(c)
                log.warning("スナップショットのあいだで大きな変化: %s（#%s → #%s）: %s", root_name, older.id, newer.id, c["alert"])
            entry["alerts"] = alerts[-20:]
        self._save(data)
        return c

    def alerts(self, root_name: str) -> list[dict[str, Any]]:
        return list(self._load().get(root_name, {}).get("alerts", []))

    def mark_protected(self, root_name: str, snapshot_id: str) -> None:
        data = self._load()
        for a in data.get(root_name, {}).get("alerts", []):
            if a.get("older") == snapshot_id or a.get("newer") == snapshot_id:
                a.setdefault("protected", []).append(snapshot_id)
        self._save(data)

    def dismiss(self, root_name: str, newer: str) -> None:
        data = self._load()
        entry = data.get(root_name, {})
        entry["alerts"] = [a for a in entry.get("alerts", []) if a.get("newer") != newer]
        self._save(data)


def protect(snapper_config: str, snapshot_id: str, *, command: str = "snapper") -> None:
    """スナップショットを snapper の自動削除の対象から外す（cleanup を空にし、印を付ける）。失敗したら RuntimeError。"""
    if not snapper_config:
        raise RuntimeError("snapper の設定（[plan] snapper_config）がありません")
    cmd = [command, "-c", snapper_config, "modify", "--cleanup-algorithm", "", "--userdata", "important=yes,tag-keeper=protected", snapshot_id]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"snapper を実行できません: {e}") from e
    if res.returncode != 0:
        raise RuntimeError(f"snapper が失敗しました: {(res.stderr or res.stdout).strip()}")
    log.info("スナップショットを自動削除の対象から外しました: %s #%s", snapper_config, snapshot_id)


@dataclass
class SnapshotOnly:
    """スナップショットにだけ残っている大きなファイル。"""

    relpath: str
    size: int
    snapshots: int  # 残っているスナップショットの数
    newest: str  # 残っている最も新しいスナップショット
    newest_at: str


def snapshot_only_files(
    root_path: Path, live: dict[str, int], *, min_bytes: int, exclude: Iterable[str] = (), exclude_paths: Iterable[str] = (),
    on_progress: Callable[[int, int], None] | None = None,
) -> list[SnapshotOnly]:
    """スナップショットにだけ残っている大きなファイル（今のツリーに同じパス・同じ大きさのものが無い）を、大きい順に返す。

    Args:
        live: 今のツリーのファイル（相対パス → 大きさ）。
        min_bytes: これより小さいものは数えない。
        on_progress: スナップショットを1つ読むたびに（読んだ数, 全体の数）を渡す。
    """
    _mount, snaps = history.snapshots_for(root_path)
    found: dict[tuple[str, int], SnapshotOnly] = {}
    for i, s in enumerate(snaps, 1):
        files = _files(history.snapshot_path(root_path, "", s.id), exclude, exclude_paths)
        for rel, (size, _mtime) in files.items():
            if size < min_bytes or live.get(rel) == size:
                continue
            key = (rel, size)
            cur = found.get(key)
            at = s.taken_at.isoformat() if s.taken_at else ""
            if cur is None:
                found[key] = SnapshotOnly(rel, size, 1, s.id, at)
            else:
                cur.snapshots += 1
                cur.newest, cur.newest_at = s.id, at
        if on_progress is not None:
            on_progress(i, len(snaps))
    return sorted(found.values(), key=lambda x: (-x.size, x.relpath))
