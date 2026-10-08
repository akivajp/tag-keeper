"""利用者の判断の記録（提案の文脈、将来の学習データ。要件 F-AI-4）。

整理の提案に対して利用者が決めたこと（採用した名前・移動先・タグ、却下したタグ）を、
端末ごとの追記専用ログ（JSONL）に残す。モデルの提案と最終的な判断が対になっているので、
そのまま学習データとして書き出せる。提案を作るときは、似た過去の判断を例として指示文に添える。

ログの1行（イベント）:

    {"op": "accept", "root", "path", "sha256", "model", "info": {...}, "suggested": {...}, "final": {...}, "plan_id"}
    {"op": "tag",    "root", "path", "sha256", "model", "tag", "decision": "accept" | "reject"}
    {"op": "revert", "plan_id"}   # 採用して作ったプランを取り消した（その判断は例に使わない）

ログとは別に、受け皿から利用者が手作業で移したもの（走査で検出した移動）も、名前と移動先の例として使う。
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import threading
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tag_keeper.catalog import utcnow
from tag_keeper.textindex import TfIdf, bigrams

log = logging.getLogger(__name__)

# 例として使う類似度の下限（これより遠い例は、かえって提案を惑わせる）
MIN_SCORE = 0.12


class DecisionLog:
    """判断のログ（端末ごとの JSONL）。"""

    def __init__(self, log_dir: Path, host: str | None = None) -> None:
        self.log_dir = log_dir
        self.host = host or socket.gethostname()
        self.lock = threading.Lock()

    @property
    def log_path(self) -> Path:
        """この端末が追記するログ。"""
        return self.log_dir / f"{self.host}.jsonl"

    def _append(self, events: Sequence[dict[str, Any]]) -> None:
        if not events:
            return
        now = utcnow()
        with self.lock:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps({"at": now, "host": self.host, **ev}, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())

    def read(self) -> list[dict[str, Any]]:
        """全端末のログを日時順に読む（読めない行は飛ばす）。"""
        events: list[dict[str, Any]] = []
        if not self.log_dir.exists():
            return events
        for f in sorted(self.log_dir.glob("*.jsonl")):
            with f.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        events.sort(key=lambda e: str(e.get("at", "")))
        return events

    def accept(self, items: Iterable[dict[str, Any]]) -> int:
        """採用した提案を記録する。items の各要素は op を除いた accept のイベント。"""
        events = [{"op": "accept", **it} for it in items]
        self._append(events)
        return len(events)

    def tag(self, root: str, path: str, sha256: str | None, tag: str, decision: str, model: str = "") -> None:
        """提案されたタグを付けた（accept）か、却下した（reject）かを記録する。"""
        self._append([{"op": "tag", "root": root, "path": path, "sha256": sha256, "model": model, "tag": tag, "decision": decision}])

    def revert(self, plan_id: str) -> None:
        """採用して作ったプランを取り消したことを記録する。"""
        self._append([{"op": "revert", "plan_id": plan_id}])


@dataclass
class Example:
    """似た過去の判断1件。"""

    name: str  # 元の名前
    final_name: str
    dest_dir: str
    title: str = ""
    doc_type: str = ""
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    source: str = "accept"  # accept（提案を採用） / move（手作業の移動を走査で検出）

    def text(self) -> str:
        """検索に使う文字列。"""
        return " ".join([Path(self.name).stem, self.title, self.doc_type, self.summary, Path(self.final_name).stem])

    def line(self) -> str:
        """指示文に添える1行。"""
        about = "：".join(x for x in (self.doc_type, self.title) if x)
        tags = f" ／ タグ {', '.join(self.tags)}" if self.tags else ""
        return f"- 「{self.name}」{f'（{about}）' if about else ''} → 名前「{self.final_name}」 ／ 移動先「{self.dest_dir}」{tags}"


class ExampleIndex:
    """似た過去の判断の検索。"""

    def __init__(self, examples: list[Example]) -> None:
        self.examples = examples
        self.tfidf = TfIdf([Counter(bigrams(e.text())) for e in examples])

    def search(self, text: str, k: int = 5, min_score: float = MIN_SCORE) -> list[tuple[Example, float]]:
        """text に近い過去の判断を、上位 k 件まで返す。"""
        return [(self.examples[i], s) for i, s in self.tfidf.search(text) if s >= min_score][:k]


def _under_any(path: str, dirs: Iterable[str]) -> bool:
    return any(path == d or path.startswith(d + "/") for d in dirs)


def load_examples(
    decisions: DecisionLog,
    conn: sqlite3.Connection,
    root: str,
    root_id: int,
    inbox: Sequence[str],
) -> tuple[ExampleIndex, Counter[str]]:
    """過去の判断から例の索引を作る。あわせて、却下されたタグの回数を返す。

    - 提案を採用したもの（取り消したプランのものを除く）。同じ内容のファイルは最後の判断だけを使う
    - 受け皿から受け皿の外へ、利用者が手作業で移したもの（走査で検出した移動）
    """
    events = decisions.read()
    reverted = {e.get("plan_id") for e in events if e.get("op") == "revert"}
    by_key: dict[str, Example] = {}
    rejected: Counter[str] = Counter()
    for e in events:
        if e.get("root") != root:
            continue
        if e.get("op") == "accept" and e.get("plan_id") not in reverted:
            info = e.get("info") or {}
            final = e.get("final") or {}
            by_key[e.get("sha256") or e.get("path", "")] = Example(
                name=Path(str(e.get("path", ""))).name,
                final_name=str(final.get("name", "")),
                dest_dir=str(final.get("dest_dir", "")),
                title=str(info.get("title", "")),
                doc_type=str(info.get("doc_type", "")),
                summary=str(info.get("summary", "")),
                tags=[str(t) for t in final.get("tags") or []],
            )
        elif e.get("op") == "tag" and e.get("decision") == "reject":
            rejected[str(e.get("tag"))] += 1
    examples = list(by_key.values())
    for r in conn.execute(
        "SELECT old_relpath, new_relpath FROM moves WHERE root_id = ? AND source = 'scan' AND is_dir = 0 ORDER BY id",
        (root_id,),
    ):
        old, new = r["old_relpath"], r["new_relpath"]
        if _under_any(old, inbox) and not _under_any(new, inbox):
            examples.append(Example(Path(old).name, Path(new).name, new.rpartition("/")[0], source="move"))
    return ExampleIndex(examples), rejected
