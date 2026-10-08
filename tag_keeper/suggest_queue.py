"""1件ずつの整理の提案の待ち行列（画面でファイルを選ぶたびに、すぐ提案を出すため）。

- 画面で選んだファイルは先頭に割り込ませ、先読み（次の数件）は後ろに並べる。
- 裏の1本のスレッドが順に処理する。ollama へは同時に1件だけ問い合わせる。
- 提案の材料（移動先の候補の索引・過去の例・既存のタグ）はルートごとに作り、しばらく使い回す。
  判断のログやタグが増えたら作り直す（採用した判断が、次の提案にすぐ効くように）。
- 結果はカタログに保存する（organize.save）。画面は状態を問い合わせ、できたら保存済みの提案を読む。
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tag_keeper import organize, rules
from tag_keeper.catalog import connect
from tag_keeper.config import Config
from tag_keeper.decisions import DecisionLog
from tag_keeper.report import build_report
from tag_keeper.tags import TagStore

log = logging.getLogger(__name__)

# 提案の材料を使い回す時間（秒）
CONTEXT_TTL = 300.0
# 終わった要求の状態を覚えておく時間（秒）
KEEP_FINISHED = 1800.0


@dataclass
class Request:
    """提案の要求1件の状態。"""

    root: str
    path: str
    model: str
    force: bool = False
    prefetch: bool = False
    state: str = "queued"  # queued / running / done / error
    error: str = ""
    updated: float = field(default_factory=time.time)

    def to_dict(self, position: int | None = None) -> dict[str, Any]:
        """画面用の辞書。"""
        return {
            "root": self.root,
            "path": self.path,
            "model": self.model,
            "state": self.state,
            "error": self.error,
            "prefetch": self.prefetch,
            "position": position,
        }


class SuggestQueue:
    """提案の待ち行列と、それを処理する裏のスレッド。"""

    def __init__(
        self,
        db_path: Path,
        load_config: Callable[[], Config],
        decisions: Callable[[Config], DecisionLog],
        tags: Callable[[Config], TagStore],
    ) -> None:
        self.db_path = db_path
        self.load_config = load_config
        self.decisions = decisions
        self.tags = tags
        self.cond = threading.Condition()
        self.requests: OrderedDict[tuple[str, str, str], Request] = OrderedDict()
        self.order: list[tuple[str, str, str]] = []  # 待っている要求の順
        self._contexts: dict[str, tuple[float, tuple[Any, ...], organize.SuggestContext]] = {}
        self._checked: set[str] = set()
        self._thread: threading.Thread | None = None

    # --- 要求 ---

    def enqueue(self, root: str, path: str, model: str, *, force: bool = False, prefetch: bool = False) -> Request:
        """提案を要求する。既に待っている・終わっているものは、そのまま状態を返す。"""
        key = (root, path, model)
        with self.cond:
            self._forget_old()
            req = self.requests.get(key)
            if req is not None and not force and req.state in ("queued", "running", "done"):
                # 画面で選ばれた（先読みでない）なら、先頭へ繰り上げる
                if req.state == "queued" and not prefetch and key in self.order:
                    self.order.remove(key)
                    self.order.insert(0, key)
                    req.prefetch = False
                return req
            req = Request(root, path, model, force=force, prefetch=prefetch)
            self.requests[key] = req
            if key in self.order:
                self.order.remove(key)
            if prefetch:
                self.order.append(key)
            else:
                self.order.insert(0, key)
            self._ensure_thread()
            self.cond.notify()
            return req

    def status(self, root: str | None = None) -> list[dict[str, Any]]:
        """要求の状態の一覧（待っているものは順番つき）。"""
        with self.cond:
            out = []
            for key, req in self.requests.items():
                if root is not None and req.root != root:
                    continue
                pos = self.order.index(key) + 1 if key in self.order else None
                out.append(req.to_dict(pos))
            return out

    def _forget_old(self) -> None:
        now = time.time()
        for key in [k for k, r in self.requests.items() if r.state in ("done", "error") and now - r.updated > KEEP_FINISHED]:
            del self.requests[key]

    # --- 処理 ---

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="suggest-queue", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        conn = connect(self.db_path)
        while True:
            with self.cond:
                while not self.order:
                    self.cond.wait()
                key = self.order.pop(0)
                req = self.requests[key]
                req.state, req.updated = "running", time.time()
            try:
                sug = self._process(conn, req)
                with self.cond:
                    req.state = "error" if sug.error else "done"
                    req.error = sug.error
                    req.updated = time.time()
            except Exception as e:  # noqa: BLE001 - 裏のスレッドを止めないため、すべて受け止めて画面に見せる
                log.exception("提案を作れませんでした: %s", req.path)
                with self.cond:
                    req.state, req.error, req.updated = "error", str(e), time.time()

    def _context(self, conn: Any, config: Config, root_name: str, root_id: int, oc: Any) -> organize.SuggestContext:
        """提案の材料を、ルートごとに使い回す（判断のログ・タグ・走査が変わったら作り直す）。"""
        decisions = self.decisions(config)
        tags = self.tags(config)
        scan_id = conn.execute("SELECT MAX(id) FROM scans WHERE root_id = ?", (root_id,)).fetchone()[0]
        log_stamp = tuple((f.name, f.stat().st_size) for f in sorted(decisions.log_dir.glob("*.jsonl"))) if decisions.log_dir.exists() else ()
        stamp = (scan_id, log_stamp, sum(tags.all_tags().values()))
        hit = self._contexts.get(root_name)
        if hit is not None and hit[1] == stamp and time.time() - hit[0] < CONTEXT_TTL:
            return hit[2]
        rc = config.find_root(root_name)
        assert rc is not None
        report = build_report(conn, root_id, rc.name, config.hygiene)
        excluded = [f.relpath for f in report.findings if f.is_dir and f.category in (rules.CAT_APP_DATA, rules.CAT_REGENERABLE)]
        ctx = organize.build_context(conn, root_id, rc.name, oc, excluded, decisions=decisions, tag_counts=tags.all_tags())
        self._contexts[root_name] = (time.time(), stamp, ctx)
        return ctx

    def _process(self, conn: Any, req: Request) -> organize.Suggestion:
        config = self.load_config()
        rc = config.find_root(req.root)
        if rc is None:
            return organize.Suggestion(req.path, "", req.model, error=f"設定ファイルにルートがありません: {req.root}")
        row = conn.execute("SELECT id FROM roots WHERE name = ?", (rc.name,)).fetchone()
        if row is None:
            return organize.Suggestion(req.path, "", req.model, error=f"まだ走査していません: {rc.name}")
        root_id = int(row["id"])
        item = organize.catalog_item(conn, root_id, req.path)
        if item is None:
            return organize.Suggestion(req.path, "", req.model, error="カタログにありません（新しいファイルなら、走査してから選んでください）")
        oc = dataclasses.replace(config.organize, model=req.model)
        if req.model not in self._checked:
            organize.check_ollama(oc)
            self._checked.add(req.model)
        log.info("提案を作ります: %s（%s）", req.path, req.model)
        sug = organize.suggest_one(
            conn, root_id, rc.path, item, oc, lambda: self._context(conn, config, rc.name, root_id, oc), force=req.force
        )
        if sug.error:
            log.warning("提案を作れませんでした: %s: %s", req.path, sug.error)
        return sug
