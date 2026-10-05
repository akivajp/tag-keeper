"""ルートの走査。ツリーを読み取り専用でたどり、カタログを差分で更新する。

- ファイル本体は読まない。lstat で得られる属性（サイズ・更新日時・inode）だけを使う。
- 移動・改名は inode で追跡する。同じファイルシステム上の改名では inode が変わらないため、
  消えたパスと同じ inode を持つ新しいパスを「移動」とみなし、行（とハッシュ）を引き継ぐ。
- 大量消失の安全弁（要件 F-SC-7）: 一度の走査で多くのファイルが消えたように見えたら、
  削除として確定せずに保留する。2回続けて見えなかったものだけを確定する。
- 読めなかったフォルダの中身は「消えた」と扱わない。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from tag_keeper.catalog import utcnow
from tag_keeper.config import SafetyConfig

log = logging.getLogger(__name__)


class RootUnavailableError(RuntimeError):
    """ルートが見えない（存在しない・空・マウントが外れている）ときの例外。

    このとき走査を続けると、全ファイルが消えたと誤認するため、走査そのものを止める。
    """


@dataclass(frozen=True)
class Observation:
    """走査で観測した1件（ファイルまたはフォルダ）の属性。"""

    is_dir: bool
    size: int
    mtime_ns: int
    inode: int


@dataclass
class WalkResult:
    """ツリーをたどった結果。"""

    observed: dict[str, Observation] = field(default_factory=dict)
    # 読めなかったフォルダ（ルートからの相対パス）。中身は消えたと扱わない
    unreadable: list[str] = field(default_factory=list)


@dataclass
class ScanResult:
    """1回の走査で起きた変化の件数。"""

    seen: int = 0
    added: int = 0
    changed: int = 0
    moved: int = 0
    gone: int = 0
    held: int = 0
    unreadable: int = 0

    @property
    def status(self) -> str:
        """消えたものの確定を保留したなら "held"、そうでなければ "committed"。"""
        return "held" if self.held else "committed"


def is_excluded(relpath: str, name: str, patterns: Iterable[str]) -> bool:
    """パスが除外パターンのどれかに一致するかを返す。

    `/` を含まないパターンは名前にも照合する（.gitignore と同じ考え方）。
    `*` は `/` もまたぐので、`archive/**` や `*.tmp` のように書ける。
    """
    for pat in patterns:
        if fnmatchcase(relpath, pat):
            return True
        if "/" not in pat and fnmatchcase(name, pat):
            return True
    return False


def walk_tree(
    root: Path,
    exclude: Iterable[str] = (),
    on_progress: Callable[[int], None] | None = None,
) -> WalkResult:
    """ルート配下を lstat でたどり、相対パスごとの観測結果を返す。

    シンボリックリンクはたどらず、記録もしない。通常のファイルとフォルダだけを記録する。

    Args:
        root: ルートの絶対パス。
        exclude: 除外パターン（is_excluded を参照）。
        on_progress: フォルダを1つ読み終えるたびに、それまでの観測件数を渡して呼ぶ。
    """
    patterns = list(exclude)
    result = WalkResult()
    stack: list[tuple[str, str]] = [("", str(root))]
    while stack:
        rel_dir, abs_dir = stack.pop()
        try:
            it = os.scandir(abs_dir)
        except OSError as e:
            log.warning("フォルダを読めませんでした: %s (%s)", abs_dir, e.strerror)
            result.unreadable.append(rel_dir)
            continue
        with it:
            for de in it:
                rel = f"{rel_dir}/{de.name}" if rel_dir else de.name
                if patterns and is_excluded(rel, de.name, patterns):
                    continue
                try:
                    st = de.stat(follow_symlinks=False)
                except OSError as e:
                    log.warning("属性を読めませんでした: %s (%s)", de.path, e.strerror)
                    continue
                if stat.S_ISDIR(st.st_mode):
                    result.observed[rel] = Observation(True, 0, st.st_mtime_ns, st.st_ino)
                    stack.append((rel, de.path))
                elif stat.S_ISREG(st.st_mode):
                    result.observed[rel] = Observation(False, st.st_size, st.st_mtime_ns, st.st_ino)
        if on_progress is not None:
            on_progress(len(result.observed))
    return result


def check_root_available(root: Path) -> None:
    """ルートが存在し、空でないことを確かめる。満たさなければ RootUnavailableError。

    マウントが外れたルートは、存在しないか空のフォルダに見える。
    """
    if not root.is_dir():
        raise RootUnavailableError(f"ルートが見つかりません: {root}")
    try:
        with os.scandir(root) as it:
            empty = next(it, None) is None
    except OSError as e:
        raise RootUnavailableError(f"ルートを読めません: {root} ({e.strerror})") from e
    if empty:
        raise RootUnavailableError(f"ルートが空です（マウントが外れている可能性があります）: {root}")


def _under_any(relpath: str, dirs: Iterable[str]) -> bool:
    """relpath が、いずれかのフォルダの配下にあるかを返す（"" はルート全体）。"""
    for d in dirs:
        if d == "" or relpath.startswith(d + "/"):
            return True
    return False


def scan_root(
    conn: sqlite3.Connection,
    root_id: int,
    root: Path,
    safety: SafetyConfig,
    *,
    exclude: Iterable[str] = (),
    accept_missing: bool = False,
    on_progress: Callable[[int], None] | None = None,
) -> ScanResult:
    """ルートを走査し、カタログを差分で更新する。

    Args:
        conn: カタログへの接続。
        root_id: ルートの ID（catalog.ensure_root で得る）。
        root: ルートの絶対パス。
        safety: 大量消失の安全弁の設定。
        exclude: 除外パターン。
        accept_missing: True なら、大量消失でも保留せずに確定する。
        on_progress: 走査の進捗（観測件数）を受け取る関数。

    Raises:
        RootUnavailableError: ルートが見えないとき。カタログは変更しない。
    """
    check_root_available(root)
    started_at = utcnow()
    walk = walk_tree(root, exclude, on_progress)
    observed = walk.observed
    now = utcnow()
    res = ScanResult(seen=len(observed), unreadable=len(walk.unreadable))

    live = {
        row["relpath"]: row
        for row in conn.execute(
            "SELECT id, relpath, is_dir, size, mtime_ns, inode, missing_count"
            " FROM entries WHERE root_id = ? AND gone_at IS NULL",
            (root_id,),
        )
    }

    with conn:  # 1回の走査を1つのトランザクションで反映する
        # 1. 既知のパス: 属性が変わっていれば更新し、見えたことを記録する
        new_paths: list[str] = []
        touched: list[tuple[str, int]] = []
        for rel, ob in observed.items():
            row = live.get(rel)
            if row is None:
                new_paths.append(rel)
                continue
            if (bool(row["is_dir"]), row["size"], row["mtime_ns"], row["inode"]) != (
                ob.is_dir,
                ob.size,
                ob.mtime_ns,
                ob.inode,
            ):
                conn.execute(
                    "UPDATE entries SET is_dir = ?, size = ?, mtime_ns = ?, inode = ?,"
                    " last_seen = ?, missing_count = 0 WHERE id = ?",
                    (int(ob.is_dir), ob.size, ob.mtime_ns, ob.inode, now, row["id"]),
                )
                # フォルダの更新日時は中身が変わるたびに変わるので、ファイルだけを数える
                if not ob.is_dir:
                    res.changed += 1
            else:
                touched.append((now, row["id"]))
        conn.executemany(
            "UPDATE entries SET last_seen = ?, missing_count = 0 WHERE id = ?", touched
        )

        # 2. 見えなくなったパス（読めなかったフォルダの配下は除く）
        missing = {
            rel: row
            for rel, row in live.items()
            if rel not in observed and not _under_any(rel, walk.unreadable)
        }

        # 3. 移動の検出: 消えたパスと同じ inode の新しいパスを結び付ける
        by_inode: dict[tuple[int, bool], list[str]] = {}
        for rel, row in missing.items():
            by_inode.setdefault((row["inode"], bool(row["is_dir"])), []).append(rel)
        added: list[tuple] = []
        for rel in new_paths:
            ob = observed[rel]
            cands = by_inode.get((ob.inode, ob.is_dir))
            if cands is not None and len(cands) == 1:
                old_rel = cands.pop()
                row = missing.pop(old_rel)
                conn.execute(
                    "UPDATE entries SET relpath = ?, size = ?, mtime_ns = ?,"
                    " last_seen = ?, missing_count = 0 WHERE id = ?",
                    (rel, ob.size, ob.mtime_ns, now, row["id"]),
                )
                if not ob.is_dir:
                    res.moved += 1
                    if (row["size"], row["mtime_ns"]) != (ob.size, ob.mtime_ns):
                        res.changed += 1
                continue
            added.append(
                (root_id, rel, int(ob.is_dir), ob.size, ob.mtime_ns, ob.inode, now, now)
            )
            if not ob.is_dir:
                res.added += 1
        conn.executemany(
            "INSERT INTO entries(root_id, relpath, is_dir, size, mtime_ns, inode,"
            " first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            added,
        )

        # 4. 消えたものの確定（大量消失なら保留）
        missing_files = sum(1 for row in missing.values() if not row["is_dir"])
        live_files = sum(1 for row in live.values() if not row["is_dir"])
        threshold = max(safety.mass_missing_min, int(live_files * safety.mass_missing_ratio))
        mass = missing_files > threshold and not accept_missing
        if mass:
            log.warning(
                "一度に %d 件のファイルが見えなくなりました（しきい値 %d）。"
                "削除としての確定を保留します。次の走査でも見えなければ確定します",
                missing_files,
                threshold,
            )
        gone: list[tuple[str, int]] = []
        held: list[int] = []
        for row in missing.values():
            # 大量消失のときは、前回も見えなかったもの（2回続けて見えないもの）だけを確定する
            if mass and row["missing_count"] < 1:
                held.append(row["id"])
                if not row["is_dir"]:
                    res.held += 1
            else:
                gone.append((now, row["id"]))
                if not row["is_dir"]:
                    res.gone += 1
        conn.executemany("UPDATE entries SET gone_at = ? WHERE id = ?", gone)
        conn.executemany(
            "UPDATE entries SET missing_count = missing_count + 1 WHERE id = ?",
            [(i,) for i in held],
        )

        conn.execute(
            "INSERT INTO scans(root_id, started_at, finished_at, seen, added, changed, moved,"
            " gone, held, unreadable, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                root_id,
                started_at,
                utcnow(),
                res.seen,
                res.added,
                res.changed,
                res.moved,
                res.gone,
                res.held,
                res.unreadable,
                res.status,
            ),
        )
    return res
