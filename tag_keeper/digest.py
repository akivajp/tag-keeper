"""定期レポート（要件 F-UI-3）。今の状況を Markdown にまとめる（Obsidian の Vault などで読む想定）。

載せるもの（ルートごと）: 走査の鮮度・消失の保留・移動の確認待ち、受け皿の新着、整理の候補（カテゴリごと）、構造の見直しの件数、
確認待ちの読み取りの結果、スナップショットの見張り。最後に、隔離フォルダに残っているもの。
読むだけで、カタログにもツリーにも書き込まない。
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any

from tag_keeper import analyze, organize, rules
from tag_keeper.config import Config
from tag_keeper.plan import list_plans
from tag_keeper.report import structure_issues, user_report
from tag_keeper.scan import relink_candidates


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}PB"


def _md(text: str) -> str:
    """Markdown の表やリストを壊さないよう、| と改行を逃がす。"""
    return text.replace("|", "\\|").replace("\n", " ")


def build(
    conn: sqlite3.Connection,
    config: Config,
    store: Any,
    *,
    plans_dir: Path,
    journal_dir: Path,
    watch: Any = None,
    days: int = 7,
    now: dt.datetime | None = None,
) -> str:
    """レポートの Markdown を作る。

    Args:
        conn: カタログへの接続。
        config: 設定。
        store: タグのログ（TagStore）。
        plans_dir: プランの置き場所。
        journal_dir: 実行記録の置き場所。
        watch: スナップショットの見張り（snapwatch.Watch）。無ければ載せない。
        days: 「新着」とする日数。
        now: 今の日時（テスト用）。
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(days=days)).isoformat(timespec="seconds")
    lines = [f"# tag-keeper のレポート（{now.astimezone():%Y-%m-%d %H:%M}）", ""]
    for rc in config.roots:
        lines += [f"## {rc.name}", ""]
        row = conn.execute("SELECT id FROM roots WHERE name = ?", (rc.name,)).fetchone()
        if row is None:
            lines += ["まだ走査していません。", ""]
            continue
        root_id = int(row["id"])
        scan = conn.execute("SELECT finished_at FROM scans WHERE root_id = ? ORDER BY id DESC LIMIT 1", (root_id,)).fetchone()
        (files,) = conn.execute("SELECT COUNT(*) FROM entries WHERE root_id = ? AND gone_at IS NULL AND is_dir = 0", (root_id,)).fetchone()
        (held,) = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE root_id = ? AND gone_at IS NULL AND is_dir = 0 AND missing_count > 0", (root_id,)
        ).fetchone()
        relink = len(relink_candidates(conn, root_id, days=config.scan.relink_days, skipped=store.relink_skipped(rc.name))) if config.scan.relink_days else 0
        review = len(analyze.review_items(conn, root_id, rc.name, store.reviewed(rc.name)))
        lines += [
            f"- ファイル {files:,} 件・最後の走査 {dt.datetime.fromisoformat(scan['finished_at']).astimezone():%Y-%m-%d %H:%M}" if scan else "- 走査の記録がありません",
        ]
        if held:
            lines.append(f"- 消えたとの確定を保留しているファイル: {held:,} 件")
        if relink:
            lines.append(f"- 移動の確認待ち: {relink:,} 件（画面の「移動の確認」）")
        if review:
            lines.append(f"- AI の読み取りの確認待ち: {review:,} 件（画面の「確認」）")
        lines.append("")

        # 受け皿の新着
        inbox, inbox_files = organize.find_inbox(conn, root_id, config.organize.inbox_patterns, [], [p for p, t in store.dir_types_of(rc.name).items() if t == "inbox"])
        new_rows = conn.execute(
            "SELECT relpath, size FROM entries WHERE root_id = ? AND gone_at IS NULL AND is_dir = 0 AND first_seen >= ? ORDER BY first_seen DESC",
            (root_id, since),
        ).fetchall()
        in_inbox = {f.relpath for f in inbox_files}
        new_inbox = [r for r in new_rows if r["relpath"] in in_inbox]
        lines += [f"### 受け皿の新着（直近 {days} 日: {len(new_inbox):,} 件・受け皿全体 {len(inbox_files):,} 件）", ""]
        lines += [f"- {_md(r['relpath'])}（{_size(r['size'])}）" for r in new_inbox[:20]] or ["- なし"]
        if len(new_inbox) > 20:
            lines.append(f"- ほか {len(new_inbox) - 20:,} 件")
        lines.append("")

        # 整理の候補と構造の見直し
        report = user_report(conn, root_id, rc.name, config, store)
        by_cat: dict[str, list[Any]] = {}
        for f in report.findings:
            by_cat.setdefault(f.category, []).append(f)
        lines += ["### 整理の候補", ""]
        if by_cat:
            lines += ["| カテゴリ | 件数 | 容量 |", "|---|---:|---:|"]
            for cat in rules.CATEGORY_ORDER:
                if cat in by_cat:
                    items = by_cat[cat]
                    lines.append(f"| {cat} | {len(items):,} | {_size(sum(f.size for f in items))} |")
        else:
            lines.append("- なし")
        lines.append("")
        issues = structure_issues(conn, root_id, rc.name, config, store, report)
        if issues:
            counts: dict[str, int] = {}
            for i in issues:
                counts[i.to_dict()["label"]] = counts.get(i.to_dict()["label"], 0) + 1
            lines += ["### 構造の見直しの提案", ""] + [f"- {k}: {v:,} 件" for k, v in counts.items()] + [""]

        # スナップショットの見張り
        if watch is not None:
            latest = watch.latest(rc.name)
            alerts = [a for a in watch.alerts(rc.name) if not a.get("protected")]
            if latest or alerts:
                lines += ["### スナップショットの見張り", ""]
                if latest:
                    lines.append(f"- 最新の比較（#{latest['older']} → #{latest['newer']}）: 消えた {latest['deleted']:,}・変わった {latest['changed']:,}・増えた {latest['added']:,}")
                for a in alerts:
                    lines.append(f"- ⚠ #{a['older']} → #{a['newer']}: {a['alert']}（守るなら、ルートの画面で自動削除の対象から外せます）")
                lines.append("")

    # 隔離フォルダ
    plans = [p for p in list_plans(plans_dir, journal_dir) if p.quarantined]
    lines += ["## 隔離フォルダ", ""]
    if plans:
        lines.append(f"合計 {_size(sum(p.quarantined_size for p in plans))}（プランを開くと、取り消すか隔離を空にできます）")
        lines += [f"- {p.id}: {p.quarantined:,} 件・{_size(p.quarantined_size)}" for p in plans]
    else:
        lines.append("- 空です")
    lines.append("")
    return "\n".join(lines)


def write(path: Path, text: str) -> None:
    """レポートを書く（途中まで書いた状態を残さないよう、別名で書いてから置き換える）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
