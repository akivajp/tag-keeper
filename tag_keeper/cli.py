"""tag-keeper のコマンドライン。

    tag-keeper roots                 設定済みのルートと、カタログの状態を一覧する
    tag-keeper scan [ROOT ...]       ルートを走査してカタログを更新する（読み取り専用）
    tag-keeper hash [ROOT ...]       変わったファイルだけ内容ハッシュを計算する
    tag-keeper report [ROOT ...]     整理候補（置く価値の薄いファイル・重複）を報告する

ROOT には、設定ファイルのルート名か、フォルダのパスを指定する。省略すると設定済みの全ルート。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
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
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from tag_keeper import __version__
from tag_keeper.catalog import connect, ensure_root, last_scan
from tag_keeper.config import (
    Config,
    ConfigError,
    RootConfig,
    default_config_path,
    default_db_path,
    load_config,
)
from tag_keeper.hashing import hash_root
from tag_keeper.report import Report, build_report
from tag_keeper.scan import RootUnavailableError, scan_root

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
            try:
                res = scan_root(
                    conn,
                    root_id,
                    rc.path,
                    config.safety,
                    exclude=rc.exclude,
                    accept_missing=args.accept_missing,
                    on_progress=lambda n: progress.update(task, completed=n),
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
        report = build_report(conn, row["id"], rc.name, config.hygiene, top_fanout=args.top)
        render_report(report, args.examples)
        reports.append(report)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps([r.to_dict() for r in reports], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        log.info("JSON を書き出しました: %s", args.json)
    return 0


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

    p = sub.add_parser("report", help="整理候補（置く価値の薄いもの・重複）を報告する")
    p.add_argument("roots", nargs="*", metavar="ROOT", help="ルート名かフォルダのパス（省略時は全ルート）")
    p.add_argument("--json", type=Path, default=None, help="報告を JSON で書き出すファイル")
    p.add_argument("--examples", type=int, default=3, help="カテゴリごとに載せる例の数")
    p.add_argument("--top", type=int, default=15, help="直下のファイルが多いフォルダを何件載せるか")
    p.set_defaults(func=cmd_report)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI の入口。終了コードを返す（0: 成功、1: 確定を保留した、2: エラー）。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.log_file, args.verbose)
    args.db = args.db or default_db_path()
    try:
        config = load_config(args.config)
        return int(args.func(args, config))
    except ConfigError as e:
        log.error("%s", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
