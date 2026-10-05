"""整理プランの作成・実行・取り消し（要件 F-PL-1〜5・F-VS-3）。

- プランは、衛生レポートの候補を「隔離フォルダへ移す」操作として並べた TOML ファイル。
  1行が1操作なので、残したいものは行を消すだけで外せる。
- 実行（apply）は、既定では確認だけを行う。実際に動かすのは明示したときだけ（P2）。
- 完全削除はしない。ツリーの外の隔離フォルダへ改名で移すだけで、取り消し（undo）で元に戻せる（F-PL-3・4）。
- 実行直前に、対象がプラン作成時から変わっていないかを確かめ、変わっていれば飛ばす（F-PL-2）。
- 実行の前後に snapper でスナップショットを撮る（F-VS-3）。撮れなければ何も動かさずに止める。
- 実行した操作は、プランごとの実行記録（JSONL、追記のみ）に残す。取り消しはこの記録だけを頼りに行う。
- ツール自身が動かしたものは、カタログも直接更新する（F-PL-5）。
  走査に任せると、大量のファイルを隔離したときに大量消失の安全弁が働いてしまうため。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import tomllib

from tag_keeper import rules
from tag_keeper.catalog import utcnow
from tag_keeper.report import Report
from tag_keeper.scan import check_root_available, walk_tree

log = logging.getLogger(__name__)

# 操作の種類。今は隔離フォルダへの移動だけ
ACTION_QUARANTINE = "quarantine"


class PlanError(RuntimeError):
    """プランを読めない・実行できないときの例外（何も動かしていない）。"""


class SnapshotError(RuntimeError):
    """スナップショットを撮れなかったときの例外。"""


@dataclass
class PlanOp:
    """プランの1操作。

    inode・files・size・mtime_ns は、実行直前に「プラン作成時から変わっていないか」を確かめるための値。
    フォルダでは、配下のファイル数・合計サイズ・配下（自身を含む）の最新の更新日時を持つ。
    """

    action: str
    path: str  # ルートからの相対パス（区切りは /）
    is_dir: bool
    files: int
    size: int
    inode: int
    mtime_ns: int
    category: str = ""
    reason: str = ""


@dataclass
class Plan:
    """整理プラン（1つのルートに対する操作の並び）。"""

    id: str
    root: str  # ルート名（カタログの roots.name）
    root_path: Path
    created_at: str
    ops: list[PlanOp] = field(default_factory=list)


@dataclass(frozen=True)
class Fingerprint:
    """対象の現在の状態（変わっていないかの確認用）。"""

    inode: int
    files: int
    size: int
    mtime_ns: int


def fingerprint(path: Path) -> Fingerprint | None:
    """対象の現在の状態を返す。存在しなければ None。

    ファイルは自身の属性を、フォルダは配下をたどって集計した値を返す（シンボリックリンクはたどらない）。
    """
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    if not path.is_dir() or path.is_symlink():
        return Fingerprint(st.st_ino, 1, st.st_size, st.st_mtime_ns)
    walk = walk_tree(path)
    files = [ob for ob in walk.observed.values() if not ob.is_dir]
    newest = max([st.st_mtime_ns] + [ob.mtime_ns for ob in walk.observed.values()])
    return Fingerprint(st.st_ino, len(files), sum(ob.size for ob in files), newest)


def _slug(name: str) -> str:
    """ルート名を、プラン ID に使える短い文字列にする。"""
    s = re.sub(r"[^\w.-]+", "_", name).strip("_")
    return s[-40:] or "root"


def _path_key(relpath: str) -> list[str]:
    """パスの並べ替え用のキー（親が子より先に来るよう、区切りごとに比べる）。"""
    return relpath.split("/")


def build_plan(
    report: Report,
    root_name: str,
    root_path: Path,
    categories: Sequence[str] | None = None,
    *,
    include_review: bool = False,
) -> Plan:
    """衛生レポートの候補から、隔離のプランを作る。

    対象の状態は、カタログではなくディスクから読み直して記録する（実行時の確認と同じ方法でそろえるため）。

    Args:
        report: report.build_report の結果。
        root_name: ルート名。
        root_path: ルートの絶対パス。
        categories: 含めるカテゴリ（rules の表示名）。None ならすべて。
        include_review: 人の確認が要る候補（元と内容が異なる競合コピーなど）も含めるか。
    """
    created = datetime.now()
    plan = Plan(
        id=f"{created:%Y%m%d-%H%M%S}-{_slug(root_name)}",
        root=root_name,
        root_path=root_path,
        created_at=utcnow(),
    )
    wanted = set(categories) if categories is not None else None
    for f in report.findings:
        if wanted is not None and f.category not in wanted:
            continue
        if f.needs_review and not include_review:
            continue
        fp = fingerprint(root_path / f.relpath)
        if fp is None:
            log.warning("走査の後に見えなくなったため、プランに含めません: %s", f.relpath)
            continue
        plan.ops.append(
            PlanOp(
                action=ACTION_QUARANTINE,
                path=f.relpath,
                is_dir=f.is_dir,
                files=fp.files,
                size=fp.size,
                inode=fp.inode,
                mtime_ns=fp.mtime_ns,
                category=f.category,
                reason=f.reason,
            )
        )
    return plan


def _toml_str(s: str) -> str:
    """文字列を TOML の基本文字列（"..."）として書く。"""
    out = []
    for ch in s:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _human_size(n: float) -> str:
    """バイト数を読みやすい単位の文字列にする（プランのコメント用）。"""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}TB"


def write_plan(plan: Plan, path: Path) -> None:
    """プランを TOML で書き出す。1操作を1行にし、カテゴリごとに見出しのコメントを付ける。"""
    lines = [
        "# tag-keeper の整理プラン。",
        "# 残したいものは、その行を消してください。各行の inode 以降は、変わっていないかの確認用です。",
        f"# 確認: tag-keeper apply {path}",
        f"# 実行: tag-keeper apply {path} --yes",
        f"id = {_toml_str(plan.id)}",
        f"root = {_toml_str(plan.root)}",
        f"root_path = {_toml_str(str(plan.root_path))}",
        f"created_at = {_toml_str(plan.created_at)}",
        "",
        "ops = [",
    ]
    by_cat: dict[str, list[PlanOp]] = {}
    for op in plan.ops:
        by_cat.setdefault(op.category, []).append(op)
    order = [c for c in rules.CATEGORY_ORDER if c in by_cat] + [
        c for c in by_cat if c not in rules.CATEGORY_ORDER
    ]
    for cat in order:
        ops = sorted(by_cat[cat], key=lambda o: o.size, reverse=True)
        lines.append(f"  # {cat}（{len(ops):,} 件、{_human_size(sum(o.size for o in ops))}）")
        for op in ops:
            lines.append(
                f"  {{ action = {_toml_str(op.action)}, path = {_toml_str(op.path)},"
                f" category = {_toml_str(op.category)}, reason = {_toml_str(op.reason)},"
                f" is_dir = {'true' if op.is_dir else 'false'}, files = {op.files}, size = {op.size},"
                f" inode = {op.inode}, mtime_ns = {op.mtime_ns} }},"
            )
    lines.append("]")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_plan(path: Path) -> Plan:
    """TOML のプランを読み込む。不正なら PlanError。"""
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise PlanError(f"プランを読めません: {path} ({e})") from e
    try:
        plan = Plan(
            id=str(data["id"]),
            root=str(data["root"]),
            root_path=Path(str(data["root_path"])),
            created_at=str(data["created_at"]),
        )
        for i, item in enumerate(data.get("ops", [])):
            if item["action"] != ACTION_QUARANTINE:
                raise PlanError(f"{path}: ops[{i}] の action が不明です: {item['action']}")
            plan.ops.append(
                PlanOp(
                    action=str(item["action"]),
                    path=str(item["path"]),
                    is_dir=bool(item["is_dir"]),
                    files=int(item["files"]),
                    size=int(item["size"]),
                    inode=int(item["inode"]),
                    mtime_ns=int(item["mtime_ns"]),
                    category=str(item.get("category", "")),
                    reason=str(item.get("reason", "")),
                )
            )
    except KeyError as e:
        raise PlanError(f"{path}: 必要な項目がありません: {e}") from e
    if not re.fullmatch(r"[\w.-]+", plan.id):
        raise PlanError(f"{path}: プラン ID に使えない文字があります: {plan.id}")
    for op in plan.ops:
        parts = op.path.split("/")
        if op.path.startswith("/") or any(p in ("", ".", "..") for p in parts):
            raise PlanError(f"{path}: ルートの外を指すパスは扱えません: {op.path}")
    # 親を子より先に処理する（子を先に動かすと、親の隔離先にフォルダができて親を動かせなくなる）
    plan.ops.sort(key=lambda o: _path_key(o.path))
    return plan


def check_op(plan: Plan, op: PlanOp) -> str | None:
    """操作を今実行できるかを確かめる。できなければ理由を返す（F-PL-2）。"""
    fp = fingerprint(plan.root_path / op.path)
    if fp is None:
        return "見つからない"
    if fp.inode != op.inode:
        return "別のものに置き換わった"
    if (fp.files, fp.size, fp.mtime_ns) != (op.files, op.size, op.mtime_ns):
        return "プランの作成後に変更された"
    return None


# --- スナップショット ---


class SnapshotTaker(Protocol):
    """実行の前後にスナップショットを撮るもの。"""

    def pre(self, description: str) -> int:
        """実行前のスナップショットを撮り、その番号を返す。"""
        ...

    def post(self, pre_number: int, description: str) -> int:
        """実行後のスナップショットを撮り、その番号を返す。"""
        ...


@dataclass
class Snapper:
    """snapper の pre / post スナップショット。

    撮影には、snapper の設定の ALLOW_USERS に実行ユーザーが入っている必要がある。
    自動削除は number アルゴリズム（設定の NUMBER_LIMIT）に任せる。
    """

    config: str
    command: str = "snapper"

    def _create(self, args: list[str]) -> int:
        cmd = [self.command, "-c", self.config, "create", "--print-number"]
        cmd += ["--cleanup-algorithm", "number", *args]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except OSError as e:
            raise SnapshotError(f"snapper を実行できません: {e}") from e
        if res.returncode != 0:
            raise SnapshotError(f"snapper が失敗しました: {res.stderr.strip() or res.stdout.strip()}")
        try:
            return int(res.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as e:
            raise SnapshotError(f"snapper の出力から番号を読めません: {res.stdout!r}") from e

    def pre(self, description: str) -> int:
        """実行前のスナップショットを撮る。"""
        return self._create(["--type", "pre", "--description", description])

    def post(self, pre_number: int, description: str) -> int:
        """実行後のスナップショットを撮る（pre と対にする）。"""
        return self._create(
            ["--type", "post", "--pre-number", str(pre_number), "--description", description]
        )


# --- 実行記録 ---


def journal_path(journal_dir: Path, plan_id: str) -> Path:
    """プランの実行記録（JSONL）のパスを返す。"""
    return journal_dir / f"{plan_id}.jsonl"


def _append(journal: Path, record: dict[str, Any]) -> None:
    """実行記録に1件追記し、すぐにディスクへ書き出す。"""
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": utcnow(), **record}, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_journal(journal: Path) -> Iterator[dict[str, Any]]:
    """実行記録を先頭から読む。"""
    with journal.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


# --- 実行と取り消し ---


@dataclass
class OpOutcome:
    """1操作の結果。"""

    path: str
    is_dir: bool
    files: int
    size: int
    done: bool
    detail: str = ""


@dataclass
class RunResult:
    """実行（または取り消し）の結果。"""

    plan_id: str
    outcomes: list[OpOutcome] = field(default_factory=list)
    pre_snapshot: int | None = None
    post_snapshot: int | None = None

    @property
    def done(self) -> list[OpOutcome]:
        """実行できた操作。"""
        return [o for o in self.outcomes if o.done]

    @property
    def skipped(self) -> list[OpOutcome]:
        """飛ばした操作。"""
        return [o for o in self.outcomes if not o.done]


def _subtree_sql(relpath: str) -> tuple[str, tuple[Any, ...]]:
    """relpath 自身とその配下に一致する WHERE 句の断片と引数を返す。

    LIKE は % や _ を含む名前で誤るので、先頭の部分文字列で比べる。
    """
    return "(relpath = ? OR substr(relpath, 1, ?) = ?)", (relpath, len(relpath) + 1, relpath + "/")


def _root_id(conn: sqlite3.Connection, name: str) -> int:
    """ルート名からカタログの ID を引く。無ければ PlanError。"""
    row = conn.execute("SELECT id FROM roots WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise PlanError(f"カタログにルートがありません: {name}")
    return int(row["id"])


def _check_same_filesystem(root_path: Path, quarantine: Path) -> None:
    """隔離先がルートと同じファイルシステムにあるかを確かめる（改名で移せるか）。"""
    quarantine.mkdir(parents=True, exist_ok=True)
    if root_path.stat().st_dev != quarantine.stat().st_dev:
        raise PlanError(
            f"隔離先がルートと別のファイルシステムにあります: {quarantine}"
            "（コピーを伴う移動にはまだ対応していません。設定の [plan] quarantine_dir を変えてください）"
        )


def _take_pre(snapshot: SnapshotTaker | None, description: str) -> int | None:
    """実行前のスナップショットを撮る。撮れなければ SnapshotError を投げて実行を止める。"""
    if snapshot is None:
        log.warning("スナップショットを撮らずに実行します")
        return None
    number = snapshot.pre(description)
    log.info("実行前のスナップショットを撮りました: %d", number)
    return number


def _take_post(snapshot: SnapshotTaker | None, pre: int | None, description: str) -> int | None:
    """実行後のスナップショットを撮る。失敗しても実行済みの操作はそのままなので、警告に留める。"""
    if snapshot is None or pre is None:
        return None
    try:
        number = snapshot.post(pre, description)
    except SnapshotError as e:
        log.warning("実行後のスナップショットを撮れませんでした: %s", e)
        return None
    log.info("実行後のスナップショットを撮りました: %d", number)
    return number


def apply_plan(
    conn: sqlite3.Connection,
    plan: Plan,
    *,
    quarantine_dir: Path,
    journal_dir: Path,
    snapshot: SnapshotTaker | None,
    on_op: Callable[[OpOutcome], None] | None = None,
) -> RunResult:
    """プランを実行し、対象を隔離フォルダへ移す。

    Args:
        conn: カタログへの接続。
        plan: 実行するプラン。
        quarantine_dir: 隔離先の親フォルダ。<quarantine_dir>/<プラン ID>/<相対パス> へ移す。
        journal_dir: 実行記録を置くフォルダ。
        snapshot: 前後のスナップショットを撮るもの。None なら撮らない。
        on_op: 操作を1つ終えるたびに呼ぶ関数。

    Raises:
        PlanError: ルートが見えない・隔離先が別のファイルシステム、など。何も動かしていない。
        SnapshotError: 実行前のスナップショットを撮れなかった。何も動かしていない。
    """
    try:
        check_root_available(plan.root_path)
    except RuntimeError as e:
        raise PlanError(str(e)) from e
    root_id = _root_id(conn, plan.root)
    dest_root = quarantine_dir / plan.id
    _check_same_filesystem(plan.root_path, quarantine_dir)
    journal = journal_path(journal_dir, plan.id)

    result = RunResult(plan.id)
    result.pre_snapshot = _take_pre(snapshot, f"tag-keeper apply {plan.id}")
    # この実行で消えたことにした行には同じ日時を記録し、取り消しのときの目印にする
    gone_at = utcnow()
    _append(
        journal,
        {
            "event": "apply",
            "root": plan.root,
            "root_path": str(plan.root_path),
            "quarantine": str(dest_root),
            "pre_snapshot": result.pre_snapshot,
        },
    )

    for op in plan.ops:
        src = plan.root_path / op.path
        dst = dest_root / op.path
        problem = check_op(plan, op)
        if problem is None and os.path.lexists(dst):
            problem = "隔離先に同じ名前のものがある"
        outcome = OpOutcome(op.path, op.is_dir, op.files, op.size, problem is None, problem or "")
        if problem is None:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.rename(src, dst)
            except OSError as e:
                outcome.done = False
                outcome.detail = f"移動できなかった（{e.strerror}）"
        if outcome.done:
            _append(
                journal,
                {
                    "event": "quarantine",
                    "path": op.path,
                    "is_dir": op.is_dir,
                    "files": op.files,
                    "size": op.size,
                    "src": str(src),
                    "dst": str(dst),
                    "gone_at": gone_at,
                },
            )
            where, args = _subtree_sql(op.path)
            with conn:
                conn.execute(
                    f"UPDATE entries SET gone_at = ? WHERE root_id = ? AND gone_at IS NULL AND {where}",
                    (gone_at, root_id, *args),
                )
        else:
            log.warning("飛ばしました: %s（%s）", op.path, outcome.detail)
            _append(journal, {"event": "skip", "path": op.path, "detail": outcome.detail})
        result.outcomes.append(outcome)
        if on_op is not None:
            on_op(outcome)

    result.post_snapshot = _take_post(snapshot, result.pre_snapshot, f"tag-keeper apply {plan.id}")
    _append(
        journal,
        {
            "event": "finish",
            "done": len(result.done),
            "skipped": len(result.skipped),
            "post_snapshot": result.post_snapshot,
        },
    )
    return result


@dataclass
class Restorable:
    """取り消しで元に戻す1件（実行記録から復元したもの）。"""

    root: str
    path: str
    is_dir: bool
    files: int
    size: int
    src: Path
    dst: Path
    gone_at: str
    quarantine: Path  # このプランの隔離先（<quarantine_dir>/<プラン ID>）


def pending_restores(journal: Path) -> list[Restorable]:
    """実行記録から、まだ元に戻していない隔離の操作を、実行と逆の順で返す。"""
    if not journal.exists():
        raise PlanError(f"実行記録がありません: {journal}")
    root = ""
    quarantine = Path()
    moved: dict[str, Restorable] = {}
    for rec in read_journal(journal):
        event = rec.get("event")
        if event == "apply":
            root = rec["root"]
            quarantine = Path(rec["quarantine"])
        elif event == "quarantine":
            moved[rec["path"]] = Restorable(
                root=root,
                path=rec["path"],
                is_dir=rec["is_dir"],
                files=rec["files"],
                size=rec["size"],
                src=Path(rec["src"]),
                dst=Path(rec["dst"]),
                gone_at=rec["gone_at"],
                quarantine=quarantine,
            )
        elif event == "restore":
            moved.pop(rec["path"], None)
    return list(reversed(moved.values()))


def _remove_empty_parents(start: Path, top: Path) -> None:
    """隔離先に残った空のフォルダを、start から top（自身を含む）まで上へ順に消す。"""
    d = start
    while d == top or top in d.parents:
        try:
            d.rmdir()
        except OSError:
            return  # 空でない（他にも隔離したものがある）か、既に無い
        d = d.parent


def undo_plan(
    conn: sqlite3.Connection,
    plan_id: str,
    *,
    journal_dir: Path,
    snapshot: SnapshotTaker | None,
    on_op: Callable[[OpOutcome], None] | None = None,
) -> RunResult:
    """実行記録をもとに、隔離したものを元の場所へ戻す（F-PL-4）。

    元の場所に既に別のものがあれば、上書きせずに飛ばす。

    Raises:
        PlanError: 実行記録が無い・戻すものが無い。
        SnapshotError: 実行前のスナップショットを撮れなかった。何も動かしていない。
    """
    journal = journal_path(journal_dir, plan_id)
    items = pending_restores(journal)
    if not items:
        raise PlanError(f"元に戻すものがありません: {plan_id}")
    root_id = _root_id(conn, items[0].root)

    result = RunResult(plan_id)
    result.pre_snapshot = _take_pre(snapshot, f"tag-keeper undo {plan_id}")
    _append(journal, {"event": "undo", "pre_snapshot": result.pre_snapshot})
    for it in items:
        outcome = OpOutcome(it.path, it.is_dir, it.files, it.size, True)
        if not os.path.lexists(it.dst):
            outcome.done, outcome.detail = False, "隔離先に見つからない"
        elif os.path.lexists(it.src):
            outcome.done, outcome.detail = False, "元の場所に別のものがある"
        else:
            try:
                it.src.parent.mkdir(parents=True, exist_ok=True)
                os.rename(it.dst, it.src)
            except OSError as e:
                outcome.done, outcome.detail = False, f"移動できなかった（{e.strerror}）"
        if outcome.done:
            _remove_empty_parents(it.dst.parent, it.quarantine)
            _append(journal, {"event": "restore", "path": it.path})
            where, args = _subtree_sql(it.path)
            now = utcnow()
            with conn:
                # 元の場所が空いていたので、そこに残っている「存在中」の行は古い記録。先に消えたことにする
                conn.execute(
                    f"UPDATE entries SET gone_at = ? WHERE root_id = ? AND gone_at IS NULL AND {where}",
                    (now, root_id, *args),
                )
                # 隔離のときに消えたことにした行を生き返らせる（ハッシュなどの記録を引き継ぐ）
                conn.execute(
                    "UPDATE entries SET gone_at = NULL, missing_count = 0, last_seen = ?"
                    f" WHERE root_id = ? AND gone_at = ? AND {where}",
                    (now, root_id, it.gone_at, *args),
                )
        else:
            log.warning("戻せませんでした: %s（%s）", it.path, outcome.detail)
            _append(journal, {"event": "restore-skip", "path": it.path, "detail": outcome.detail})
        result.outcomes.append(outcome)
        if on_op is not None:
            on_op(outcome)

    result.post_snapshot = _take_post(snapshot, result.pre_snapshot, f"tag-keeper undo {plan_id}")
    _append(
        journal,
        {
            "event": "undo-finish",
            "done": len(result.done),
            "skipped": len(result.skipped),
            "post_snapshot": result.post_snapshot,
        },
    )
    return result
