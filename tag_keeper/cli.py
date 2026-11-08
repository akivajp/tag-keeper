"""tag-keeper のコマンドライン。

    tag-keeper roots                 設定済みのルートと、カタログの状態を一覧する
    tag-keeper scan [ROOT ...]       ルートを走査してカタログを更新する（読み取り専用）
    tag-keeper hash [ROOT ...]       変わったファイルだけ内容ハッシュを計算する
    tag-keeper extract [ROOT ...]    本文を抽出して、全文検索の索引に入れる
    tag-keeper tags / tag / alias / parent / attr / relate   タグ・辞書・属性・関係を見る・付ける
    tag-keeper export / import       管理情報の全量を JSONL で書き出す・読み込む
    tag-keeper search [WORD ...]     名前・本文（--text）・種類・タグで探す
    tag-keeper report [ROOT ...]     整理候補（置く価値の薄いファイル・重複）を報告する
    tag-keeper plan ROOT             整理候補を隔離するプラン（TOML）を書き出す
    tag-keeper apply PLAN [--yes]    プランを確認する。--yes で実行する（隔離フォルダへ移す）
    tag-keeper undo PLAN_ID [--yes]  実行したプランを取り消す
    tag-keeper purge PLAN_ID [--yes] 隔離したものを完全に消す（隔離を空にする）
    tag-keeper sync PLAN_ID          止めたままの同期を再試行・再開する
    tag-keeper serve                 Web の画面を開く

ROOT には、設定ファイルのルート名か、フォルダのパスを指定する。省略すると設定済みの全ルート。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TaskID,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from tag_keeper import __version__, rules
from tag_keeper import extract as extract_mod
from tag_keeper.catalog import connect, ensure_root, last_scan
from tag_keeper.config import (
    Config,
    ConfigError,
    RootConfig,
    default_config_path,
    default_db_path,
    load_config,
)
from tag_keeper.dirtypes import scan_exclusions
from tag_keeper.fulltext import KINDS, Pending, find_entries, query_terms, run_pending, snippets
from tag_keeper.hashing import hash_root
from tag_keeper.plan import (
    OpOutcome,
    Plan,
    PlanError,
    Reporter,
    RunResult,
    Snapper,
    SnapshotError,
    apply_plan,
    build_plan,
    check_op,
    force_resume_sync,
    journal_path,
    load_plan,
    pending_restores,
    purge_quarantine,
    purgeable,
    read_journal,
    retry_sync,
    sync_paused,
    undo_plan,
    write_plan,
)
from tag_keeper.report import Report, structure_issues, user_report
from tag_keeper.scan import RootUnavailableError, relink_by_hash, scan_root
from tag_keeper.syncguard import SyncError, SyncGuard, make_guard
from tag_keeper.tags import TagError, TagStore
from tag_keeper.tags import search as search_tags

log = logging.getLogger("tag_keeper")

# 進捗やログは標準エラーへ、結果の表は標準出力へ出す
err_console = Console(stderr=True)
out_console = Console()


def human_size(n: float) -> str:
    """バイト数を読みやすい単位の文字列にする。"""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}PB"


def setup_logging(log_file: Path | None, verbose: bool) -> None:
    """ログを設定する。画面には rich で、--log-file を指定したらファイルにも日時付きで書く。"""
    handlers: list[logging.Handler] = [
        RichHandler(console=err_console, show_path=False, rich_tracebacks=True)
    ]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handlers.append(fh)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=handlers,
        force=True,
    )


def resolve_roots(config: Config, names: Sequence[str]) -> list[RootConfig]:
    """コマンドラインで指定されたルートを解決する。

    名前が設定ファイルにあればそれを、無ければフォルダのパスとして扱う。
    何も指定されなければ、設定済みの全ルートを返す。
    """
    if not names:
        if not config.roots:
            raise ConfigError(
                "ルートが指定されていません。フォルダのパスを指定するか、"
                f"設定ファイル（{default_config_path()}）に [[roots]] を書いてください"
            )
        return list(config.roots)
    resolved: list[RootConfig] = []
    for name in names:
        rc = config.find_root(name)
        if rc is None:
            path = Path(name).expanduser().resolve()
            if not path.is_dir():
                raise ConfigError(f"ルート名でもフォルダでもありません: {name}")
            rc = RootConfig(name=str(path), path=path)
        resolved.append(rc)
    return resolved


def cmd_roots(args: argparse.Namespace, config: Config) -> int:
    """設定済みのルートと、カタログの状態を一覧する。"""
    conn = connect(args.db)
    table = Table(title="ルート")
    for col in ("名前", "パス", "ファイル数", "容量", "最後の走査", "状態"):
        table.add_column(col)
    names = [r.name for r in config.roots] + [
        row["name"]
        for row in conn.execute("SELECT name FROM roots ORDER BY name")
        if config.find_root(row["name"]) is None
    ]
    for name in names:
        rc = config.find_root(name)
        row = conn.execute("SELECT id, path FROM roots WHERE name = ?", (name,)).fetchone()
        path = str(rc.path) if rc else (row["path"] if row else "")
        if row is None:
            table.add_row(name, path, "-", "-", "未走査", "")
            continue
        n, size = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM entries"
            " WHERE root_id = ? AND gone_at IS NULL AND is_dir = 0",
            (row["id"],),
        ).fetchone()
        scan = last_scan(conn, row["id"])
        table.add_row(
            name,
            path,
            f"{n:,}",
            human_size(size),
            scan["finished_at"] if scan else "未走査",
            scan["status"] if scan else "",
        )
    out_console.print(table)
    return 0


def cmd_scan(args: argparse.Namespace, config: Config) -> int:
    """ルートを走査してカタログを更新する。"""
    conn = connect(args.db)
    status = 0
    for rc in resolve_roots(config, args.roots):
        root_id = ensure_root(conn, rc.name, rc.path)
        log.info("走査を始めます: %s (%s)", rc.name, rc.path)
        with Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            TextColumn("{task.completed:,} 件"),
            TimeElapsedColumn(),
            console=err_console,
            transient=True,
        ) as progress:
            task = progress.add_task(f"走査中: {rc.name}", total=None)
            skip = scan_exclusions(config, rc, TagStore(config.tags.dir).dir_types_of(rc.name))
            for p, why in sorted(skip.items()):
                log.info("走査しないフォルダ: %s（%s）", p, why)
            try:
                res = scan_root(
                    conn,
                    root_id,
                    rc.path,
                    config.safety,
                    exclude=rc.exclude,
                    accept_missing=args.accept_missing,
                    on_progress=lambda n: progress.update(task, completed=n),
                    exclude_paths=skip,
                )
            except RootUnavailableError as e:
                log.error("%s", e)
                status = 2
                continue
        log.info(
            "走査を終えました: %s  見えた %s / 追加 %s / 変更 %s / 移動 %s / 消失 %s / 保留 %s / 読めないフォルダ %s",
            rc.name,
            f"{res.seen:,}",
            f"{res.added:,}",
            f"{res.changed:,}",
            f"{res.moved:,}",
            f"{res.gone:,}",
            f"{res.held:,}",
            f"{res.unreadable:,}",
        )
        # ツールの外での移動・改名に、タグを追従させる（P4）
        TagStore(config.tags.dir).follow_moves(rc.name, res.moves)
        if res.held:
            status = max(status, 1)
    return status


def cmd_hash(args: argparse.Namespace, config: Config) -> int:
    """変わったファイルだけ内容ハッシュを計算する。"""
    conn = connect(args.db)
    for rc in resolve_roots(config, args.roots):
        root_id = ensure_root(conn, rc.name, rc.path)
        with Progress(
            TextColumn("{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=err_console,
        ) as progress:
            task = progress.add_task(f"ハッシュ計算: {rc.name}", total=None)

            def on_start(count: int, total_bytes: int) -> None:
                log.info("ハッシュを計算します: %s  %s 件 / %s", rc.name, f"{count:,}", human_size(total_bytes))
                progress.update(task, total=total_bytes)

            res = hash_root(
                conn,
                root_id,
                rc.path,
                on_start=on_start,
                on_bytes=lambda n: progress.advance(task, n),
            )
        log.info(
            "ハッシュの計算を終えました: %s  計算 %s 件（%s） / 変化のため後回し %s / 読めない %s",
            rc.name,
            f"{res.hashed:,}",
            human_size(res.hashed_bytes),
            f"{res.skipped_changed:,}",
            f"{res.skipped_error:,}",
        )
        # inode で結び付かなかった移動（クラウド側での移動が「削除と新規」として届いたものなど）を、
        # 内容ハッシュで結び付け、タグを追従させる（F-ID-1・P4）
        rl = relink_by_hash(conn, root_id, days=config.scan.relink_days)
        if rl.moves:
            TagStore(config.tags.dir).follow_moves(rc.name, rl.moves)
    return 0


def cmd_extract(args: argparse.Namespace, config: Config) -> int:
    """ハッシュを計算済みのファイルから本文を抽出し、全文検索の索引に入れる（Web の画面が裏で行うのと同じ）。"""
    if args.no_ocr:
        config.extract.ocr = False
    ocr = extract_mod.ocr_languages(config.extract.ocr_lang) if config.extract.ocr else None
    log.info("OCR: %s", ocr or ("tesseract が無いので使いません" if config.extract.ocr else "使いません"))
    conn = connect(args.db)
    for rc in resolve_roots(config, args.roots):
        root_id = ensure_root(conn, rc.name, rc.path)
        with Progress(
            TextColumn("{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            TextColumn("[dim]{task.fields[current]}"),
            console=err_console,
        ) as progress:
            task = progress.add_task(f"本文の抽出: {rc.name}", total=None, current="")

            def on_start(count: int) -> None:
                log.info("本文を抽出します: %s  %s 件", rc.name, f"{count:,}")
                progress.update(task, total=count)

            def on_item(item: Pending, _ex: object) -> None:
                progress.update(task, advance=1, current=item.relpath[-50:])

            res = run_pending(conn, root_id, rc.path, config, limit=args.limit, on_start=on_start, on_item=on_item)
        log.info(
            "本文の抽出を終えました: %s  抽出 %s 件（うち OCR %s） / 読めない %s / 後回し %s",
            rc.name,
            f"{res.done:,}",
            f"{res.ocr:,}",
            f"{res.errors:,}",
            f"{res.skipped:,}",
        )
    return 0


def cmd_search(args: argparse.Namespace, config: Config) -> int:
    """名前・本文・種類・タグで探して、表にする（Web の画面の「検索」と同じ条件）。"""
    if not (args.words or args.text or args.kind or args.tag or args.exclude):
        log.error("探す条件（名前の語・--text・--kind・--tag・--not のどれか）を指定してください")
        return 2
    conn = connect(args.db)
    store = TagStore(config.tags.dir)
    terms = query_terms(args.text or "")
    table = Table(title="検索の結果")
    for col, just in (("ルート", "left"), ("パス", "left"), ("サイズ", "right"), ("更新日時", "left"), ("一致したところ", "left")):
        table.add_column(col, justify=just)  # type: ignore[arg-type]
    total = 0
    for rc in resolve_roots(config, args.root or []):
        row = conn.execute("SELECT id FROM roots WHERE name = ?", (rc.name,)).fetchone()
        if row is None:
            log.warning("まだ走査していません: %s", rc.name)
            continue
        rows = find_entries(
            conn, int(row["id"]), words=args.words, text=args.text or "", kind=args.kind or "", files_only=not args.folders
        )
        info = {r["relpath"]: r for r in rows}
        paths = list(info)
        if args.tag or args.exclude:
            paths = search_tags(
                ((p, bool(info[p]["is_dir"])) for p in paths),
                store.tagged_paths(rc.name),
                args.tag or [],
                "or" if args.any else "and",
                args.exclude or [],
            )
        # 表に載せる件数の上限は、全ルートを通して数える
        shown = paths[: max(args.limit - len(table.rows), 0)]
        snips = snippets(conn, [info[p]["sha256"] for p in shown if info[p]["sha256"]], terms) if terms else {}
        for p in shown:
            r = info[p]
            mtime = datetime.fromtimestamp(r["mtime_ns"] / 1e9).strftime("%Y-%m-%d %H:%M")
            table.add_row(
                rc.name, p + ("/" if r["is_dir"] else ""), "" if r["is_dir"] else human_size(r["size"]), mtime,
                snips.get(r["sha256"] or "", ""),
            )
        total += len(paths)
    out_console.print(table)
    shown_n = len(table.rows)
    out_console.print(f"[dim]{total:,} 件{f'（うち {shown_n:,} 件を表示。--limit で増やせます）' if shown_n < total else ''}[/dim]")
    return 0


# --- タグ・辞書・属性・関係・書き出しと読み込み（画面と同じことを CLI からも。F-UI-1・F-MD-9） ---


def _root_named(config: Config, name: str) -> RootConfig:
    """設定ファイルのルートを名前で引く。無ければ ConfigError。"""
    rc = config.find_root(name)
    if rc is None:
        raise ConfigError(f"設定ファイルにルートがありません: {name}")
    return rc


def _relpath_in(rc: RootConfig, raw: str, *, must_exist: bool = True) -> str:
    """コマンドラインで渡されたパス（ルートからの相対パスか、ルートの中の絶対パス）を、ルートからの相対パスにする。"""
    p = Path(raw).expanduser()
    if p.is_absolute():
        try:
            rel = p.resolve().relative_to(rc.path.resolve()).as_posix()
        except ValueError as e:
            raise ConfigError(f"ルート {rc.name} の外を指しています: {raw}") from e
        rel = "" if rel == "." else rel
    else:
        rel = raw.strip("/")
        if any(part in ("", ".", "..") for part in rel.split("/")) and rel:
            raise ConfigError(f"不正なパスです: {raw}")
    if must_exist and not (rc.path / rel).exists():
        raise ConfigError(f"ありません: {rc.name}/{rel}")
    return rel


def cmd_tags(args: argparse.Namespace, config: Config) -> int:
    """タグ（件数・別名・親）・属性の定義・保存した検索を一覧する。"""
    store = TagStore(config.tags.dir)
    # 先に辞書を読み、続けて件数を読む（件数の側が、タグが直接付いている数の真実源）
    dictionary = store.dictionary()
    counts = store.all_tags()
    table = Table(title="タグ")
    for col in ("タグ", "件数", "別名", "親"):
        table.add_column(col)
    for tag in sorted(set(counts) | set(dictionary)):
        d = dictionary.get(tag, {})
        table.add_row(tag, f"{counts.get(tag, 0):,}", "、".join(d.get("aliases", [])), "、".join(d.get("parents", [])))
    out_console.print(table)
    defs = store.attr_defs_list()
    if defs:
        at = Table(title="属性")
        for col in ("名前", "型", "選択肢", "件数"):
            at.add_column(col)
        for d in defs:
            at.add_row(d["name"], d["type"], " / ".join(d["choices"]), f"{d['count']:,}")
        out_console.print(at)
    searches = store.saved_searches()
    if searches:
        st = Table(title="保存した検索")
        st.add_column("名前")
        st.add_column("条件")
        for sv in searches:
            st.add_row(sv["name"], sv["query"])
        out_console.print(st)
    return 0


def cmd_tag(args: argparse.Namespace, config: Config) -> int:
    """ファイル・フォルダにタグを付ける（--add）・外す（--remove）。別名は正式な名前になる。"""
    if not (args.add or args.remove):
        log.error("--add か --remove でタグを指定してください")
        return 2
    rc = _root_named(config, args.root)
    paths = [_relpath_in(rc, p) for p in args.paths]
    store = TagStore(config.tags.dir)
    for tag in args.add or []:
        log.info("「%s」を %d 件に付けました", store.canonical(tag), store.add(rc.name, paths, tag))
    for tag in args.remove or []:
        log.info("「%s」を %d 件から外しました", store.canonical(tag), store.remove(rc.name, paths, tag))
    return 0


def cmd_alias(args: argparse.Namespace, config: Config) -> int:
    """タグの別名を登録する（--remove で外す）。既に使われている名前を別名にすると、正式な名前にまとめる。"""
    store = TagStore(config.tags.dir)
    if args.remove:
        ok = store.remove_alias(args.alias)
        log.info("別名を外しました: %s" if ok else "その別名は登録されていません: %s", args.alias)
        return 0 if ok else 1
    store.add_alias(args.tag, args.alias)
    log.info("「%s」を「%s」の別名にしました", args.alias, store.canonical(args.tag))
    return 0


def cmd_parent(args: argparse.Namespace, config: Config) -> int:
    """タグの親子を登録する（--remove で外す）。子が付いたものは、親のタグで探しても見つかる。"""
    store = TagStore(config.tags.dir)
    if args.remove:
        ok = store.remove_parent(args.tag, args.parent)
        log.info("親子を外しました: %s → %s" if ok else "その親子は登録されていません: %s → %s", args.tag, args.parent)
        return 0 if ok else 1
    store.add_parent(args.tag, args.parent)
    log.info("親子を登録しました: %s → %s", store.canonical(args.tag), store.canonical(args.parent))
    return 0


def cmd_attr(args: argparse.Namespace, config: Config) -> int:
    """属性を定義する（define）・定義を消す（undefine）・値を付ける（set）・外す（unset）。"""
    store = TagStore(config.tags.dir)
    if args.attr_command == "define":
        choices = [c.strip() for c in (args.choices or "").replace("、", ",").split(",") if c.strip()]
        d = store.define_attr(args.name, args.type, choices)
        log.info("属性を定義しました: %s（%s%s）", d.name, d.type, f": {' / '.join(d.choices)}" if d.choices else "")
        return 0
    if args.attr_command == "undefine":
        log.info("属性の定義を消しました: %s（値が付いていた %d 件から外しました）", args.name, store.undefine_attr(args.name))
        return 0
    rc = _root_named(config, args.root)
    paths = [_relpath_in(rc, p) for p in args.paths]
    if args.attr_command == "set":
        log.info("「%s」を %d 件に付けました", args.name, store.set_attr(rc.name, paths, args.name, args.value))
    else:
        log.info("「%s」を %d 件から外しました", args.name, store.unset_attr(rc.name, paths, args.name))
    return 0


def cmd_relate(args: argparse.Namespace, config: Config) -> int:
    """2つのアイテムの間に関係を張る（--remove で外す）。FROM から TO へ（例: 新しい版 → 前の版）。"""
    rc = _root_named(config, args.root)
    store = TagStore(config.tags.dir)
    src = _relpath_in(rc, args.src, must_exist=not args.remove)
    dst = _relpath_in(rc, args.dst, must_exist=not args.remove)
    if args.remove:
        ok = store.unrelate(rc.name, src, dst, args.kind)
    else:
        ok = store.relate(rc.name, src, dst, args.kind)
    log.info("%s: %s →（%s）→ %s", ("外しました" if args.remove else "張りました") if ok else "変わりません", src, args.kind, dst)
    return 0


def cmd_export(args: argparse.Namespace, config: Config) -> int:
    """管理情報の全量（タグ・属性・関係・辞書・保存した検索）を JSONL で書き出す。"""
    events = TagStore(config.tags.dir).export_events()
    text = "".join(json.dumps(ev, ensure_ascii=False) + "\n" for ev in events)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        log.info("書き出しました: %s（%s 行）", args.output, f"{len(events):,}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_import(args: argparse.Namespace, config: Config) -> int:
    """export で書き出した JSONL を読み込む（この端末のタグのログに追記する。既にあるものは飛ばす）。"""
    events = []
    for n, line in enumerate(args.file.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise ConfigError(f"{args.file} の {n} 行目を読めません: {e}") from e
    try:
        counts = TagStore(config.tags.dir).import_events(events)
    except TagError as e:
        raise ConfigError(f"{args.file}: {e}") from e
    log.info("読み込みました: %s", "、".join(f"{k} {v:,} 件" for k, v in sorted(counts.items())) or "（足したものはありません）")
    return 0


def render_report(report: Report, examples: int) -> None:
    """報告を表にして標準出力に描く。"""
    out_console.rule(f"[bold]{report.root}")
    out_console.print(
        f"ファイル {report.files:,} / フォルダ {report.dirs:,} / 合計 {human_size(report.total_bytes)}"
        f" / ハッシュ計算済み {report.hashed_files:,}"
    )

    table = Table(title="置く価値の薄いものの候補", show_lines=True)
    for col, just in (("カテゴリ", "left"), ("件数", "right"), ("ファイル", "right"), ("容量", "right"), ("容量の大きい例", "left")):
        table.add_column(col, justify=just)  # type: ignore[arg-type]
    for category, items in report.by_category().items():
        items = sorted(items, key=lambda f: f.size, reverse=True)
        sample = "\n".join(
            f"{human_size(f.size):>8}  {f.relpath}{'/' if f.is_dir else ''}  ({f.reason})"
            for f in items[:examples]
        )
        table.add_row(
            category,
            f"{len(items):,}",
            f"{sum(f.files for f in items):,}",
            human_size(sum(f.size for f in items)),
            sample,
        )
    out_console.print(table)

    if report.duplicates:
        dup = Table(title=f"内容が同じファイル（{len(report.duplicates):,} 組、余分な容量 {human_size(sum(g.waste for g in report.duplicates))}）")
        for col, just in (("サイズ", "right"), ("個数", "right"), ("余分", "right"), ("パス", "left")):
            dup.add_column(col, justify=just)  # type: ignore[arg-type]
        for g in report.duplicates[:examples * 3]:
            dup.add_row(human_size(g.size), str(len(g.paths)), human_size(g.waste), "\n".join(g.paths[:4]))
        out_console.print(dup)
    elif report.hashed_files < report.files:
        out_console.print(
            f"[dim]重複は、ハッシュを計算したファイルだけで判定します（未計算 {report.files - report.hashed_files:,} 件）。"
            " `tag-keeper hash` で計算できます[/dim]"
        )

    fan = Table(title="直下のファイルが多いフォルダ（候補の配下を除く）")
    fan.add_column("ファイル", justify="right")
    fan.add_column("フォルダ")
    for d, n in report.fanout:
        fan.add_row(f"{n:,}", d or "(ルート直下)")
    out_console.print(fan)


def cmd_report(args: argparse.Namespace, config: Config) -> int:
    """整理候補を報告する。"""
    conn = connect(args.db)
    reports: list[Report] = []
    for rc in resolve_roots(config, args.roots):
        row = conn.execute("SELECT id FROM roots WHERE name = ?", (rc.name,)).fetchone()
        if row is None or last_scan(conn, row["id"]) is None:
            log.error("まだ走査していません: %s（先に tag-keeper scan を実行してください）", rc.name)
            return 2
        store = TagStore(config.tags.dir)
        report = user_report(conn, row["id"], rc.name, config, store, top_fanout=args.top)
        render_report(report, args.examples)
        issues = structure_issues(conn, row["id"], rc.name, config, store, report)
        if issues:
            st = Table(title="構造の見直しの提案")
            for col in ("種類", "フォルダ", "内容"):
                st.add_column(col)
            for i in issues:
                st.add_row(i.to_dict()["label"], i.path or "（ルート）", i.detail)
            out_console.print(st)
        reports.append(report)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps([r.to_dict() for r in reports], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        log.info("JSON を書き出しました: %s", args.json)
    return 0


def _plans_dir(args: argparse.Namespace) -> Path:
    """プランの既定の置き場所（カタログと同じフォルダの plans/）。"""
    return Path(args.db).parent / "plans"


def _journal_dir(args: argparse.Namespace) -> Path:
    """実行記録の置き場所（カタログと同じフォルダの journal/）。"""
    return Path(args.db).parent / "journal"


def _snapper(args: argparse.Namespace, config: Config) -> Snapper | None:
    """設定と --no-snapshot に従って、スナップショットを撮るものを返す。"""
    if args.no_snapshot or not config.plan.snapper_config:
        return None
    return Snapper(config.plan.snapper_config)


def _parse_categories(values: Sequence[str] | None) -> list[str] | None:
    """--category の値（短い名前か表示名）を表示名に直す。"""
    if not values:
        return None
    labels = set(rules.CATEGORY_KEYS.values())
    out: list[str] = []
    for v in values:
        if v in rules.CATEGORY_KEYS:
            out.append(rules.CATEGORY_KEYS[v])
        elif v in labels:
            out.append(v)
        else:
            raise ConfigError(f"カテゴリが不明です: {v}（使えるもの: {', '.join(rules.CATEGORY_KEYS)}）")
    return out


def cmd_plan(args: argparse.Namespace, config: Config) -> int:
    """整理候補を隔離するプランを書き出す。"""
    conn = connect(args.db)
    rc = resolve_roots(config, [args.root])[0]
    row = conn.execute("SELECT id FROM roots WHERE name = ?", (rc.name,)).fetchone()
    if row is None or last_scan(conn, row["id"]) is None:
        log.error("まだ走査していません: %s（先に tag-keeper scan を実行してください）", rc.name)
        return 2
    report = user_report(conn, row["id"], rc.name, config, TagStore(config.tags.dir))
    plan = build_plan(
        report, rc.name, rc.path, _parse_categories(args.category), include_review=args.include_review
    )
    review = sum(1 for f in report.findings if f.needs_review)
    if review and not args.include_review:
        log.info("中身の確認が要る候補 %d 件はプランに含めていません（report で確認できます。含めるには --include-review）", review)
    out = args.output or _plans_dir(args) / f"{plan.id}.toml"
    write_plan(plan, out)

    table = Table(title=f"整理プラン {plan.id}")
    for col, just in (("カテゴリ", "left"), ("件数", "right"), ("ファイル", "right"), ("容量", "right")):
        table.add_column(col, justify=just)  # type: ignore[arg-type]
    by_cat: dict[str, list] = {}
    for op in plan.ops:
        by_cat.setdefault(op.category, []).append(op)
    for cat in sorted(by_cat, key=lambda c: rules.CATEGORY_ORDER.index(c) if c in rules.CATEGORY_ORDER else len(rules.CATEGORY_ORDER)):
        ops = by_cat[cat]
        table.add_row(cat, f"{len(ops):,}", f"{sum(o.files for o in ops):,}", human_size(sum(o.size for o in ops)))
    table.add_row(
        "[bold]合計",
        f"[bold]{len(plan.ops):,}",
        f"[bold]{sum(o.files for o in plan.ops):,}",
        f"[bold]{human_size(sum(o.size for o in plan.ops))}",
    )
    out_console.print(table)
    out_console.print(f"プランを書き出しました: {out}")
    out_console.print("残したいものの行を消してから、次のコマンドで確認・実行してください。")
    out_console.print(f"  tag-keeper apply {out}          # 確認だけ")
    out_console.print(f"  tag-keeper apply {out} --yes    # 隔離フォルダへ移す")
    return 0


def render_outcomes(title: str, outcomes: Sequence[OpOutcome], limit: int) -> None:
    """操作の結果（または確認結果）を表にする。飛ばすものを先に、容量の大きい順に並べる。"""
    table = Table(title=title)
    for col, just in (("状態", "left"), ("ファイル", "right"), ("容量", "right"), ("パス", "left")):
        table.add_column(col, justify=just)  # type: ignore[arg-type]
    ordered = sorted(outcomes, key=lambda o: (o.done, -o.size))
    for o in ordered[:limit]:
        state = "[green]OK" if o.done else f"[yellow]飛ばす: {o.detail}"
        table.add_row(state, f"{o.files:,}", human_size(o.size), o.path + ("/" if o.is_dir else ""))
    if len(ordered) > limit:
        table.add_row("…", "", "", f"ほか {len(ordered) - limit:,} 件（--limit で増やせます）")
    out_console.print(table)


def _summary(result: RunResult, verb: str) -> str:
    """実行結果の1行の要約。"""
    done = result.done
    return (
        f"{verb} {len(done):,} 件（ファイル {sum(o.files for o in done):,}、"
        f"{human_size(sum(o.size for o in done))}） / 飛ばした {len(result.skipped):,} 件"
    )


class RichReporter(Reporter):
    """進捗を rich の進捗バーで表示する（段階が変わるたびに、バーを作り直す）。"""

    def __init__(self, progress: Progress) -> None:
        self.progress = progress
        self.task: TaskID | None = None

    def phase(self, name: str, total: int | None = None, unit: str = "件") -> None:
        """段階の名前と作業量でバーを作り直す。"""
        if self.task is not None:
            self.progress.update(self.task, visible=False)
        log.info("段階: %s", name)
        self.task = self.progress.add_task(name, total=total)

    def advance(self, n: int = 1) -> None:
        """バーを進める。"""
        if self.task is not None:
            self.progress.advance(self.task, n)

    def note(self, line: str) -> None:
        """同期クライアントの出力などは、詳しいログ（-v）にだけ出す。"""
        log.debug("%s", line)


def _rich_progress() -> Progress:
    """段階ごとの進捗バー。"""
    return Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=err_console,
    )


def _sync_guard(config: Config, root_name: str) -> SyncGuard | None:
    """ルートの設定から、同期クライアントとの連携の窓口を作る。"""
    rc = config.find_root(root_name)
    if rc is None:
        return None
    return make_guard(rc.sync_client, rc.sync_service, rc.sync_guard)


def _log_sync_error(result: RunResult) -> None:
    """同期クライアントとの連携に失敗したことと、復旧の方法を知らせる。"""
    if result.sync_error:
        log.error(
            "削除をクラウドに反映できず、常駐の同期を止めたままです: %s\n"
            "  再試行: tag-keeper sync %s --retry\n"
            "  反映をあきらめて再開: tag-keeper sync %s --resume",
            result.sync_error,
            result.plan_id,
            result.plan_id,
        )


def cmd_apply(args: argparse.Namespace, config: Config) -> int:
    """プランを確認し、--yes なら実行する。"""
    plan: Plan = load_plan(args.plan)
    conn = connect(args.db)
    if not args.yes:
        checks = []
        for op in plan.ops:
            problem = "プランで除外" if op.skip else check_op(plan, op)
            checks.append(OpOutcome(op.path, op.is_dir, op.files, op.size, problem is None, problem or ""))
        render_outcomes(f"確認: {plan.id}（{plan.root_path}）", checks, args.limit)
        ok = [c for c in checks if c.done]
        out_console.print(
            f"実行できる {len(ok):,} 件（ファイル {sum(c.files for c in ok):,}、{human_size(sum(c.size for c in ok))}）"
            f"を {config.plan.quarantine_dir / plan.id} へ移します。"
            f"飛ばすもの {len(checks) - len(ok):,} 件。"
        )
        out_console.print("[bold]確認だけで、何も動かしていません。[/bold]実行するには --yes を付けてください。")
        return 0
    with _rich_progress() as progress:
        result = apply_plan(
            conn,
            plan,
            quarantine_dir=config.plan.quarantine_dir,
            journal_dir=_journal_dir(args),
            snapshot=_snapper(args, config),
            sync=_sync_guard(config, plan.root),
            reporter=RichReporter(progress),
            tags=TagStore(config.tags.dir),
        )
    render_outcomes(f"実行結果: {plan.id}", result.outcomes, args.limit)
    log.info("%s", _summary(result, "隔離した"))
    log.info(
        "スナップショット: 実行前 %s / 実行後 %s。取り消すには: tag-keeper undo %s --yes",
        result.pre_snapshot if result.pre_snapshot is not None else "なし",
        result.post_snapshot if result.post_snapshot is not None else "なし",
        plan.id,
    )
    _log_sync_error(result)
    if result.sync_error:
        return 2
    return 1 if result.skipped else 0


def _plan_id_of(value: str) -> str:
    """undo の引数（プラン ID か、プランのファイル）からプラン ID を得る。"""
    path = Path(value)
    if path.suffix == ".toml" and path.exists():
        return load_plan(path).id
    return value


def cmd_undo(args: argparse.Namespace, config: Config) -> int:
    """実行したプランを取り消し、隔離したものを元の場所へ戻す。"""
    plan_id = _plan_id_of(args.plan)
    conn = connect(args.db)
    if not args.yes:
        items = pending_restores(journal_path(_journal_dir(args), plan_id))
        render_outcomes(
            f"取り消しの確認: {plan_id}",
            [OpOutcome(i.path, i.is_dir, i.files, i.size, True) for i in items],
            args.limit,
        )
        out_console.print("[bold]確認だけで、何も動かしていません。[/bold]元に戻すには --yes を付けてください。")
        return 0
    with _rich_progress() as progress:
        result = undo_plan(
            conn,
            plan_id,
            journal_dir=_journal_dir(args),
            snapshot=_snapper(args, config),
            reporter=RichReporter(progress),
            tags=TagStore(config.tags.dir),
        )
    render_outcomes(f"取り消しの結果: {plan_id}", result.outcomes, args.limit)
    log.info("%s", _summary(result, "元に戻した"))
    return 1 if result.skipped else 0


def cmd_purge(args: argparse.Namespace, config: Config) -> int:
    """プランで隔離したものを完全に消す（隔離を空にする）。消したものは取り消せなくなる。"""
    plan_id = _plan_id_of(args.plan)
    items = purgeable(journal_path(_journal_dir(args), plan_id))
    if not items:
        log.info("隔離フォルダに残っているものはありません: %s", plan_id)
        return 0
    if not args.yes:
        render_outcomes(
            f"隔離を空にする確認: {plan_id}",
            [OpOutcome(i.path, i.is_dir, i.files, i.size, True) for i in items],
            args.limit,
        )
        out_console.print(
            "[bold]確認だけで、何も消していません。[/bold]"
            "--yes を付けると完全に削除し、このプランは取り消せなくなります"
            "（スナップショットが残っている間は、そこから手作業で取り出せます）。"
        )
        return 0
    with _rich_progress() as progress:
        result = purge_quarantine(
            plan_id, journal_dir=_journal_dir(args), snapshot=_snapper(args, config), reporter=RichReporter(progress)
        )
    log.info(
        "隔離を空にしました: %s  %s 件（ファイル %s、%s）%s",
        plan_id,
        f"{result.removed:,}",
        f"{result.files:,}",
        human_size(result.size),
        f"。消せなかったもの {len(result.failed):,} 件" if result.failed else "",
    )
    for path, detail in result.failed:
        log.warning("消せませんでした: %s（%s）", path, detail)
    return 1 if result.failed else 0


def cmd_sync(args: argparse.Namespace, config: Config) -> int:
    """削除の反映に失敗して止まったままの同期を、再試行するか再開する。"""
    plan_id = _plan_id_of(args.plan)
    journal = journal_path(_journal_dir(args), plan_id)
    paused = sync_paused(journal)
    if paused is None:
        log.info("このプランで止めたままの同期はありません: %s", plan_id)
        return 0
    root = next((r["root"] for r in read_journal(journal) if r.get("event") == "apply"), "")
    guard = _sync_guard(config, root)
    if guard is None:
        raise ConfigError(f"ルート {root} に同期クライアント（sync_client）が設定されていません")
    if args.resume:
        force_resume_sync(plan_id, journal_dir=_journal_dir(args), sync=guard)
        log.info("常駐の同期を再開しました（削除の反映は確かめていません）")
        return 0
    if not args.retry:
        log.info("同期を止めたままです（%s）。--retry で再試行、--resume で再開します", paused or "理由の記録なし")
        return 1
    with _rich_progress() as progress:
        error = retry_sync(plan_id, journal_dir=_journal_dir(args), sync=guard, reporter=RichReporter(progress))
    if error:
        log.error("まだ反映できません: %s", error)
        return 2
    log.info("削除を反映し、常駐の同期を再開しました")
    return 0


def cmd_serve(args: argparse.Namespace, config: Config) -> int:
    """Web の画面を開く。"""
    from tag_keeper.web.server import serve

    return serve(
        host=args.host,
        port=args.port,
        config_path=args.config,
        db_path=args.db,
        auth_file=args.auth_file,
        allow_no_auth=args.allow_no_auth,
    )


def build_parser() -> argparse.ArgumentParser:
    """コマンドラインの解析器を作る。"""
    parser = argparse.ArgumentParser(
        prog="tag-keeper",
        description="既存のフォルダ構造を動かさずに、外側からカタログを作り、整理の候補を報告する",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config", type=Path, default=None, help=f"設定ファイル（既定: {default_config_path()}）"
    )
    parser.add_argument(
        "--db", type=Path, default=None, help=f"カタログの SQLite ファイル（既定: {default_db_path()}）"
    )
    parser.add_argument("--log-file", type=Path, default=None, help="ログを日時付きで書き出すファイル")
    parser.add_argument("-v", "--verbose", action="store_true", help="詳しいログを出す")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("roots", help="設定済みのルートと、カタログの状態を一覧する")
    p.set_defaults(func=cmd_roots)

    p = sub.add_parser("scan", help="ルートを走査してカタログを更新する（ファイルには書き込まない）")
    p.add_argument("roots", nargs="*", metavar="ROOT", help="ルート名かフォルダのパス（省略時は全ルート）")
    p.add_argument(
        "--accept-missing",
        action="store_true",
        help="一度に大量のファイルが消えて見えても、保留せずに消えたと確定する",
    )
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("hash", help="変わったファイルだけ内容ハッシュ（SHA-256）を計算する")
    p.add_argument("roots", nargs="*", metavar="ROOT", help="ルート名かフォルダのパス（省略時は全ルート）")
    p.set_defaults(func=cmd_hash)

    p = sub.add_parser("extract", help="ハッシュを計算済みのファイルから本文を抽出し、全文検索の索引に入れる")
    p.add_argument("roots", nargs="*", metavar="ROOT", help="ルート名かフォルダのパス（省略時は全ルート）")
    p.add_argument("--limit", type=int, default=None, help="この回に抽出する件数の上限（書類・新しいものから）")
    p.add_argument("--no-ocr", action="store_true", help="tesseract があっても OCR を使わない（スキャンは後で読み直す）")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("search", help="名前・本文・種類・タグで探す")
    p.add_argument("words", nargs="*", metavar="WORD", help="名前やフォルダに含む語（すべて含むもの）")
    p.add_argument("-t", "--text", default=None, help="本文に含む語（空白区切りで、すべて含むもの）")
    p.add_argument("-k", "--kind", choices=sorted(KINDS), default=None, help="種類で絞り込む")
    p.add_argument("--tag", action="append", metavar="TAG", help="付いているタグ（複数指定可。フォルダのタグは配下にも効く）")
    p.add_argument("--not", dest="exclude", action="append", metavar="TAG", help="付いていないタグ（複数指定可）")
    p.add_argument("--any", action="store_true", help="--tag のどれかを含むもの（既定はすべて含むもの）")
    p.add_argument("--folders", action="store_true", help="フォルダも結果に出す")
    p.add_argument("-r", "--root", action="append", metavar="ROOT", help="探すルート（複数指定可、省略時は全ルート）")
    p.add_argument("--limit", type=int, default=50, help="表に載せる件数")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("tags", help="タグ（件数・別名・親）・属性の定義・保存した検索を一覧する")
    p.set_defaults(func=cmd_tags)

    p = sub.add_parser("tag", help="ファイル・フォルダにタグを付ける・外す")
    p.add_argument("root", metavar="ROOT", help="ルート名")
    p.add_argument("paths", nargs="+", metavar="PATH", help="ルートからの相対パスか、ルートの中の絶対パス")
    p.add_argument("--add", action="append", metavar="TAG", help="付けるタグ（複数指定可。別名は正式な名前になる）")
    p.add_argument("--remove", action="append", metavar="TAG", help="外すタグ（複数指定可）")
    p.set_defaults(func=cmd_tag)

    p = sub.add_parser("alias", help="タグの別名を登録する（既に使われている名前なら、正式な名前にまとめる）")
    p.add_argument("tag", metavar="TAG", help="正式な名前")
    p.add_argument("alias", metavar="ALIAS", help="別名")
    p.add_argument("--remove", action="store_true", help="別名の登録を外す（TAG は何でもよい）")
    p.set_defaults(func=cmd_alias)

    p = sub.add_parser("parent", help="タグの親子を登録する（子が付いたものは、親のタグで探しても見つかる）")
    p.add_argument("tag", metavar="TAG", help="子のタグ")
    p.add_argument("parent", metavar="PARENT", help="親のタグ")
    p.add_argument("--remove", action="store_true", help="親子の登録を外す")
    p.set_defaults(func=cmd_parent)

    p = sub.add_parser("attr", help="型付きの属性を定義する・値を付ける")
    attr_sub = p.add_subparsers(dest="attr_command", required=True, metavar="ACTION")
    q = attr_sub.add_parser("define", help="属性を定義する（同じ名前なら定義し直す）")
    q.add_argument("name", metavar="NAME", help="属性の名前（例: 対応状態）")
    q.add_argument("type", choices=["choice", "flag", "date", "number", "text"], help="型（choice=択一・flag・date・number・text）")
    q.add_argument("--choices", default="", help="択一の選択肢（カンマ区切り。例: 未対応,対応中,対応済み）")
    q = attr_sub.add_parser("undefine", help="属性の定義を消す（付いた値もすべて外れる）")
    q.add_argument("name", metavar="NAME", help="属性の名前")
    q = attr_sub.add_parser("set", help="属性の値を付ける（同じ名前の値は置き換わる）")
    q.add_argument("root", metavar="ROOT", help="ルート名")
    q.add_argument("name", metavar="NAME", help="属性の名前")
    q.add_argument("value", metavar="VALUE", help="値（フラグなら true、日付は YYYY-MM-DD）")
    q.add_argument("paths", nargs="+", metavar="PATH", help="ルートからの相対パスか、ルートの中の絶対パス")
    q = attr_sub.add_parser("unset", help="属性の値を外す")
    q.add_argument("root", metavar="ROOT", help="ルート名")
    q.add_argument("name", metavar="NAME", help="属性の名前")
    q.add_argument("paths", nargs="+", metavar="PATH", help="ルートからの相対パスか、ルートの中の絶対パス")
    p.set_defaults(func=cmd_attr)

    p = sub.add_parser("relate", help="2つのアイテムの間に関係を張る（例: 新しい版 → 前の版）")
    p.add_argument("root", metavar="ROOT", help="ルート名")
    p.add_argument("src", metavar="FROM", help="関係の元（例: 新しい版）")
    p.add_argument("dst", metavar="TO", help="関係の先（例: 前の版）")
    p.add_argument("--kind", default="関連", help="関係の種類（例: 前の版・派生元・添付先。既定: 関連）")
    p.add_argument("--remove", action="store_true", help="関係を外す")
    p.set_defaults(func=cmd_relate)

    p = sub.add_parser("export", help="管理情報の全量（タグ・属性・関係・辞書・保存した検索）を JSONL で書き出す")
    p.add_argument("-o", "--output", type=Path, default=None, help="書き出すファイル（省略時は標準出力）")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import", help="export で書き出した JSONL を読み込む（既にあるものは飛ばす）")
    p.add_argument("file", type=Path, metavar="FILE", help="読み込む JSONL")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("report", help="整理候補（置く価値の薄いもの・重複）を報告する")
    p.add_argument("roots", nargs="*", metavar="ROOT", help="ルート名かフォルダのパス（省略時は全ルート）")
    p.add_argument("--json", type=Path, default=None, help="報告を JSON で書き出すファイル")
    p.add_argument("--examples", type=int, default=3, help="カテゴリごとに載せる例の数")
    p.add_argument("--top", type=int, default=15, help="直下のファイルが多いフォルダを何件載せるか")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("plan", help="整理候補を隔離するプラン（TOML）を書き出す（ファイルは動かさない）")
    p.add_argument("root", metavar="ROOT", help="ルート名かフォルダのパス")
    p.add_argument(
        "--category",
        action="append",
        metavar="CAT",
        help=f"含めるカテゴリ（複数指定可、省略時はすべて）: {', '.join(rules.CATEGORY_KEYS)}",
    )
    p.add_argument("-o", "--output", type=Path, default=None, help="プランの書き出し先（既定: カタログと同じフォルダの plans/）")
    p.add_argument(
        "--include-review",
        action="store_true",
        help="中身の確認が要る候補（元のファイルと内容が異なる競合コピーなど）もプランに含める",
    )
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("apply", help="プランを確認する。--yes を付けると実行し、対象を隔離フォルダへ移す")
    p.add_argument("plan", type=Path, metavar="PLAN", help="プランのファイル（TOML）")
    p.add_argument("--yes", action="store_true", help="確認だけでなく、実際に実行する")
    p.add_argument("--no-snapshot", action="store_true", help="設定があっても、前後のスナップショットを撮らない")
    p.add_argument("--limit", type=int, default=30, help="表に載せる件数")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("undo", help="実行したプランを取り消し、隔離したものを元の場所へ戻す")
    p.add_argument("plan", metavar="PLAN", help="プラン ID か、プランのファイル")
    p.add_argument("--yes", action="store_true", help="確認だけでなく、実際に元に戻す")
    p.add_argument("--no-snapshot", action="store_true", help="設定があっても、前後のスナップショットを撮らない")
    p.add_argument("--limit", type=int, default=30, help="表に載せる件数")
    p.set_defaults(func=cmd_undo)

    p = sub.add_parser("purge", help="プランで隔離したものを完全に消す（隔離を空にする。取り消せなくなる）")
    p.add_argument("plan", metavar="PLAN", help="プラン ID か、プランのファイル")
    p.add_argument("--yes", action="store_true", help="確認だけでなく、実際に完全に削除する")
    p.add_argument("--no-snapshot", action="store_true", help="設定があっても、前後のスナップショットを撮らない")
    p.add_argument("--limit", type=int, default=30, help="表に載せる件数")
    p.set_defaults(func=cmd_purge)

    p = sub.add_parser("sync", help="削除の反映に失敗して止まったままの同期を、再試行するか再開する")
    p.add_argument("plan", metavar="PLAN", help="プラン ID か、プランのファイル")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--retry", action="store_true", help="削除の反映をやり直し、成功したら常駐の同期を再開する")
    g.add_argument("--resume", action="store_true", help="反映をあきらめて、常駐の同期だけを再開する")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("serve", help="Web の画面を開く（既定は http://127.0.0.1:8090/）")
    p.add_argument("--host", default="127.0.0.1", help="待ち受けるアドレス（既定: 127.0.0.1）")
    p.add_argument("--port", type=int, default=8090, help="待ち受けるポート（既定: 8090）")
    p.add_argument(
        "--auth-file",
        type=Path,
        default=None,
        help="BASIC 認証の資格情報（user:password の1行）を書いたファイル。環境変数 TAG_KEEPER_AUTH でも指定できる",
    )
    p.add_argument(
        "--allow-no-auth",
        action="store_true",
        help="ループバック以外で待ち受けるときも、認証なしを許す（信頼できるネットワークに限ること）",
    )
    p.set_defaults(func=cmd_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI の入口。終了コードを返す（0: 成功、1: 確定を保留した・飛ばした操作がある、2: エラー）。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.log_file, args.verbose)
    args.db = args.db or default_db_path()
    try:
        config = load_config(args.config)
        return int(args.func(args, config))
    except (ConfigError, PlanError, SnapshotError, SyncError, TagError) as e:
        log.error("%s", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
