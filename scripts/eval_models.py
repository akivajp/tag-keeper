"""受け皿の整理の提案を、複数のモデル（とエフォート）で作って比べる。

実際のカタログの写しを使い、本物のカタログ・タグのログ・利用の記録には書き込まない（ファイルも動かさない）。
同じファイルの組に対して、モデルごとに提案を作り、結果・かかった秒数・トークン数・料金の見積もりを JSONL に書き出す。

    uv run --extra claude python scripts/eval_models.py --out runs/eval-20261116 \\
        --models claude-haiku-5-5:low@anthropic claude-sonnet-5-5:low@anthropic --n 20

- 対象は、--reference のモデルの提案が保存済みのファイルを優先し、拡張子ごとに偏らないように選ぶ（--seed で固定）
- 料金のかかるモデルを含むときは、始める前に見積もりを出す。--max-usd を超えたら、そこで止める
- 途中で止めても、同じ --out で動かし直せば、済んだ分は飛ばして続きから始める
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn, TimeRemainingColumn
from rich.table import Table

from tag_keeper import claude, dirtypes, organize, rules
from tag_keeper.catalog import connect
from tag_keeper.config import Config, default_config_path, default_db_path, load_config
from tag_keeper.decisions import DecisionLog
from tag_keeper.report import build_report
from tag_keeper.tags import TagStore

log = logging.getLogger("eval_models")
console = Console()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True, help="結果を書き出すフォルダ（results.jsonl・usage.jsonl・eval.log・catalog.db の写し）")
    ap.add_argument("--models", nargs="+", required=True, help="比べるモデル（例: gemma4:12b claude-haiku-5-5:low@anthropic）")
    ap.add_argument("--n", type=int, default=20, help="対象のファイル数（既定: 20）")
    ap.add_argument("--seed", type=int, default=20261116, help="対象を選ぶ乱数の種（既定: 20261116）")
    ap.add_argument("--root", default=None, help="ルート名（省略時は設定の最初のルート）")
    ap.add_argument("--reference", default="gemma4:31b-cloud", help="保存済みの提案を比較の基準にするモデル（既定: gemma4:31b-cloud）")
    ap.add_argument("--paths", nargs="*", default=None, help="対象のファイルを名指しする（ルートからの相対パス。--n と --seed は使わない）")
    ap.add_argument("--paths-file", type=Path, default=None, help="対象のファイルを1行に1つ書いたファイル（--paths と同じ）")
    ap.add_argument("--exclude", nargs="*", default=[], help="対象から外すフォルダ（ルートからの相対パス。配下ごと。外部へ送りたくない受け皿など）")
    ap.add_argument("--max-usd", type=float, default=5.0, help="料金の見積もりの合計がこれを超えたら止める（既定: 5.0）")
    ap.add_argument("--config", type=Path, default=None, help=f"設定ファイル（既定: {default_config_path()}）")
    ap.add_argument("--db", type=Path, default=None, help=f"写しの元にするカタログ（既定: {default_db_path()}）")
    ap.add_argument("--log-file", type=Path, default=None, help="ログのファイル（既定: <out>/eval.log）")
    ap.add_argument("--yes", action="store_true", help="料金の見積もりを確かめずに始める")
    return ap.parse_args(argv)


def setup_logging(log_file: Path) -> None:
    """画面（rich）と、日時付きのログのファイルの両方に出す。"""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[RichHandler(console=Console(stderr=True), show_path=False), fh])


def copy_catalog(src: Path, dst: Path) -> None:
    """カタログの写しを作る（動いているサービスが書いている途中でも、整合した写しになる）。"""
    if dst.exists():
        return
    a = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    b = sqlite3.connect(dst)
    try:
        a.backup(b)
    finally:
        a.close()
        b.close()


def pick_files(conn: sqlite3.Connection, files: list[organize.InboxFile], reference: str, n: int, seed: int) -> list[organize.InboxFile]:
    """対象を選ぶ。基準のモデルの提案があるものを優先し、拡張子ごとに順に取って偏りを抑える。"""
    have = {r["sha256"] for r in conn.execute("SELECT sha256 FROM suggestions WHERE model = ?", (reference,))}
    rng = random.Random(seed)
    by_ext: dict[str, list[organize.InboxFile]] = defaultdict(list)
    for f in sorted(files, key=lambda f: f.relpath):
        if f.sha256:
            by_ext[Path(f.relpath).suffix.lower()].append(f)
    for group in by_ext.values():
        rng.shuffle(group)
        # 基準の提案があるものを前へ
        group.sort(key=lambda f: f.sha256 not in have)
    out: list[organize.InboxFile] = []
    # 件数の多い拡張子から順に、1つずつ取る
    order = sorted(by_ext, key=lambda e: (-len(by_ext[e]), e))
    while len(out) < n and any(by_ext.values()):
        for ext in order:
            if by_ext[ext] and len(out) < n:
                out.append(by_ext[ext].pop(0))
    return out


def estimate_usd(model: str, n: int) -> float | None:
    """始める前の、料金のおおよその見積もり（1ファイルあたり 入力 5,000・出力 2,500 トークンと置く。2026-11-16 の実測に基づく）。"""
    parts = claude.split(model)
    if parts is None:
        return 0.0
    cost = claude.cost_usd(parts[0], 5000, 2500)
    return None if cost is None else cost * n


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(args.log_file or out_dir / "eval.log")
    config: Config = load_config(args.config)
    rc = config.find_root(args.root) if args.root else (config.roots[0] if config.roots else None)
    if rc is None:
        log.error("ルートがありません")
        return 2

    # 本物には書き込まない: カタログは写し、利用の記録はこのフォルダの中へ
    db = out_dir / "catalog.db"
    copy_catalog(args.db or default_db_path(), db)
    usage_file = out_dir / "usage.jsonl"
    claude.usage_path = lambda: usage_file  # type: ignore[assignment]
    conn = connect(db)
    root_id = int(conn.execute("SELECT id FROM roots WHERE name = ?", (rc.name,)).fetchone()["id"])
    store = TagStore(config.tags.dir)
    types = store.dir_types_of(rc.name)
    report = build_report(conn, root_id, rc.name, config.hygiene)
    excluded = [f.relpath for f in report.findings if f.is_dir and f.category in (rules.CAT_APP_DATA, rules.CAT_REGENERABLE)]
    excluded += dirtypes.closed_dirs(conn, root_id, types)
    inbox_dirs = dirtypes.inbox_dirs(types)
    _inbox, files = organize.find_inbox(conn, root_id, config.organize.inbox_patterns, excluded, inbox_dirs)
    if args.paths_file:
        args.paths = [p for p in args.paths_file.read_text(encoding="utf-8").splitlines() if p.strip()]
    if args.paths is not None and not args.paths:
        # 名指しの指定が空のまま、無作為の選び方に落ちて意図しないファイルを送らないようにする
        log.error("--paths / --paths-file の対象がありません")
        return 2
    if args.paths:
        items = [it for it in (organize.catalog_item(conn, root_id, p) for p in args.paths) if it is not None]
    else:
        skip = [e.strip("/") + "/" for e in args.exclude]
        files = [f for f in files if not any(f.relpath.startswith(e) for e in skip)]
        items = pick_files(conn, files, args.reference, args.n, args.seed)
    log.info("対象: %d 件（受け皿 %d 件のうち）／ モデル: %s", len(items), len(files), "、".join(args.models))

    # 料金の見積もり（始める前に出す）
    total_est = 0.0
    for m in args.models:
        est = estimate_usd(m, len(items))
        log.info("  %s: 見積もり %s", m, "料金表に無い" if est is None else f"約 {est:.2f} USD")
        total_est += est or 0.0
    log.info("見積もりの合計: 約 %.2f USD（上限 %.2f USD）", total_est, args.max_usd)
    if total_est > args.max_usd:
        log.error("見積もりが上限を超えています（--max-usd を上げるか、--n・--models を減らしてください）")
        return 2
    if total_est > 0 and not args.yes:
        if input("始めますか？ [y/N] ").strip().lower() != "y":
            return 1

    results_file = out_dir / "results.jsonl"
    done: set[tuple[str, str]] = set()
    if results_file.exists():
        for line in results_file.read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            if not d["suggestion"].get("error"):
                done.add((d["model"], d["path"]))

    decisions = DecisionLog(out_dir / "decisions")  # 空（採用の記録は使わない。全モデルを同じ条件にする）
    cards = store.dir_cards_of(rc.name)
    spent = sum(u.cost or 0.0 for u in claude.read_usage())
    with Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), TimeElapsedColumn(), TimeRemainingColumn(),
                  TextColumn("[dim]{task.fields[current]}"), console=Console(stderr=True)) as progress, results_file.open("a", encoding="utf-8") as out:
        for model in args.models:
            oc, note = organize.config_for_root(replace(config.organize, fallback=False), rc.allow_cloud, model)
            if note:
                log.warning("%s", note)
            try:
                organize.check_ollama(oc)
            except organize.OllamaError as e:
                log.error("%s は使えません: %s", model, e)
                continue
            tag_counts = organize.prompt_tag_counts(store, config.roots, oc)
            ctx = organize.build_context(conn, root_id, rc.name, oc, excluded, decisions=decisions, tag_counts=tag_counts, inbox_dirs=inbox_dirs, cards=cards)
            task = progress.add_task(model, total=len(items), current="")
            for item in items:
                progress.update(task, current=item.relpath[-40:])
                if (model, item.relpath) in done:
                    progress.advance(task)
                    continue
                before = len(claude.read_usage())
                began = time.monotonic()
                sug = organize.suggest_one(conn, root_id, rc.path, item, oc, lambda: ctx, force=True)
                seconds = time.monotonic() - began
                used = claude.read_usage()[before:]
                cost = sum(u.cost or 0.0 for u in used)
                spent += cost
                out.write(json.dumps({
                    "model": model, "path": item.relpath, "sha256": item.sha256, "seconds": round(seconds, 2),
                    "calls": len(used), "input_tokens": sum(u.input_tokens for u in used), "output_tokens": sum(u.output_tokens for u in used),
                    "cost": round(cost, 6), "suggestion": asdict(sug),
                }, ensure_ascii=False) + "\n")
                out.flush()
                log.info("%s | %s → %s（%.1f 秒・%.4f USD）%s", model, item.relpath.rsplit("/", 1)[-1], sug.new_name or "-", seconds, cost,
                         f" 失敗: {sug.error}" if sug.error else "")
                progress.advance(task)
                if spent > args.max_usd:
                    log.error("料金の見積もりが上限（%.2f USD）を超えたので止めます（ここまで %.2f USD）", args.max_usd, spent)
                    summarize(results_file)
                    return 1
    summarize(results_file)
    return 0


def summarize(results_file: Path) -> None:
    """モデルごとの件数・秒数・トークン数・料金を表にする（質の採点は、結果を読んで別に行う）。"""
    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in results_file.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        rows[d["model"]].append(d)
    table = Table(title="モデルごとの結果")
    for name, justify in (("モデル", "left"), ("成功", "right"), ("失敗", "right"), ("秒/件", "right"), ("入力/件", "right"), ("出力/件", "right"),
                          ("USD/件", "right"), ("USD 合計", "right")):
        table.add_column(name, justify=justify)  # type: ignore[arg-type]
    for model, ds in rows.items():
        ok = [d for d in ds if not d["suggestion"].get("error")]
        n = max(1, len(ok))
        table.add_row(model, str(len(ok)), str(len(ds) - len(ok)), f"{sum(d['seconds'] for d in ok) / n:.1f}", f"{sum(d['input_tokens'] for d in ok) / n:,.0f}",
                      f"{sum(d['output_tokens'] for d in ok) / n:,.0f}", f"{sum(d['cost'] for d in ok) / n:.4f}", f"{sum(d['cost'] for d in ds):.4f}")
    console.print(table)


if __name__ == "__main__":
    sys.exit(main())
