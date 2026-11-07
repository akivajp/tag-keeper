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
from tag_keeper.catalog import record_move, utcnow
from tag_keeper.report import Report
from tag_keeper.scan import check_root_available, walk_tree
from tag_keeper.syncguard import SyncError, SyncGuard, count_children
from tag_keeper.tags import TagStore

log = logging.getLogger(__name__)

# 操作の種類。隔離フォルダへの移動と、ツリーの中での移動・改名
ACTION_QUARANTINE = "quarantine"
ACTION_MOVE = "move"
ACTIONS = (ACTION_QUARANTINE, ACTION_MOVE)
# 移動・改名の操作のカテゴリ（プランの見出し）
CAT_MOVE = "整理（移動・改名）"


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
    # 実行しない（利用者が外した）。行を消すのと同じ意味で、Web の画面からはこちらを使う
    skip: bool = False
    # 移動・改名（action = "move"）の移動先。ルートからの相対パス（新しい名前を含む）
    dest: str = ""


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


def dir_is_empty(path: Path) -> bool:
    """フォルダの中に、空のフォルダ以外のもの（ファイル・シンボリックリンクなど何でも）が無いか。

    カタログには、走査で除外したもの・名前を読めないもの・通常のファイル以外が入らないので、
    「空フォルダ」を隔離する前に、ディスクを直接見て確かめる。
    """
    try:
        stack = [path]
        while stack:
            with os.scandir(stack.pop()) as it:
                for de in it:
                    if de.is_dir(follow_symlinks=False):
                        stack.append(Path(de.path))
                    else:
                        return False
    except OSError:
        # 読めないものがあるなら、空とはみなさない
        return False
    return True


def _slug(name: str) -> str:
    """ルート名を、プラン ID に使える短い文字列にする。"""
    s = re.sub(r"[^\w.-]+", "_", name).strip("_")
    return s[-40:] or "root"


def new_plan_id(root_name: str, kind: str = "", when: datetime | None = None) -> str:
    """新しいプランの ID（日時-ルート名[-種類]）。

    ルート名は ID に使える文字にそろえる（そろえないと、空白などを含むルート名のプランを画面から開けず、取り消せない）。
    """
    stamp = f"{(when or datetime.now()):%Y%m%d-%H%M%S}-{_slug(root_name)}"
    return f"{stamp}-{_slug(kind)}" if kind else stamp


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
        if f.category == rules.CAT_EMPTY_DIR and not dir_is_empty(root_path / f.relpath):
            log.warning("空ではない（走査の対象外のものがある）ため、プランに含めません: %s", f.relpath)
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
        "# 残したいものは、その行を消すか、先頭に skip = true を足してください。",
        "# 各行の inode 以降は、変わっていないかの確認用です。",
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
                f"  {{ {'skip = true, ' if op.skip else ''}action = {_toml_str(op.action)}, path = {_toml_str(op.path)},"
                f"{' dest = ' + _toml_str(op.dest) + ',' if op.action == ACTION_MOVE else ''}"
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
            if item["action"] not in ACTIONS:
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
                    skip=bool(item.get("skip", False)),
                    dest=str(item.get("dest", "")),
                )
            )
    except KeyError as e:
        raise PlanError(f"{path}: 必要な項目がありません: {e}") from e
    if not re.fullmatch(r"[\w.-]+", plan.id):
        raise PlanError(f"{path}: プラン ID に使えない文字があります: {plan.id}")
    for op in plan.ops:
        for target in [op.path] + ([op.dest] if op.action == ACTION_MOVE else []):
            parts = target.split("/")
            if target.startswith("/") or any(p in ("", ".", "..") for p in parts):
                raise PlanError(f"{path}: ルートの外を指すパスは扱えません: {target!r}")
        if op.action == ACTION_MOVE:
            problem = rules.cloud_name_problem(op.dest)
            if problem:
                raise PlanError(f"{path}: 移動先 {op.dest} は使えません（{problem}）")
            if op.dest == op.path or op.dest.startswith(op.path + "/"):
                raise PlanError(f"{path}: 移動先が自身か配下です: {op.path} → {op.dest}")
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
    if op.category == rules.CAT_EMPTY_DIR and op.is_dir and not dir_is_empty(plan.root_path / op.path):
        return "空ではない（走査の対象外のものがある）"
    if op.action == ACTION_MOVE:
        # 移動先の途中にルートの外を指すシンボリックリンクがあると、ルートの外へ移ってしまう
        real = Path(os.path.realpath(plan.root_path / op.dest))
        base = Path(os.path.realpath(plan.root_path))
        if base not in real.parents:
            return "移動先がルートの外を指している"
        if os.path.lexists(plan.root_path / op.dest):
            return "移動先に同じ名前のものがある"
        if rules.case_conflict(plan.root_path, op.dest, op.path):
            return "移動先に大文字・小文字だけが違う名前のものがある"
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
    """実行記録を先頭から読む。

    書き込みの途中の電源断などで欠けた行は、警告して飛ばす（残りの記録から、取り消しなどを続けられるように）。
    """
    with journal.open(encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                log.warning("実行記録の %d 行目を読めないので飛ばします: %s", n, journal)
                continue
            if isinstance(rec, dict):
                yield rec


# --- 進捗の通知 ---


class Reporter:
    """長い処理の進み具合を受け取るもの。既定の実装は何もしない（CLI と Web がそれぞれ上書きする）。"""

    def phase(self, name: str, total: int | None = None, unit: str = "件") -> None:
        """新しい段階に入ったことを知らせる。total は段階内の作業量（分からなければ None）。"""

    def advance(self, n: int = 1) -> None:
        """段階内の作業が n だけ進んだことを知らせる。"""

    def note(self, line: str) -> None:
        """表示用のログを1行知らせる（同期クライアントの出力など）。"""


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
    # 同期クライアントと連携したか、連携に失敗したときの理由
    synced: bool = False
    sync_error: str | None = None

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


def _flush_and_resume(
    sync: SyncGuard,
    journal: Path,
    paths: Sequence[str],
    max_children: int,
    rep: Reporter,
    restart: bool = True,
) -> str | None:
    """止めておいた同期で削除をクラウドに反映し、常駐の同期を再開する。

    反映に失敗したら再開せず（再開しても大量削除で終了を繰り返すため）、理由を実行記録に残して返す。

    Args:
        sync: 同期クライアントとの連携の窓口。
        journal: 実行記録。
        paths: クラウドから消えるはずのパス（同期フォルダからの相対パス）。進捗の表示に使う。
        max_children: 消したパスの子の数の最大値。
        rep: 進捗の通知先。
        restart: 反映の後に常駐の同期を再開するか。止める前から止まっていた（利用者が止めていた）なら False。
    """
    expected = set(paths)
    seen: set[str] = set()

    def on_deleted(path: str) -> None:
        if path in expected and path not in seen:
            seen.add(path)
            rep.advance()

    rep.phase("削除をクラウドに反映", total=len(expected))
    try:
        sync.flush(max_children, on_deleted=on_deleted, on_line=rep.note)
    except SyncError as e:
        log.error("削除をクラウドに反映できませんでした。常駐の同期は止めたままです: %s", e)
        _append(journal, {"event": "sync-error", "detail": str(e)})
        return str(e)
    _append(journal, {"event": "sync-flush", "deleted": len(seen), "expected": len(expected)})
    if not restart:
        # 利用者が止めていた同期は、止めたままにしておく（勝手に動かし始めない）
        log.info("同期は実行の前から止まっていたので、再開しません")
        _append(journal, {"event": "sync-resume", "restarted": False})
        return None
    rep.phase("常駐の同期を再開")
    try:
        sync.resume()
    except SyncError as e:
        log.error("常駐の同期を再開できませんでした: %s", e)
        _append(journal, {"event": "sync-error", "detail": str(e)})
        return str(e)
    _append(journal, {"event": "sync-resume"})
    return None


def apply_plan(
    conn: sqlite3.Connection,
    plan: Plan,
    *,
    quarantine_dir: Path,
    journal_dir: Path,
    snapshot: SnapshotTaker | None,
    sync: SyncGuard | None = None,
    reporter: Reporter | None = None,
    tags: TagStore | None = None,
) -> RunResult:
    """プランを実行する（隔離フォルダへ移す・ツリーの中で移動・改名する）。skip の付いた操作は行わない。

    同期クライアントとの連携（sync）があり、大量削除とみなされる操作を含むなら、
    常駐の同期を止めてから移し、削除をクラウドに反映してから再開する（syncguard を参照）。

    Args:
        conn: カタログへの接続。
        plan: 実行するプラン。
        quarantine_dir: 隔離先の親フォルダ。<quarantine_dir>/<プラン ID>/<相対パス> へ移す。
        journal_dir: 実行記録を置くフォルダ。
        snapshot: 前後のスナップショットを撮るもの。None なら撮らない。
        sync: 同期クライアントとの連携の窓口。None なら連携しない。
        reporter: 進捗の通知先。
        tags: タグ。移動・改名したアイテムのタグを追従させる。

    Raises:
        PlanError: ルートが見えない・隔離先が別のファイルシステム、など。何も動かしていない。
        SnapshotError: 実行前のスナップショットを撮れなかった。何も動かしていない。
        SyncError: 常駐の同期を止められなかった。何も動かしていない。
    """
    rep = reporter or Reporter()
    try:
        check_root_available(plan.root_path)
    except RuntimeError as e:
        raise PlanError(str(e)) from e
    root_id = _root_id(conn, plan.root)
    dest_root = quarantine_dir / plan.id
    _check_same_filesystem(plan.root_path, quarantine_dir)
    journal = journal_path(journal_dir, plan.id)
    ops = [op for op in plan.ops if not op.skip]

    # 同期クライアントと連携するか（大量削除とみなされるフォルダがあるか）を先に決める
    use_sync = False
    max_children = 0
    if sync is not None and ops:
        dirs = [op for op in ops if op.is_dir and op.action == ACTION_QUARANTINE]
        rep.phase("大量削除の確認", total=len(dirs))
        children: list[int] = []
        for op in dirs:
            children.append(count_children(plan.root_path / op.path))
            rep.advance()
        max_children = max(children, default=0)
        use_sync = sync.needs_pause(children)

    result = RunResult(plan.id, synced=use_sync)
    rep.phase("実行前のスナップショット")
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
    was_active = True
    if use_sync:
        assert sync is not None
        rep.phase("常駐の同期を一時停止")
        try:
            # 止める前に動いていたか（利用者が止めていたなら、終わった後も再開しない）。
            # 返り値の無い古い実装（None）は、これまでどおり再開する
            was_active = sync.pause() is not False
        except SyncError as e:
            _append(journal, {"event": "abort", "detail": str(e)})
            raise
        _append(
            journal,
            {"event": "sync-pause", "client": sync.name, "max_children": max_children, "was_active": was_active},
        )

    rep.phase("実行（隔離・移動）", total=len(ops))
    try:
        for op in ops:
            if op.action == ACTION_MOVE:
                outcome = _move_one(conn, plan, op, root_id, journal, tags)
            else:
                outcome = _quarantine_one(conn, plan, op, root_id, dest_root, journal, gone_at, tags)
            result.outcomes.append(outcome)
            rep.advance()
    finally:
        # 途中で例外が起きても、止めた同期は必ず反映・再開を試みる
        if use_sync:
            assert sync is not None
            quarantined = {op.path for op in ops if op.action == ACTION_QUARANTINE}
            paths = [sync.relpath(plan.root_path / o.path) for o in result.done if o.path in quarantined]
            result.sync_error = _flush_and_resume(sync, journal, paths, max_children, rep, restart=was_active)

    rep.phase("実行後のスナップショット")
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


def _move_one(
    conn: sqlite3.Connection,
    plan: Plan,
    op: PlanOp,
    root_id: int,
    journal: Path,
    tags: TagStore | None,
) -> OpOutcome:
    """1操作を確かめてからツリーの中で移動・改名し、実行記録・カタログ・タグを更新する。"""
    src = plan.root_path / op.path
    dst = plan.root_path / op.dest
    problem = check_op(plan, op)
    outcome = OpOutcome(op.path, op.is_dir, op.files, op.size, problem is None, problem or "")
    if problem is None:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(src, dst)
        except OSError as e:
            outcome.done, outcome.detail = False, f"移動できなかった（{e.strerror}）"
    if not outcome.done:
        log.warning("飛ばしました: %s（%s）", op.path, outcome.detail)
        _append(journal, {"event": "skip", "path": op.path, "detail": outcome.detail})
        return outcome
    _append(
        journal,
        {
            "event": "move",
            "path": op.path,
            "dest": op.dest,
            "is_dir": op.is_dir,
            "files": op.files,
            "size": op.size,
            "src": str(src),
            "dst": str(dst),
        },
    )
    _relocate_in_catalog(conn, root_id, op.path, op.dest, op.is_dir, "plan")
    if tags is not None:
        tags.follow_moves(plan.root, [(op.path, op.dest)])
    return outcome


def _relocate_in_catalog(conn: sqlite3.Connection, root_id: int, old: str, new: str, is_dir: bool, source: str) -> None:
    """ツール自身が動かしたものの行を、新しいパスへ確実に移す（F-PL-5）。移動の履歴も残す。"""
    now = utcnow()
    with conn:
        # 移動先に残っている「存在中」の行は古い記録（移動先が空いていたことを確かめてから動かしている）
        where_new, args_new = _subtree_sql(new)
        conn.execute(
            f"UPDATE entries SET gone_at = ?, gone_by = 'plan' WHERE root_id = ? AND gone_at IS NULL AND {where_new}",
            (now, root_id, *args_new),
        )
        row = conn.execute(
            "SELECT id FROM entries WHERE root_id = ? AND gone_at IS NULL AND relpath = ?", (root_id, old)
        ).fetchone()
        where, args = _subtree_sql(old)
        conn.execute(
            f"UPDATE entries SET relpath = ? || substr(relpath, ?) WHERE root_id = ? AND gone_at IS NULL AND {where}",
            (new, len(old) + 1, root_id, *args),
        )
        if row is not None:
            record_move(conn, root_id, int(row["id"]), old, new, is_dir, source, now)


def _quarantine_one(
    conn: sqlite3.Connection,
    plan: Plan,
    op: PlanOp,
    root_id: int,
    dest_root: Path,
    journal: Path,
    gone_at: str,
    tags: TagStore | None = None,
) -> OpOutcome:
    """1操作を確かめてから隔離フォルダへ移し、実行記録・カタログ・タグを更新する。

    隔離したアイテムのタグは外し、実行記録に残す（パスに残すと、後で同じパスに来た別のファイルに付いてしまう）。
    取り消しで元に戻したら付け直す。
    """
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
    if not outcome.done:
        log.warning("飛ばしました: %s（%s）", op.path, outcome.detail)
        _append(journal, {"event": "skip", "path": op.path, "detail": outcome.detail})
        return outcome
    detached = tags.detach_under(plan.root, op.path) if tags is not None else {}
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
            **({"tags": detached} if detached else {}),
        },
    )
    where, args = _subtree_sql(op.path)
    with conn:
        # 隔離したもの（gone_by = 'plan'）は、内容ハッシュによる移動の推定の相手にしない（取り消しで元に戻すため）
        conn.execute(
            f"UPDATE entries SET gone_at = ?, gone_by = 'plan' WHERE root_id = ? AND gone_at IS NULL AND {where}",
            (gone_at, root_id, *args),
        )
    return outcome


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
    kind: str = ACTION_QUARANTINE  # quarantine（隔離から戻す） / move（移動・改名を戻す）
    dest: str = ""  # 移動・改名の移動先（ルートからの相対パス）
    # 隔離のときに外したタグ（相対パス → [タグ, 出所] の一覧）。戻したら付け直す
    tags: dict[str, list[list[str]]] = field(default_factory=dict)


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
                tags=rec.get("tags") or {},
            )
        elif event == "move":
            moved[rec["path"]] = Restorable(
                root=root,
                path=rec["path"],
                is_dir=rec["is_dir"],
                files=rec["files"],
                size=rec["size"],
                src=Path(rec["src"]),
                dst=Path(rec["dst"]),
                gone_at="",
                quarantine=quarantine,
                kind=ACTION_MOVE,
                dest=rec["dest"],
            )
        elif event in ("restore", "purge"):
            # 戻したもの・隔離を空にして消したものは、もう取り消しの対象ではない
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
    reporter: Reporter | None = None,
    tags: TagStore | None = None,
) -> RunResult:
    """実行記録をもとに、隔離・移動したものを元の場所へ戻す（F-PL-4）。

    元の場所に既に別のものがあれば、上書きせずに飛ばす。
    戻したものは常駐の同期がそのままアップロードするので、同期クライアントとの連携は要らない。

    Raises:
        PlanError: 実行記録が無い・戻すものが無い。
        SnapshotError: 実行前のスナップショットを撮れなかった。何も動かしていない。
    """
    rep = reporter or Reporter()
    journal = journal_path(journal_dir, plan_id)
    items = pending_restores(journal)
    if not items:
        raise PlanError(f"元に戻すものがありません: {plan_id}")
    root_id = _root_id(conn, items[0].root)

    result = RunResult(plan_id)
    rep.phase("実行前のスナップショット")
    result.pre_snapshot = _take_pre(snapshot, f"tag-keeper undo {plan_id}")
    _append(journal, {"event": "undo", "pre_snapshot": result.pre_snapshot})
    rep.phase("元に戻す", total=len(items))
    for it in items:
        outcome = OpOutcome(it.path, it.is_dir, it.files, it.size, True)
        if not os.path.lexists(it.dst):
            outcome.done, outcome.detail = False, "移動先に見つからない" if it.kind == ACTION_MOVE else "隔離先に見つからない"
        elif os.path.lexists(it.src):
            outcome.done, outcome.detail = False, "元の場所に別のものがある"
        else:
            try:
                it.src.parent.mkdir(parents=True, exist_ok=True)
                os.rename(it.dst, it.src)
            except OSError as e:
                outcome.done, outcome.detail = False, f"移動できなかった（{e.strerror}）"
        if outcome.done and it.kind == ACTION_MOVE:
            _append(journal, {"event": "restore", "path": it.path})
            _relocate_in_catalog(conn, root_id, it.dest, it.path, it.is_dir, "plan-undo")
            if tags is not None:
                tags.follow_moves(it.root, [(it.dest, it.path)])
        elif outcome.done:
            _remove_empty_parents(it.dst.parent, it.quarantine)
            _append(journal, {"event": "restore", "path": it.path})
            if tags is not None and it.tags:
                tags.attach(it.root, it.tags)
            where, args = _subtree_sql(it.path)
            now = utcnow()
            with conn:
                # 元の場所が空いていたので、そこに残っている「存在中」の行は古い記録。先に消えたことにする
                conn.execute(
                    f"UPDATE entries SET gone_at = ?, gone_by = 'plan' WHERE root_id = ? AND gone_at IS NULL AND {where}",
                    (now, root_id, *args),
                )
                # 隔離のときに消えたことにした行を生き返らせる（ハッシュなどの記録を引き継ぐ）
                conn.execute(
                    "UPDATE entries SET gone_at = NULL, gone_by = NULL, missing_count = 0, last_seen = ?"
                    f" WHERE root_id = ? AND gone_at = ? AND {where}",
                    (now, root_id, it.gone_at, *args),
                )
        else:
            log.warning("戻せませんでした: %s（%s）", it.path, outcome.detail)
            _append(journal, {"event": "restore-skip", "path": it.path, "detail": outcome.detail})
        result.outcomes.append(outcome)
        rep.advance()

    rep.phase("実行後のスナップショット")
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


# --- 同期が止まったままのときの復旧 ---


def sync_paused(journal: Path) -> str | None:
    """実行記録から、常駐の同期を止めたまま再開できていないかを調べる。

    Returns:
        止めたままなら最後のエラーの内容（無ければ空文字列）、再開済みか連携していなければ None。
    """
    if not journal.exists():
        return None
    paused: str | None = None
    for rec in read_journal(journal):
        event = rec.get("event")
        if event == "sync-pause":
            paused = ""
        elif event == "sync-error" and paused is not None:
            paused = str(rec.get("detail", ""))
        elif event == "sync-resume":
            paused = None
    return paused


def retry_sync(
    plan_id: str,
    *,
    journal_dir: Path,
    sync: SyncGuard,
    reporter: Reporter | None = None,
) -> str | None:
    """反映に失敗して止まったままの同期を、もう一度反映してから再開する。

    Returns:
        失敗したら理由、成功したら None。
    """
    journal = journal_path(journal_dir, plan_id)
    if sync_paused(journal) is None:
        raise PlanError(f"同期を止めたままのプランではありません: {plan_id}")
    max_children = 0
    was_active = True
    paths: list[str] = []
    for rec in read_journal(journal):
        if rec.get("event") == "sync-pause":
            max_children = int(rec.get("max_children", 0))
            # 古い記録には無い（これまでどおり再開する）
            was_active = rec.get("was_active", True) is not False
        elif rec.get("event") == "quarantine":
            paths.append(sync.relpath(Path(rec["src"])))
    return _flush_and_resume(sync, journal, paths, max_children, reporter or Reporter(), restart=was_active)


def force_resume_sync(plan_id: str, *, journal_dir: Path, sync: SyncGuard) -> None:
    """反映をあきらめて、常駐の同期だけを再開する（利用者が状況を確かめた上で選ぶ）。"""
    journal = journal_path(journal_dir, plan_id)
    sync.resume()
    _append(journal, {"event": "sync-resume", "forced": True})


# --- 隔離を空にする（F-PL-3） ---


@dataclass
class PurgeResult:
    """隔離を空にした結果。"""

    plan_id: str
    removed: int = 0  # 消したもの（隔離した単位。フォルダは1件）
    files: int = 0
    size: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)  # 消せなかったもの（パス, 理由）。取り消しの対象に残る
    pre_snapshot: int | None = None
    post_snapshot: int | None = None


def purgeable(journal: Path) -> list[Restorable]:
    """隔離フォルダに残っていて、空にできるもの（まだ取り消していない隔離）。実行記録が無ければ空。"""
    if not journal.exists():
        return []
    return [r for r in pending_restores(journal) if r.kind == ACTION_QUARANTINE]


def _inside(path: Path, base: Path) -> bool:
    """path の実体（最後の要素はたどらない）が base の中にあるか。"""
    parent = Path(os.path.realpath(path.parent))
    base_real = Path(os.path.realpath(base))
    return parent == base_real or base_real in parent.parents


def _delete_tree(path: Path, on_bytes: Callable[[int], None]) -> tuple[int, int]:
    """ファイルかフォルダを消す。シンボリックリンクはたどらず、リンクそのものを消す。消したファイルの数と容量を返す。"""
    if path.is_symlink() or not path.is_dir():
        st = path.lstat()
        path.unlink()
        on_bytes(st.st_size)
        return 1, st.st_size
    files = size = 0
    for dirpath, dirnames, filenames in os.walk(path, topdown=False):
        for name in filenames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            os.unlink(p)
            files += 1
            size += st.st_size
            on_bytes(st.st_size)
        for name in dirnames:
            p = os.path.join(dirpath, name)
            # フォルダを指すシンボリックリンクは dirnames に入る（たどらない）。リンクだけを消す
            if os.path.islink(p):
                os.unlink(p)
            else:
                os.rmdir(p)
    os.rmdir(path)
    return files, size


def purge_quarantine(
    plan_id: str,
    *,
    journal_dir: Path,
    snapshot: SnapshotTaker | None,
    reporter: Reporter | None = None,
) -> PurgeResult:
    """プランで隔離したものを完全に消す（隔離を空にする。F-PL-3）。消したものは取り消せなくなる。

    消すのは、実行記録にある隔離先（<quarantine_dir>/<プラン ID>/）の中の、まだ取り消していないものだけ。
    シンボリックリンクはたどらない。前後にスナップショットを撮り（撮れなければ何も消さない）、
    1件消すたびに実行記録に残す（途中で止まっても、消したものと残っているものが分かるように）。

    Raises:
        PlanError: 空にするものが無い・隔離先が不正。何も消していない。
        SnapshotError: 実行前のスナップショットを撮れなかった。何も消していない。
    """
    rep = reporter or Reporter()
    journal = journal_path(journal_dir, plan_id)
    items = purgeable(journal)
    if not items:
        raise PlanError(f"隔離フォルダに残っているものがありません: {plan_id}")
    quarantine = items[0].quarantine
    if quarantine.name != plan_id:
        raise PlanError(f"実行記録の隔離先がプランと合いません: {quarantine}")
    for it in items:
        if not _inside(it.dst, quarantine):
            raise PlanError(f"隔離先の外を指しているので消しません: {it.dst}")

    result = PurgeResult(plan_id)
    rep.phase("実行前のスナップショット")
    result.pre_snapshot = _take_pre(snapshot, f"tag-keeper purge {plan_id}")
    _append(journal, {"event": "purge-start", "items": len(items), "pre_snapshot": result.pre_snapshot})
    rep.phase("隔離を空にする", total=sum(it.size for it in items), unit="B")
    for it in items:
        if not os.path.lexists(it.dst):
            # 手作業で消したものなど。もう無いので、空にしたものとして記録する
            _append(journal, {"event": "purge", "path": it.path, "files": 0, "size": 0, "detail": "隔離先に見つからない"})
            result.removed += 1
            rep.advance(it.size)
            continue
        try:
            files, size = _delete_tree(it.dst, rep.advance)
        except OSError as e:
            detail = f"消せなかった（{e.strerror or e}）"
            log.warning("隔離から消せませんでした: %s（%s）", it.path, detail)
            _append(journal, {"event": "purge-skip", "path": it.path, "detail": detail})
            result.failed.append((it.path, detail))
            continue
        _append(journal, {"event": "purge", "path": it.path, "files": files, "size": size})
        _remove_empty_parents(it.dst.parent, quarantine)
        result.removed += 1
        result.files += files
        result.size += size
    rep.phase("実行後のスナップショット")
    result.post_snapshot = _take_post(snapshot, result.pre_snapshot, f"tag-keeper purge {plan_id}")
    _append(
        journal,
        {
            "event": "purge-finish",
            "removed": result.removed,
            "files": result.files,
            "size": result.size,
            "failed": len(result.failed),
            "post_snapshot": result.post_snapshot,
        },
    )
    log.info("隔離を空にしました: %s  %d 件（ファイル %d、%d バイト）", plan_id, result.removed, result.files, result.size)
    return result


# --- プランの一覧と状態 ---


@dataclass
class PlanInfo:
    """プランの概要（一覧の表示用）。"""

    id: str
    file: Path
    root: str
    created_at: str
    ops: int
    files: int
    size: int
    skipped: int
    # draft: 未実行 / applied: 実行済み / partially-undone: 一部を取り消し済み / undone: 取り消し済み
    # interrupted: 実行が途中で止まった / purged: 隔離を空にした（もう取り消せない）
    state: str
    sync_paused: str | None = None
    quarantined: int = 0
    quarantined_size: int = 0


def plan_state(journal: Path) -> tuple[str, int, int]:
    """実行記録から、プランの状態と、隔離中の件数・容量を求める。"""
    if not journal.exists():
        return "draft", 0, 0
    applied = finished = restored = purged = False
    ops_since_apply = 0
    for rec in read_journal(journal):
        event = rec.get("event")
        if event == "apply":
            applied, finished, ops_since_apply = True, False, 0
        elif event in ("quarantine", "move"):
            ops_since_apply += 1
        elif event == "abort" and ops_since_apply == 0:
            # 何も動かす前に止めた（動いている同期を待ち切れなかったなど）。未実行に戻し、画面から実行し直せるようにする
            applied = False
        elif event == "finish":
            finished = True
        elif event == "restore":
            restored = True
        elif event == "purge":
            purged = True
    pending = pending_restores(journal)
    n, size = len(pending), sum(r.size for r in pending)
    if not applied:
        return "draft", n, size
    if not finished:
        return "interrupted", n, size
    if purged and not pending:
        return "purged", n, size
    if restored:
        return ("partially-undone" if pending else "undone"), n, size
    return "applied", n, size


def plan_info(plan_file: Path, journal_dir: Path) -> PlanInfo:
    """プランのファイルと実行記録から概要を作る。"""
    plan = load_plan(plan_file)
    journal = journal_path(journal_dir, plan.id)
    state, n, size = plan_state(journal)
    active = [op for op in plan.ops if not op.skip]
    return PlanInfo(
        id=plan.id,
        file=plan_file,
        root=plan.root,
        created_at=plan.created_at,
        ops=len(active),
        files=sum(op.files for op in active),
        size=sum(op.size for op in active),
        skipped=len(plan.ops) - len(active),
        state=state,
        sync_paused=sync_paused(journal),
        quarantined=n,
        quarantined_size=size,
    )


def list_plans(plans_dir: Path, journal_dir: Path) -> list[PlanInfo]:
    """プランの一覧を新しい順に返す。読めないファイルは警告して飛ばす。"""
    out: list[PlanInfo] = []
    for f in sorted(plans_dir.glob("*.toml"), reverse=True):
        try:
            out.append(plan_info(f, journal_dir))
        except (PlanError, OSError, ValueError, KeyError, TypeError) as e:
            # 1つのプランが読めなくても、一覧（と、それを使う全画面）は出す
            log.warning("プランを読めないので一覧から外します: %s (%s)", f, e)
    return out


def set_skips(plan_file: Path, skip_paths: set[str]) -> Plan:
    """プランの各操作の skip を、skip_paths に含まれるかどうかに合わせて書き直す。"""
    plan = load_plan(plan_file)
    for op in plan.ops:
        op.skip = op.path in skip_paths
    write_plan(plan, plan_file)
    return plan
