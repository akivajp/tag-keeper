"""タグ（要件 F-MD-1・5・6・9、N-3・N-4）。

- 真実源は、端末ごとの追記専用ログ（JSONL）。<tags_dir>/<ホスト名>.jsonl に追記し、
  読むときは全端末のファイルを日時順に統合する。1つのファイルに複数の端末が書き込まないので、
  ログをクラウドで同期しても競合しない。ツールが無くなっても読める。
- アイテムは「ルート名 + ルートからの相対パス」で指す（"" はルート自身）。
- フォルダに付けたタグは、配下のアイテムが継承する（フォルダ由来タグ）。継承は計算で求め、保存しない。
- 付けたアイテム（またはその祖先のフォルダ）が動いたら、move の記録を足して追従する。

ログの1行（イベント）:

    {"at": "...", "host": "zefat", "op": "add",    "root": "onedrive", "path": "a/b.pdf", "tag": "会社:アティード", "source": "manual"}
    {"at": "...", "host": "zefat", "op": "remove", "root": "onedrive", "path": "a/b.pdf", "tag": "会社:アティード"}
    {"at": "...", "host": "zefat", "op": "move",   "root": "onedrive", "from": "a", "to": "c/a"}
    {"at": "...", "host": "zefat", "op": "rename", "from": "会社:ATID", "to": "会社:アティード"}   # タグの名前の変更（全ルート）
    {"at": "...", "host": "zefat", "op": "delete", "tag": "仮"}                                 # タグの削除（全ルート）
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tag_keeper.catalog import utcnow

log = logging.getLogger(__name__)

# タグの長さの上限（画面とログを壊さない程度の目安）
MAX_TAG_LENGTH = 100


class TagError(ValueError):
    """タグの名前が不正なときの例外。"""


def normalize_tag(tag: str) -> str:
    """タグの表記をそろえる（Unicode の NFC、前後の空白を除き、連続する空白を1つに）。

    名前空間は「会社:アティード」のようにコロンで区切る。コロンの前後の空白も詰める。

    Raises:
        TagError: 空、長すぎる、改行などの制御文字を含む。
    """
    t = unicodedata.normalize("NFC", tag)
    t = " ".join(t.split())
    t = ":".join(part.strip() for part in t.split(":"))
    if not t or t.startswith(":") or t.endswith(":"):
        raise TagError(f"タグの名前が空です: {tag!r}")
    if len(t) > MAX_TAG_LENGTH:
        raise TagError(f"タグの名前が長すぎます（{MAX_TAG_LENGTH} 文字まで）: {t[:20]}…")
    if any(unicodedata.category(ch).startswith("C") for ch in t):
        raise TagError(f"タグの名前に制御文字は使えません: {tag!r}")
    return t


def _ancestors(path: str) -> list[str]:
    """祖先のフォルダ（ルート "" から近い順ではなく、ルートから順に）。自身は含まない。"""
    if path == "":
        return []
    parts = path.split("/")
    return [""] + ["/".join(parts[:i]) for i in range(1, len(parts))]


def _under(path: str, base: str) -> bool:
    """path が base 自身か、その配下か。"""
    return base == "" or path == base or path.startswith(base + "/")


@dataclass(frozen=True)
class Assignment:
    """アイテムに直接付いたタグ1件。"""

    tag: str
    source: str
    at: str
    host: str


class TagStore:
    """タグのログを読み込んで保持し、付け外しをログに追記する。

    ログのファイルが変わっていれば（他の端末から同期されてきた場合など）、読み出しの前に読み直す。
    """

    def __init__(self, tags_dir: Path, host: str | None = None) -> None:
        self.tags_dir = tags_dir
        self.host = host or socket.gethostname()
        self.lock = threading.RLock()
        # (ルート名, 相対パス) → {タグ → Assignment}
        self.items: dict[tuple[str, str], dict[str, Assignment]] = {}
        self._stamp: tuple[tuple[str, int, int], ...] = ()

    @property
    def log_path(self) -> Path:
        """この端末が追記するログ。"""
        return self.tags_dir / f"{self.host}.jsonl"

    # --- 読み込み ---

    def _files(self) -> list[Path]:
        return sorted(self.tags_dir.glob("*.jsonl")) if self.tags_dir.exists() else []

    def reload_if_changed(self) -> None:
        """ログのファイルが前回の読み込みから変わっていれば、読み直す。"""
        with self.lock:
            stamp = tuple((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in self._files())
            if stamp == self._stamp:
                return
            events: list[dict[str, Any]] = []
            for f in self._files():
                with f.open(encoding="utf-8") as fh:
                    for n, line in enumerate(fh, 1):
                        if not line.strip():
                            continue
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            # 同期の途中などで最後の行が欠けていることがある。読める行だけ使う
                            log.warning("タグのログの %s 行目を読めません: %s", n, f)
            # 端末をまたいで日時順に並べる（同じ日時ならファイル内の順を保つ）
            events.sort(key=lambda e: str(e.get("at", "")))
            self.items = {}
            for ev in events:
                self._apply(ev)
            self._stamp = stamp

    def _apply(self, ev: dict[str, Any]) -> None:
        """イベント1件を、保持している状態に反映する。"""
        op = ev.get("op")
        root = str(ev.get("root", ""))
        if op == "add":
            key = (root, str(ev["path"]))
            self.items.setdefault(key, {})[ev["tag"]] = Assignment(
                ev["tag"], str(ev.get("source", "manual")), str(ev.get("at", "")), str(ev.get("host", ""))
            )
        elif op == "remove":
            key = (root, str(ev["path"]))
            tags = self.items.get(key)
            if tags is not None:
                tags.pop(ev["tag"], None)
                if not tags:
                    del self.items[key]
        elif op == "rename":
            # タグの名前の変更。変更先が既に付いているアイテムでは、1つにまとめる
            src, dst = str(ev["from"]), str(ev["to"])
            for tags in self.items.values():
                a = tags.pop(src, None)
                if a is not None and dst not in tags:
                    tags[dst] = Assignment(dst, a.source, a.at, a.host)
        elif op == "delete":
            tag = str(ev["tag"])
            for key in [k for k, tags in self.items.items() if tag in tags]:
                del self.items[key][tag]
                if not self.items[key]:
                    del self.items[key]
        elif op == "move":
            src, dst = str(ev["from"]), str(ev["to"])
            moved = [k for k in self.items if k[0] == root and _under(k[1], src)]
            entries = {k: self.items.pop(k) for k in moved}
            for (r, p), tags in entries.items():
                new = dst + p[len(src):]
                self.items.setdefault((r, new), {}).update(tags)

    # --- 書き込み ---

    def _append(self, events: Sequence[dict[str, Any]]) -> None:
        """イベントをこの端末のログに追記し、保持している状態にも反映する。"""
        if not events:
            return
        with self.lock:
            self.reload_if_changed()
            self.tags_dir.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            for ev in events:
                self._apply(ev)
            files = self._files()
            self._stamp = tuple((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in files)

    def add(self, root: str, paths: Iterable[str], tag: str, source: str = "manual") -> int:
        """アイテムにタグを付ける。既に付いているものは飛ばす。付けた件数を返す。"""
        tag = normalize_tag(tag)
        now = utcnow()
        with self.lock:
            self.reload_if_changed()
            events = [
                {"at": now, "host": self.host, "op": "add", "root": root, "path": p, "tag": tag, "source": source}
                for p in dict.fromkeys(paths)
                if tag not in self.items.get((root, p), {})
            ]
            self._append(events)
        return len(events)

    def remove(self, root: str, paths: Iterable[str], tag: str) -> int:
        """アイテムからタグを外す（直接付いたものだけ。継承したものは元のフォルダで外す）。"""
        tag = normalize_tag(tag)
        now = utcnow()
        with self.lock:
            self.reload_if_changed()
            events = [
                {"at": now, "host": self.host, "op": "remove", "root": root, "path": p, "tag": tag}
                for p in dict.fromkeys(paths)
                if tag in self.items.get((root, p), {})
            ]
            self._append(events)
        return len(events)

    def follow_moves(self, root: str, moves: Iterable[tuple[str, str]]) -> int:
        """移動・改名に追従する。タグの付いたアイテムを含む移動だけを記録する。"""
        now = utcnow()
        with self.lock:
            self.reload_if_changed()
            events = []
            for src, dst in moves:
                if any(k[0] == root and _under(k[1], src) for k in self.items):
                    events.append({"at": now, "host": self.host, "op": "move", "root": root, "from": src, "to": dst})
            self._append(events)
        return len(events)

    def rename_tag(self, old: str, new: str) -> int:
        """タグの名前を変える（全ルートのすべてのアイテム）。変わったアイテムの数を返す。"""
        old, new = normalize_tag(old), normalize_tag(new)
        if old == new:
            return 0
        with self.lock:
            self.reload_if_changed()
            n = sum(1 for tags in self.items.values() if old in tags)
            if n:
                self._append([{"at": utcnow(), "host": self.host, "op": "rename", "from": old, "to": new}])
        return n

    def delete_tag(self, tag: str) -> int:
        """タグを、付いているすべてのアイテムから外す。外したアイテムの数を返す。"""
        tag = normalize_tag(tag)
        with self.lock:
            self.reload_if_changed()
            n = sum(1 for tags in self.items.values() if tag in tags)
            if n:
                self._append([{"at": utcnow(), "host": self.host, "op": "delete", "tag": tag}])
        return n

    def tagged_paths(self, root: str) -> dict[str, set[str]]:
        """ルートの中で、タグごとに直接付いているアイテムの相対パス（検索用）。"""
        with self.lock:
            self.reload_if_changed()
            out: dict[str, set[str]] = {}
            for (r, path), tags in self.items.items():
                if r == root:
                    for tag in tags:
                        out.setdefault(tag, set()).add(path)
            return out

    # --- 問い合わせ ---

    def tags_of(self, root: str, path: str) -> dict[str, list[dict[str, str]]]:
        """アイテムのタグ。直接付いたもの（direct）と、祖先のフォルダから継承したもの（inherited）。"""
        with self.lock:
            self.reload_if_changed()
            direct = [
                {"tag": a.tag, "source": a.source}
                for a in sorted(self.items.get((root, path), {}).values(), key=lambda a: a.tag)
            ]
            inherited = []
            for anc in _ancestors(path):
                for a in sorted(self.items.get((root, anc), {}).values(), key=lambda a: a.tag):
                    inherited.append({"tag": a.tag, "from": anc})
            return {"direct": direct, "inherited": inherited}

    def all_tags(self) -> Counter[str]:
        """使われているタグと、直接付いている件数。"""
        with self.lock:
            self.reload_if_changed()
            return Counter(t for tags in self.items.values() for t in tags)

    def items_with(self, tag: str, root: str | None = None) -> list[tuple[str, str]]:
        """そのタグが直接付いたアイテム（ルート名, 相対パス）。フォルダなら配下も検索結果になる。"""
        tag = normalize_tag(tag)
        with self.lock:
            self.reload_if_changed()
            return sorted(k for k, tags in self.items.items() if tag in tags and (root is None or k[0] == root))

    def tagged_under(self, root: str, path: str) -> dict[str, list[str]]:
        """path の配下で、直接タグが付いているアイテム（相対パス → タグの一覧）。ファイルブラウザ用。"""
        with self.lock:
            self.reload_if_changed()
            return {
                p: sorted(tags)
                for (r, p), tags in self.items.items()
                if r == root and _under(p, path)
            }


def search(
    entries: Iterable[tuple[str, bool]],
    tagged: dict[str, set[str]],
    include: Sequence[str],
    mode: str = "and",
    exclude: Sequence[str] = (),
) -> list[str]:
    """タグでアイテムを絞り込む。フォルダに付いたタグは、配下のアイテムにも効く（継承）。

    Args:
        entries: 対象のアイテム（相対パス, フォルダか）。
        tagged: タグごとに直接付いているアイテムの相対パス（TagStore.tagged_paths）。
        include: 含むタグ。空なら、除外だけで絞る。
        mode: "and"（すべて含む）か "or"（どれかを含む）。
        exclude: 含まないタグ（どれか1つでも効いていれば除く）。

    Returns:
        当てはまるアイテムの相対パス（entries の順）。
    """
    inc = [tagged.get(normalize_tag(t), set()) for t in include]
    exc = [tagged.get(normalize_tag(t), set()) for t in exclude]

    def has(path: str, paths: set[str]) -> bool:
        if not paths:
            return False
        if path in paths or "" in paths:
            return True
        parts = path.split("/")
        return any("/".join(parts[:i]) in paths for i in range(1, len(parts)))

    out = []
    for path, _is_dir in entries:
        if inc:
            hits = (has(path, s) for s in inc)
            if not (all(hits) if mode == "and" else any(hits)):
                continue
        if any(has(path, s) for s in exc):
            continue
        out.append(path)
    return out
