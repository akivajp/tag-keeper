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
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fnmatch import fnmatchcase
from pathlib import Path

from tag_keeper.catalog import record_move, utcnow
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
    # 名前を UTF-8 として読めずに飛ばしたものの数
    undecodable: int = 0


def _utf8_ok(name: str) -> bool:
    """名前が UTF-8 として読めたか（読めないバイトは os が surrogateescape で文字にしている）。"""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


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
    # 検出した移動（旧パス, 新パス）。フォルダごと動いたものは、そのフォルダだけを含む
    moves: list[tuple[str, str]] = field(default_factory=list)

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
                if not _utf8_ok(de.name):
                    # 名前を UTF-8 として読めない（CP932 の名前のまま展開した zip の中身など）。
                    # カタログ（SQLite）にも画面にも入れられないので、飛ばして知らせる（走査全体は止めない）
                    log.warning("名前を UTF-8 として読めないので飛ばします: %s", os.fsencode(de.path).decode("utf-8", "backslashreplace"))
                    result.undecodable += 1
                    continue
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
        moved: list[tuple[int, str, str, bool]] = []  # (行 ID, 旧パス, 新パス, フォルダか)
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
                moved.append((row["id"], old_rel, rel, ob.is_dir))
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

        # 移動の履歴: 親フォルダごと動いたもの（親の旧パス→新パスも移動に含まれるもの）は省く
        pairs = {(old, new) for _id, old, new, _d in moved}
        for entry_id, old, new, is_dir in moved:
            if "/" in old and "/" in new and (old.rsplit("/", 1)[0], new.rsplit("/", 1)[0]) in pairs:
                if old.rsplit("/", 1)[1] == new.rsplit("/", 1)[1]:
                    continue
            record_move(conn, root_id, entry_id, old, new, is_dir, "scan", now)
            res.moves.append((old, new))

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
        conn.executemany("UPDATE entries SET gone_at = ?, gone_by = 'scan' WHERE id = ?", gone)
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


@dataclass
class RelinkResult:
    """内容ハッシュで移動を結び付けた結果（要件 F-ID-1 の2つ目の手がかり）。"""

    # 結び付けたファイルの数（同じパスに戻ってきたものも含む）
    files: int = 0
    # 同じ内容の候補が複数あって、結び付けなかったファイルの数（F-ID-2: 自動では決めない）
    ambiguous: int = 0
    # 記録した移動（旧パス, 新パス）。フォルダごと動いたと推定したものは、そのフォルダだけを含む。タグの追従に使う
    moves: list[tuple[str, str]] = field(default_factory=list)


# 一度に IN (...) で問い合わせるハッシュの数（SQLite の変数の数の上限より十分小さく）
_IN_CHUNK = 500


def _basename(relpath: str) -> str:
    return relpath.rsplit("/", 1)[-1]


def _pair_by_hash(
    news: list[sqlite3.Row], olds: list[sqlite3.Row]
) -> tuple[list[tuple[sqlite3.Row, sqlite3.Row]], int]:
    """同じ内容の「新しく現れたファイル」と「見えなくなったファイル」を、一対一に決まるものだけ組にする。

    相手になれるのは、新しい側が現れる前に見えなくなったもの（old.last_seen < new.first_seen）だけ。
    候補が複数あるときは、名前（パスの最後）が同じものが互いに1つずつなら組にする。

    Returns:
        （組の一覧, 候補はあったが決まらなかった新しいファイルの数）
    """
    def valid(n: sqlite3.Row, o: sqlite3.Row) -> bool:
        return str(o["last_seen"]) < str(n["first_seen"])

    pairs: list[tuple[sqlite3.Row, sqlite3.Row]] = []
    if len(news) == 1 and len(olds) == 1:
        if valid(news[0], olds[0]):
            return [(news[0], olds[0])], 0
        return [], 0
    used: set[int] = set()
    undecided = 0
    for n in news:
        cands = [o for o in olds if valid(n, o)]
        if not cands:
            continue
        same_name = [o for o in cands if _basename(o["relpath"]) == _basename(n["relpath"])]
        rivals = [m for m in news if _basename(m["relpath"]) == _basename(n["relpath"])]
        if len(same_name) == 1 and len(rivals) == 1 and same_name[0]["id"] not in used:
            pairs.append((n, same_name[0]))
            used.add(int(same_name[0]["id"]))
        else:
            undecided += 1
    return pairs, undecided


def _under_dir(relpath: str, base: str) -> bool:
    return relpath.startswith(base + "/")


def _infer_dir_moves(
    conn: sqlite3.Connection, root_id: int, file_moves: list[tuple[str, str]], cutoff: str
) -> list[tuple[str, str, int, str]]:
    """ファイルの移動から、フォルダごとの移動を推定する。

    旧パスと新パスの後ろの部分（名前）がそろっている親フォルダの組（a/x/f → b/x/f なら a/x → b/x と a → b）を候補にし、
    次のすべてを満たすものだけを採る。

    - 旧フォルダが今は見えない（消えたか、走査で見えなくなった）
    - 新フォルダが、旧フォルダが最後に見えた後に新しく現れた
    - 旧フォルダの配下から動いたファイルが、すべて新フォルダの同じ相対パスへ動いている

    Returns:
        採ったフォルダの移動（旧, 新, 新フォルダの行 ID, 新フォルダを初めて見た日時）。浅い順で、他の移動に含まれるものは除く。
    """
    cands: dict[str, set[str]] = defaultdict(set)
    for old, new in file_moves:
        o, n = old.split("/"), new.split("/")
        i = 1
        while i < len(o) and i < len(n) and o[-i] == n[-i]:
            cands["/".join(o[:-i])].add("/".join(n[:-i]))
            i += 1
    chosen: list[tuple[str, str, int, str]] = []
    for od in sorted(cands, key=lambda d: (d.count("/"), d)):
        if len(cands[od]) != 1:
            continue
        nd = next(iter(cands[od]))
        if od == nd or _under_dir(nd, od) or _under_dir(od, nd):
            continue
        # すでに採った浅い移動に含まれるもの（a → b を採ったときの a/x → b/x）は記録しない
        if any(_under_dir(od, c_od) for c_od, _n, _i, _f in chosen):
            continue
        # 旧フォルダの配下から動いたファイルが、すべて新フォルダの同じ相対パスへ動いていること
        if any(_under_dir(old, od) and new != nd + old[len(od):] for old, new in file_moves):
            continue
        live_old = conn.execute(
            "SELECT 1 FROM entries WHERE root_id = ? AND relpath = ? AND is_dir = 1"
            " AND gone_at IS NULL AND missing_count = 0",
            (root_id, od),
        ).fetchone()
        if live_old is not None:
            continue
        old_row = conn.execute(
            "SELECT last_seen FROM entries WHERE root_id = ? AND relpath = ? AND is_dir = 1 ORDER BY id DESC LIMIT 1",
            (root_id, od),
        ).fetchone()
        new_row = conn.execute(
            "SELECT id, first_seen FROM entries WHERE root_id = ? AND relpath = ? AND is_dir = 1 AND gone_at IS NULL",
            (root_id, nd),
        ).fetchone()
        if old_row is None or new_row is None:
            continue
        if not (str(new_row["first_seen"]) >= cutoff and str(old_row["last_seen"]) < str(new_row["first_seen"])):
            continue
        chosen.append((od, nd, int(new_row["id"]), str(new_row["first_seen"])))
    return chosen


# 結び付けに使う、新しく現れたファイルの列と、見えなくなったファイルの列
_NEW_COLS = "id, relpath, size, mtime_ns, inode, sha256, hashed_size, hashed_mtime_ns, first_seen, last_seen"
_OLD_COLS = "id, relpath, size, sha256, last_seen, gone_at"


def _merge_rows(conn: sqlite3.Connection, n: sqlite3.Row, o: sqlite3.Row) -> None:
    """新しく足された行を消し、消えた側の行を新しいパスで生き返らせる（同じ行が同じアイテムを指し続ける）。

    呼び出し側のトランザクションの中で呼ぶ。
    """
    conn.execute("DELETE FROM entries WHERE id = ?", (n["id"],))
    conn.execute(
        "UPDATE entries SET relpath = ?, size = ?, mtime_ns = ?, inode = ?, hashed_size = ?,"
        " hashed_mtime_ns = ?, last_seen = ?, missing_count = 0, gone_at = NULL, gone_by = NULL WHERE id = ?",
        (n["relpath"], n["size"], n["mtime_ns"], n["inode"], n["hashed_size"], n["hashed_mtime_ns"], n["last_seen"], o["id"]),
    )
    # 新しい行を指していた移動の記録（現れた後に inode で追った改名など）は、生き返らせた行に付け替える
    conn.execute("UPDATE moves SET entry_id = ? WHERE entry_id = ?", (o["id"], n["id"]))


def relink_candidates(
    conn: sqlite3.Connection,
    root_id: int,
    *,
    days: int = 30,
    skipped: Iterable[tuple[str, str]] = (),
    now: str | None = None,
    limit: int = 200,
) -> list[dict[str, object]]:
    """内容ハッシュでは自動で結び付けられなかった（候補が複数ある）新しいファイルと、その候補（F-ID-2 の確認待ち）。

    relink_by_hash の後に残っているものだけが出る（一対一に決まるものは、もう結び付けてある）。

    Args:
        conn: カタログへの接続。
        root_id: ルートの ID。
        days: 何日前までに見えなくなったものを候補にするか。
        skipped: 利用者が「どれとも結び付けない」と決めた（新しいパス, 内容ハッシュ）。
        now: 現在の日時（テスト用）。
        limit: 返す件数の上限。

    Returns:
        {"id", "path", "size", "first_seen", "candidates": [{"id", "path", "last_seen", "gone_at"}]} の一覧（パスの順）。
    """
    if days <= 0:
        return []
    now = now or utcnow()
    cutoff = (datetime.fromisoformat(now) - timedelta(days=days)).isoformat(timespec="seconds")
    skip = set(skipped)
    news = [
        r for r in conn.execute(
            f"SELECT {_NEW_COLS} FROM entries WHERE root_id = ? AND gone_at IS NULL AND is_dir = 0 AND missing_count = 0"
            " AND sha256 IS NOT NULL AND hashed_size IS size AND hashed_mtime_ns IS mtime_ns AND first_seen >= ? ORDER BY relpath",
            (root_id, cutoff),
        )
        if (unicodedata.normalize("NFC", str(r["relpath"])), str(r["sha256"])) not in skip
    ]
    # 候補（見えなくなったファイル）は、内容ハッシュでまとめて引く（新しいファイルごとに問い合わせない）
    by_key: dict[tuple[str, int], list[sqlite3.Row]] = defaultdict(list)
    shas = sorted({str(n["sha256"]) for n in news})
    for i in range(0, len(shas), _IN_CHUNK):
        chunk = shas[i : i + _IN_CHUNK]
        marks = ",".join("?" * len(chunk))
        for o in conn.execute(
            f"SELECT {_OLD_COLS} FROM entries WHERE root_id = ? AND is_dir = 0 AND sha256 IN ({marks})"
            " AND ((gone_at IS NOT NULL AND gone_at >= ? AND (gone_by IS NULL OR gone_by = 'scan'))"
            "      OR (gone_at IS NULL AND missing_count > 0)) ORDER BY relpath",
            (root_id, *chunk, cutoff),
        ):
            by_key[(str(o["sha256"]), int(o["size"]))].append(o)
    out: list[dict[str, object]] = []
    for n in news:
        olds = [o for o in by_key.get((str(n["sha256"]), int(n["size"])), []) if str(o["last_seen"]) < str(n["first_seen"])]
        if not olds:
            continue
        out.append({
            "id": int(n["id"]), "path": str(n["relpath"]), "size": int(n["size"]), "first_seen": str(n["first_seen"]),
            "candidates": [{"id": int(o["id"]), "path": str(o["relpath"]), "last_seen": str(o["last_seen"]), "gone_at": o["gone_at"]} for o in olds],
        })
        if len(out) >= limit:
            break
    return out


def link_move(conn: sqlite3.Connection, root_id: int, new_id: int, old_id: int) -> tuple[str, str]:
    """利用者が選んだ「消えたファイル → 新しいファイル」を、移動として結び付ける（F-ID-2）。

    消えた側の行を新しいパスで生き返らせ、移動の履歴（source = "manual"）に残す。
    どちらの行も条件（新しい側は存在中、消えた側は消えたか保留中、内容ハッシュと大きさが同じ）を満たさなければ ValueError。

    Returns:
        （旧パス, 新パス）。タグの追従に使う。
    """
    n = conn.execute(
        f"SELECT {_NEW_COLS}, gone_at FROM entries WHERE id = ? AND root_id = ? AND is_dir = 0", (new_id, root_id)
    ).fetchone()
    o = conn.execute(
        f"SELECT {_OLD_COLS}, missing_count, gone_by FROM entries WHERE id = ? AND root_id = ? AND is_dir = 0", (old_id, root_id)
    ).fetchone()
    if n is None or o is None:
        raise ValueError("カタログに見つかりません")
    if n["gone_at"] is not None:
        raise ValueError(f"新しい側のファイルが今はありません: {n['relpath']}")
    if not (o["gone_at"] is not None or int(o["missing_count"]) > 0) or o["gone_by"] == "plan":
        raise ValueError(f"消えたファイルではありません（またはツールが隔離したもの）: {o['relpath']}")
    if (n["sha256"], n["size"]) != (o["sha256"], o["size"]) or not n["sha256"]:
        raise ValueError("内容が同じではありません")
    old_rel, new_rel = str(o["relpath"]), str(n["relpath"])
    with conn:
        _merge_rows(conn, n, o)
        if old_rel != new_rel:
            record_move(conn, root_id, int(o["id"]), old_rel, new_rel, False, "manual")
    log.info("移動として結び付けました: %s → %s", old_rel, new_rel)
    return old_rel, new_rel


def relink_by_hash(
    conn: sqlite3.Connection,
    root_id: int,
    *,
    days: int = 30,
    now: str | None = None,
) -> RelinkResult:
    """inode では結び付かなかった移動を、内容ハッシュで結び付ける（要件 F-ID-1・F-ID-2）。

    クラウド側での移動が同期で「削除と新規」として届いたとき（rclone bisync など）や、
    ファイルシステムをまたいだ移動では inode が変わり、走査は「消えたものと、新しいもの」としか見られない。
    そこで、新しく現れたファイルと、その前に見えなくなったファイルとを内容ハッシュと大きさで突き合わせ、
    一対一に決まるものだけを移動とみなす。曖昧なものは結び付けない。
    ツールが隔離したもの（gone_by = 'plan'）は、取り消しで元に戻すので相手にしない。

    結び付けたものは、消えた側の行を新しいパスで生き返らせ（初めて見た日時などの記録を引き継ぐ）、
    新しく足された行を消す。移動の履歴（moves 表、source = "hash"）にも残す。
    配下のファイルがそろって動いたフォルダは、フォルダ1件の移動として記録する（タグの追従と版の履歴のため）。

    ハッシュは hash_root で計算した後に呼ぶ（ハッシュの無いファイルは相手にできない）。

    Args:
        conn: カタログへの接続。
        root_id: ルートの ID。
        days: 何日前までに見えなくなったものを相手にするか。0 以下なら何もしない。
        now: 現在の日時（UTC の ISO 8601。テスト用）。

    Returns:
        結び付けた件数と、タグの追従に使う移動の一覧。
    """
    res = RelinkResult()
    if days <= 0:
        return res
    now = now or utcnow()
    cutoff = (datetime.fromisoformat(now) - timedelta(days=days)).isoformat(timespec="seconds")

    # 新しく現れたファイル（ハッシュが今の中身のもの）
    news_rows = conn.execute(
        "SELECT id, relpath, size, mtime_ns, inode, sha256, hashed_size, hashed_mtime_ns, first_seen, last_seen"
        " FROM entries WHERE root_id = ? AND gone_at IS NULL AND is_dir = 0 AND missing_count = 0"
        " AND sha256 IS NOT NULL AND hashed_size IS size AND hashed_mtime_ns IS mtime_ns AND first_seen >= ?",
        (root_id, cutoff),
    ).fetchall()
    if not news_rows:
        return res
    news_by: dict[tuple[str, int], list[sqlite3.Row]] = defaultdict(list)
    for r in news_rows:
        news_by[(str(r["sha256"]), int(r["size"]))].append(r)

    # 見えなくなったファイル（相手の候補）: 期間内に走査で消えたもの、または見えなくなって確定を保留しているもの
    olds_by: dict[tuple[str, int], list[sqlite3.Row]] = defaultdict(list)
    shas = sorted({k[0] for k in news_by})
    for i in range(0, len(shas), _IN_CHUNK):
        chunk = shas[i : i + _IN_CHUNK]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
            "SELECT id, relpath, size, sha256, last_seen FROM entries"
            f" WHERE root_id = ? AND is_dir = 0 AND sha256 IN ({marks})"
            " AND ((gone_at IS NOT NULL AND gone_at >= ? AND (gone_by IS NULL OR gone_by = 'scan'))"
            "      OR (gone_at IS NULL AND missing_count > 0))",
            (root_id, *chunk, cutoff),
        ):
            olds_by[(str(r["sha256"]), int(r["size"]))].append(r)

    pairs: list[tuple[sqlite3.Row, sqlite3.Row]] = []
    # 1巡目で一対一に決まらなかった組（新しいもの, 消えたもの）。2巡目で、推定したフォルダの移動から決める
    unsettled: list[tuple[list[sqlite3.Row], list[sqlite3.Row]]] = []
    for key, news in news_by.items():
        olds = olds_by.get(key)
        if not olds:
            continue
        got, undecided = _pair_by_hash(news, olds)
        pairs.extend(got)
        res.ambiguous += undecided
        if undecided:
            got_n = {int(n["id"]) for n, _o in got}
            got_o = {int(o["id"]) for _n, o in got}
            unsettled.append(([n for n in news if int(n["id"]) not in got_n], [o for o in olds if int(o["id"]) not in got_o]))
    if not pairs:
        if res.ambiguous:
            log.info("同じ内容の候補が複数あるため、%d 件の移動を結び付けませんでした", res.ambiguous)
        return res

    file_moves = [(str(o["relpath"]), str(n["relpath"])) for n, o in pairs if o["relpath"] != n["relpath"]]
    dir_moves = _infer_dir_moves(conn, root_id, file_moves, cutoff)

    # 2巡目: 同じ内容のものが複数あっても（フォルダの中の同じファイルの写しなど）、推定したフォルダの移動の中なら、
    # 移動元のフォルダの同じ相対パスにあったものが相手と決まる
    if dir_moves and unsettled:
        res.ambiguous = 0
        deepest_first = sorted(dir_moves, key=lambda m: -len(m[1]))
        for news, olds in unsettled:
            by_path = {str(o["relpath"]): o for o in olds}
            used: set[int] = set()
            for n in news:
                rel = str(n["relpath"])
                mapped = next((od + rel[len(nd):] for od, nd, _i, _f in deepest_first if _under_dir(rel, nd)), None)
                o = by_path.get(mapped) if mapped is not None else None
                if o is not None and int(o["id"]) not in used and str(o["last_seen"]) < str(n["first_seen"]):
                    pairs.append((n, o))
                    used.add(int(o["id"]))
                elif any(str(c["last_seen"]) < str(n["first_seen"]) for c in olds):
                    res.ambiguous += 1

    def covered(old: str, new: str) -> bool:
        return any(_under_dir(old, od) and new == nd + old[len(od):] for od, nd, _i, _f in dir_moves)

    with conn:
        for n, o in pairs:
            _merge_rows(conn, n, o)
            res.files += 1
            old_rel, new_rel = str(o["relpath"]), str(n["relpath"])
            if old_rel != new_rel and not covered(old_rel, new_rel):
                record_move(conn, root_id, int(o["id"]), old_rel, new_rel, False, "hash", str(n["first_seen"]))
                res.moves.append((old_rel, new_rel))
        for od, nd, new_id, first_seen in dir_moves:
            record_move(conn, root_id, new_id, od, nd, True, "hash", first_seen)
            res.moves.append((od, nd))
    log.info(
        "内容ハッシュで %d 件の移動を結び付けました（フォルダごと %d 件）%s",
        res.files,
        len(dir_moves),
        f"。候補が複数のため {res.ambiguous} 件は結び付けていません" if res.ambiguous else "",
    )
    return res
