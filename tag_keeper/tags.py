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

タグの辞書（版 0.12 から。F-MD-2・3）:

    {"op": "alias",    "tag": "会社:アティード", "alias": "会社:ATID"}       # 別名。付けるときと探すときに正式な名前へそろえる
    {"op": "unalias",  "alias": "会社:ATID"}
    {"op": "parent",   "tag": "種別:請求書", "parent": "種別:経理"}          # 親子。子が付けば親も成り立つ（探すときに広げる）
    {"op": "unparent", "tag": "種別:請求書", "parent": "種別:経理"}

型付きの属性（F-MD-4）。定義してから付ける。値はアイテムごと・名前ごとに1つ（付け直すと置き換わる）。フォルダの値は配下が継承する:

    {"op": "attr-def",   "name": "対応状態", "type": "choice", "choices": ["未対応", "対応中", "対応済み"]}
    {"op": "attr-undef", "name": "対応状態"}
    {"op": "attr-set",   "root": "onedrive", "path": "a/b.pdf", "name": "対応状態", "value": "対応中", "source": "manual"}
    {"op": "attr-unset", "root": "onedrive", "path": "a/b.pdf", "name": "対応状態"}

保存した検索（F-SE-5）:

    {"op": "search-save",   "name": "要対応", "query": "attr=要対応&kind=pdf"}
    {"op": "search-delete", "name": "要対応"}

アイテム間の関係（F-MD-7。種類は自由に足せる）と、内容ハッシュでの移動の結び付けを見送ったもの（F-ID-2）:

    {"op": "relate",      "root": "onedrive", "from": "a/v2.docx", "to": "a/v1.docx", "kind": "前の版"}
    {"op": "unrelate",    "root": "onedrive", "from": "a/v2.docx", "to": "a/v1.docx", "kind": "前の版"}
    {"op": "relink-skip", "root": "onedrive", "path": "new/x.pdf", "sha256": "..."}

ディレクトリの型とディレクトリカード（F-DR-1〜4）:

    {"op": "dir-type", "root": "onedrive", "path": "archive", "type": "archive"}       # type が "" なら設定を外す（推定に任せる）
    {"op": "dir-card", "root": "onedrive", "path": "経理/請求書", "description": "...", "naming": "YYYYMMDD_相手_件名"}  # 両方空なら外す

古い版は知らない種類の行を読み飛ばす（互換を保つため、既存の行の書式は変えない）。

パスは Unicode の NFC にそろえて扱う（F-ID-4。同じ名前でも NFC と NFD でバイト列が違うことがあるため）。
読み込むときも問い合わせのときもそろえるので、どちらの形で書かれた行でも同じアイテムを指す。
実際のファイルの操作には、ディスク上の元の名前を使う（ここはパスで管理情報を引くだけ）。
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
# 属性の型: choice（択一ラベル）・flag（フラグ）・date（日付）・number（数・金額）・text（文字。取引先など）
ATTR_TYPES = ("choice", "flag", "date", "number", "text")
# 文字の属性の値の長さの上限
MAX_TEXT_VALUE = 200
# 別名・親子をたどる深さの上限（記録の誤りで循環しても止まるように）
MAX_DEPTH = 32


class TagError(ValueError):
    """タグ・属性の名前や値が不正なときの例外。"""


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


def _nk(path: str) -> str:
    """パスを管理情報の引き当てに使う形（Unicode の NFC）にそろえる（F-ID-4）。"""
    return unicodedata.normalize("NFC", path)


# 関係の種類の既定の候補（画面の補完に出す。自由に足せる）
RELATION_KINDS = ("前の版", "派生元", "添付先", "関連")


def _ancestors(path: str) -> list[str]:
    """祖先のフォルダ（ルート "" から近い順ではなく、ルートから順に）。自身は含まない。"""
    if path == "":
        return []
    parts = path.split("/")
    return [""] + ["/".join(parts[:i]) for i in range(1, len(parts))]


def _under(path: str, base: str) -> bool:
    """path が base 自身か、その配下か。"""
    return base == "" or path == base or path.startswith(base + "/")


def order_moves(moves: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """同時に起きた移動を、1件ずつ順に当てはめても同時に当てはめたのと同じ結果になる順に並べる。

    - 配下の移動を先に: 「a → b」と「a/x.pdf → b/y.pdf」を同時に見つけたとき、a を先に動かすと
      a/x.pdf のタグが実在しない b/x.pdf へ行ってしまう
    - 入れ替えでは、移動元を先に空ける: 「a → b」と「c → a」では、c を先に動かすと c のタグが a と混ざって b へ行く
    - 循環（a → b と b → a）だけは順番では解けないので、深い順に並べて警告する（1回の走査でまず起きない）
    """
    items = list(dict.fromkeys(moves))
    n = len(items)
    if n <= 1:
        return items

    def strictly_under(path: str, base: str) -> bool:
        return path != base and _under(path, base)

    # i → j: i を j より先に当てはめる
    after: list[set[int]] = [set() for _ in range(n)]
    indeg = [0] * n
    for i, (si, _di) in enumerate(items):
        for j, (sj, dj) in enumerate(items):
            if i == j:
                continue
            # 配下の移動（si が sj の配下）を先に / j の移動先が i の移動元にかかるなら、i を先に動かして空ける
            if (strictly_under(si, sj) or _under(dj, si)) and j not in after[i]:
                after[i].add(j)
                indeg[j] += 1
    ready = sorted((k for k in range(n) if indeg[k] == 0), key=lambda k: (-items[k][0].count("/"), k))
    out: list[int] = []
    while ready:
        k = ready.pop(0)
        out.append(k)
        for j in sorted(after[k]):
            indeg[j] -= 1
            if indeg[j] == 0:
                ready.append(j)
        ready.sort(key=lambda m: (-items[m][0].count("/"), m))
    if len(out) < n:
        rest = sorted((k for k in range(n) if k not in out), key=lambda k: (-items[k][0].count("/"), k))
        log.warning("移動が循環しているため、タグの追従が正しくない可能性があります: %s", [items[k] for k in rest])
        out.extend(rest)
    return [items[k] for k in out]


@dataclass(frozen=True)
class Assignment:
    """アイテムに直接付いたタグ1件。"""

    tag: str
    source: str
    at: str
    host: str


@dataclass(frozen=True)
class AttrDef:
    """属性の定義（名前・型・択一の選択肢）。"""

    name: str
    type: str
    choices: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "choices": list(self.choices)}


@dataclass(frozen=True)
class AttrValue:
    """アイテムに直接付いた属性の値1件。"""

    name: str
    value: Any
    source: str
    at: str


def parse_attr_value(d: AttrDef, value: Any) -> Any:
    """属性の値を型に合わせて確かめ、保存する形にする。不正なら TagError。

    choice は選択肢のどれか、flag は true、date は YYYY-MM-DD、number は数（整数なら整数）、text は 200 文字まで。
    """
    import datetime as dt

    if d.type == "flag":
        if value in (True, "true", "1", 1, "on"):
            return True
        raise TagError(f"フラグの値は true だけです: {d.name}")
    if value is None or (isinstance(value, str) and not value.strip()):
        raise TagError(f"値が空です: {d.name}")
    if d.type == "choice":
        v = unicodedata.normalize("NFC", str(value)).strip()
        if v not in d.choices:
            raise TagError(f"{d.name} の値は {' / '.join(d.choices)} のどれかです: {v}")
        return v
    if d.type == "date":
        try:
            return dt.date.fromisoformat(str(value).strip()).isoformat()
        except ValueError as e:
            raise TagError(f"{d.name} の値は YYYY-MM-DD の日付です: {value}") from e
    if d.type == "number":
        try:
            n = float(str(value).replace(",", "").strip())
        except ValueError as e:
            raise TagError(f"{d.name} の値は数です: {value}") from e
        return int(n) if n.is_integer() else n
    v = " ".join(unicodedata.normalize("NFC", str(value)).split())
    if len(v) > MAX_TEXT_VALUE:
        raise TagError(f"{d.name} の値が長すぎます（{MAX_TEXT_VALUE} 文字まで）")
    return v


def match_attr(d: AttrDef | None, value: Any, op: str, want: str) -> bool:
    """属性の値が条件に合うか。op は exists・=・>=・<=・>・<（日付と数は大小、ほかは = だけ。文字は部分一致）。"""
    if op == "exists":
        return value is not None
    if value is None or d is None:
        return False
    if d.type == "number":
        try:
            w: Any = float(want.replace(",", ""))
        except ValueError:
            return False
        v: Any = float(value)
    elif d.type == "date":
        v, w = str(value), want.strip()
    elif d.type == "text":
        return op == "=" and want.strip().casefold() in str(value).casefold()
    else:
        return op == "=" and str(value) == unicodedata.normalize("NFC", want).strip()
    return {"=": v == w, ">=": v >= w, "<=": v <= w, ">": v > w, "<": v < w}.get(op, False)


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
        # 別名 → 正式な名前、タグ → 親のタグ
        self.aliases: dict[str, str] = {}
        self.parents: dict[str, set[str]] = {}
        # 属性の定義と、(ルート名, 相対パス) → {属性名 → AttrValue}
        self.attr_defs: dict[str, AttrDef] = {}
        self.attrs: dict[tuple[str, str], dict[str, AttrValue]] = {}
        # 保存した検索（名前 → 問い合わせの文字列）
        self.searches: dict[str, str] = {}
        # アイテム間の関係（ルート名 → {(元, 先, 種類)}）と、結び付けを見送った移動（ルート名, 新しいパス, 内容ハッシュ）
        self.relations: dict[str, set[tuple[str, str, str]]] = {}
        self.relink_skips: set[tuple[str, str, str]] = set()
        # ディレクトリの型（(ルート名, 相対パス) → 型）と、ディレクトリカード（(ルート名, 相対パス) → {description, naming}）
        self.dir_types: dict[tuple[str, str], str] = {}
        self.dir_cards: dict[tuple[str, str], dict[str, str]] = {}
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
            self.aliases, self.parents = {}, {}
            self.attr_defs, self.attrs, self.searches = {}, {}, {}
            self.relations, self.relink_skips = {}, set()
            self.dir_types, self.dir_cards = {}, {}
            for ev in events:
                self._apply(ev)
            self._stamp = stamp

    def _apply(self, ev: dict[str, Any]) -> None:
        """イベント1件を、保持している状態に反映する。"""
        op = ev.get("op")
        root = str(ev.get("root", ""))
        if op == "add":
            key = (root, _nk(str(ev["path"])))
            self.items.setdefault(key, {})[ev["tag"]] = Assignment(
                ev["tag"], str(ev.get("source", "manual")), str(ev.get("at", "")), str(ev.get("host", ""))
            )
        elif op == "remove":
            key = (root, _nk(str(ev["path"])))
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
            # 辞書（別名・親子）の中の名前も付け替える
            for alias, canon in list(self.aliases.items()):
                if canon == src:
                    self.aliases[alias] = dst
            if src in self.parents:
                self.parents.setdefault(dst, set()).update(self.parents.pop(src))
            for ps in self.parents.values():
                if src in ps:
                    ps.discard(src)
                    ps.add(dst)
            self.parents.get(dst, set()).discard(dst)
        elif op == "delete":
            tag = str(ev["tag"])
            for key in [k for k, tags in self.items.items() if tag in tags]:
                del self.items[key][tag]
                if not self.items[key]:
                    del self.items[key]
            # 消したタグを指す別名と親子も消す
            for alias in [a for a, c in self.aliases.items() if c == tag]:
                del self.aliases[alias]
            self.parents.pop(tag, None)
            for ps in self.parents.values():
                ps.discard(tag)
        elif op == "move":
            src, dst = _nk(str(ev["from"])), _nk(str(ev["to"]))
            moved = [k for k in self.items if k[0] == root and _under(k[1], src)]
            entries = {k: self.items.pop(k) for k in moved}
            for (r, p), tags in entries.items():
                new = dst + p[len(src):]
                self.items.setdefault((r, new), {}).update(tags)
            # 属性も同じように追従させる
            moved_attrs = {k: self.attrs.pop(k) for k in [k for k in self.attrs if k[0] == root and _under(k[1], src)]}
            for (r, p), values in moved_attrs.items():
                self.attrs.setdefault((r, dst + p[len(src):]), {}).update(values)
            # 関係の両端も追従させる
            rels = self.relations.get(root)
            if rels:
                def moved_path(path: str) -> str:
                    return dst + path[len(src):] if _under(path, src) else path

                self.relations[root] = {(moved_path(a), moved_path(b), k) for a, b, k in rels}
            # ディレクトリの型とカードも追従させる
            for table in (self.dir_types, self.dir_cards):
                for k in [k for k in table if k[0] == root and _under(k[1], src)]:
                    table[(root, dst + k[1][len(src):])] = table.pop(k)
        elif op == "alias":
            alias, tag = str(ev["alias"]), str(ev["tag"])
            if alias != tag:
                self.aliases[alias] = tag
        elif op == "unalias":
            self.aliases.pop(str(ev["alias"]), None)
        elif op == "parent":
            tag, parent = str(ev["tag"]), str(ev["parent"])
            if tag != parent:
                self.parents.setdefault(tag, set()).add(parent)
        elif op == "unparent":
            ps = self.parents.get(str(ev["tag"]))
            if ps is not None:
                ps.discard(str(ev["parent"]))
                if not ps:
                    del self.parents[str(ev["tag"])]
        elif op == "attr-def":
            name = str(ev["name"])
            self.attr_defs[name] = AttrDef(name, str(ev.get("type", "text")), tuple(str(c) for c in ev.get("choices") or ()))
        elif op == "attr-undef":
            name = str(ev["name"])
            self.attr_defs.pop(name, None)
            for key in [k for k, vals in self.attrs.items() if name in vals]:
                del self.attrs[key][name]
                if not self.attrs[key]:
                    del self.attrs[key]
        elif op == "attr-set":
            key = (root, _nk(str(ev["path"])))
            name = str(ev["name"])
            self.attrs.setdefault(key, {})[name] = AttrValue(name, ev.get("value"), str(ev.get("source", "manual")), str(ev.get("at", "")))
        elif op == "attr-unset":
            key = (root, _nk(str(ev["path"])))
            vals = self.attrs.get(key)
            if vals is not None:
                vals.pop(str(ev["name"]), None)
                if not vals:
                    del self.attrs[key]
        elif op == "search-save":
            self.searches[str(ev["name"])] = str(ev.get("query", ""))
        elif op == "search-delete":
            self.searches.pop(str(ev["name"]), None)
        elif op == "relate":
            a, b, kind = _nk(str(ev["from"])), _nk(str(ev["to"])), str(ev.get("kind") or "関連")
            if a != b:
                self.relations.setdefault(root, set()).add((a, b, kind))
        elif op == "unrelate":
            self.relations.get(root, set()).discard((_nk(str(ev["from"])), _nk(str(ev["to"])), str(ev.get("kind") or "関連")))
        elif op == "relink-skip":
            self.relink_skips.add((root, _nk(str(ev["path"])), str(ev.get("sha256", ""))))
        elif op == "dir-type":
            key = (root, _nk(str(ev["path"])))
            if ev.get("type"):
                self.dir_types[key] = str(ev["type"])
            else:
                self.dir_types.pop(key, None)
        elif op == "dir-card":
            key = (root, _nk(str(ev["path"])))
            card = {"description": str(ev.get("description") or ""), "naming": str(ev.get("naming") or "")}
            if card["description"] or card["naming"]:
                self.dir_cards[key] = card
            else:
                self.dir_cards.pop(key, None)

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
        """アイテムにタグを付ける（別名なら正式な名前で付ける）。既に付いているものは飛ばす。付けた件数を返す。"""
        now = utcnow()
        with self.lock:
            self.reload_if_changed()
            tag = self._canonical(normalize_tag(tag))
            events = [
                {"at": now, "host": self.host, "op": "add", "root": root, "path": p, "tag": tag, "source": source}
                for p in dict.fromkeys(_nk(x) for x in paths)
                if tag not in self.items.get((root, p), {})
            ]
            self._append(events)
        return len(events)

    def remove(self, root: str, paths: Iterable[str], tag: str) -> int:
        """アイテムからタグを外す（直接付いたものだけ。継承したものは元のフォルダで外す）。"""
        now = utcnow()
        with self.lock:
            self.reload_if_changed()
            tag = self._canonical(normalize_tag(tag))
            events = [
                {"at": now, "host": self.host, "op": "remove", "root": root, "path": p, "tag": tag}
                for p in dict.fromkeys(_nk(x) for x in paths)
                if tag in self.items.get((root, p), {})
            ]
            self._append(events)
        return len(events)

    def follow_moves(self, root: str, moves: Iterable[tuple[str, str]]) -> int:
        """移動・改名に追従する。タグか属性の付いたアイテムを含む移動だけを記録する。

        1回の走査で見つけた移動は同時に起きたものなので、順に当てはめても同じ結果になる順に並べ直してから記録する
        （order_moves を参照）。
        """
        now = utcnow()
        with self.lock:
            self.reload_if_changed()
            events = []
            rel_paths = {p for a, b, _k in self.relations.get(root, ()) for p in (a, b)}
            rel_paths |= {k[1] for k in [*self.dir_types, *self.dir_cards] if k[0] == root}
            for src, dst in order_moves([(_nk(a), _nk(b)) for a, b in moves]):
                if any(k[0] == root and _under(k[1], src) for k in [*self.items, *self.attrs]) or any(
                    _under(p, src) for p in rel_paths
                ):
                    events.append({"at": now, "host": self.host, "op": "move", "root": root, "from": src, "to": dst})
            self._append(events)
        return len(events)

    def detach_under(self, root: str, base: str) -> dict[str, list[list[str]]]:
        """base とその配下に直接付いたタグを外し、外したもの（相対パス → [タグ, 出所] の一覧）を返す。

        隔離したアイテムのタグを、そのパスに残さないために使う（残すと、後で同じパスに来た別のファイルに付いてしまう）。
        返したものを実行記録に残しておき、取り消しのときに attach で戻す。属性は detach_attrs_under で同じように扱う。
        """
        now = utcnow()
        base = _nk(base)
        with self.lock:
            self.reload_if_changed()
            detached = {
                p: [[a.tag, a.source] for a in sorted(tags.values(), key=lambda a: a.tag)]
                for (r, p), tags in self.items.items()
                if r == root and _under(p, base) and tags
            }
            events = [
                {"at": now, "host": self.host, "op": "remove", "root": root, "path": p, "tag": tag}
                for p, pairs in sorted(detached.items())
                for tag, _source in pairs
            ]
            self._append(events)
        return detached

    def attach(self, root: str, detached: dict[str, list[list[str]]]) -> int:
        """detach_under で外したタグを付け直す（出所も元のまま）。付けた件数を返す。"""
        now = utcnow()
        with self.lock:
            self.reload_if_changed()
            events = [
                {"at": now, "host": self.host, "op": "add", "root": root, "path": p, "tag": tag, "source": source}
                for p, pairs in sorted(detached.items())
                for tag, source in pairs
                if tag not in self.items.get((root, p), {})
            ]
            self._append(events)
        return len(events)

    def detach_attrs_under(self, root: str, base: str) -> dict[str, list[list[Any]]]:
        """base とその配下の属性を外し、外したもの（相対パス → [名前, 値, 出所] の一覧）を返す（隔離のとき）。"""
        now = utcnow()
        base = _nk(base)
        with self.lock:
            self.reload_if_changed()
            detached = {
                p: [[v.name, v.value, v.source] for v in sorted(vals.values(), key=lambda v: v.name)]
                for (r, p), vals in self.attrs.items()
                if r == root and _under(p, base) and vals
            }
            events = [
                {"at": now, "host": self.host, "op": "attr-unset", "root": root, "path": p, "name": name}
                for p, triples in sorted(detached.items())
                for name, _value, _source in triples
            ]
            self._append(events)
        return detached

    def attach_attrs(self, root: str, detached: dict[str, list[list[Any]]]) -> int:
        """detach_attrs_under で外した属性を付け直す（取り消しのとき）。定義が消えたものは飛ばす。"""
        now = utcnow()
        with self.lock:
            self.reload_if_changed()
            events = [
                {"at": now, "host": self.host, "op": "attr-set", "root": root, "path": p, "name": name, "value": value, "source": source}
                for p, triples in sorted(detached.items())
                for name, value, source in triples
                if name in self.attr_defs and name not in self.attrs.get((root, p), {})
            ]
            self._append(events)
        return len(events)

    def rename_tag(self, old: str, new: str) -> int:
        """タグの名前を変える（全ルートのすべてのアイテム）。変わったアイテムの数を返す。

        別名と親子の中の名前も付け替える。変更先が別名なら、その正式な名前にする。
        """
        old, new = normalize_tag(old), normalize_tag(new)
        with self.lock:
            self.reload_if_changed()
            old, new = self._canonical(old), self._canonical(new)
            if old == new:
                return 0
            n = sum(1 for tags in self.items.values() if old in tags)
            if n or self._in_dictionary(old):
                self._append([{"at": utcnow(), "host": self.host, "op": "rename", "from": old, "to": new}])
        return n

    def delete_tag(self, tag: str) -> int:
        """タグを、付いているすべてのアイテムから外す（別名と親子も消す）。外したアイテムの数を返す。"""
        tag = normalize_tag(tag)
        with self.lock:
            self.reload_if_changed()
            tag = self._canonical(tag)
            n = sum(1 for tags in self.items.values() if tag in tags)
            if n or self._in_dictionary(tag):
                self._append([{"at": utcnow(), "host": self.host, "op": "delete", "tag": tag}])
        return n

    # --- タグの辞書（別名・親子。F-MD-2・3） ---

    def _canonical(self, tag: str) -> str:
        """別名をたどって正式な名前にする（ロックを取った状態で呼ぶ）。"""
        seen = set()
        while tag in self.aliases and tag not in seen and len(seen) < MAX_DEPTH:
            seen.add(tag)
            tag = self.aliases[tag]
        return tag

    def _in_dictionary(self, tag: str) -> bool:
        """別名か親子に名前が出てくるか（付いているアイテムが無くても、名前の変更・削除の対象にする）。"""
        return tag in self.aliases.values() or tag in self.parents or any(tag in ps for ps in self.parents.values())

    def canonical(self, tag: str) -> str:
        """タグの表記をそろえ、別名なら正式な名前にする（探すときに使う）。"""
        tag = normalize_tag(tag)
        with self.lock:
            self.reload_if_changed()
            return self._canonical(tag)

    def _ancestors_of(self, tag: str) -> set[str]:
        """親・親の親…（自身は含まない。ロックを取った状態で呼ぶ）。"""
        out: set[str] = set()
        stack = list(self.parents.get(tag, ()))
        while stack and len(out) < 10_000:
            t = stack.pop()
            if t in out:
                continue
            out.add(t)
            stack.extend(self.parents.get(t, ()))
        out.discard(tag)
        return out

    def add_alias(self, tag: str, alias: str) -> None:
        """別名を登録する。別名として既に使われているタグは、正式な名前に統合する（付いたアイテムを移す）。

        Raises:
            TagError: 別名が正式な名前と同じ。
        """
        tag, alias = normalize_tag(tag), normalize_tag(alias)
        now = utcnow()
        with self.lock:
            self.reload_if_changed()
            tag = self._canonical(tag)
            if self._canonical(alias) == tag or alias == tag:
                raise TagError(f"別名が正式な名前と同じです: {alias}")
            events: list[dict[str, Any]] = []
            if any(alias in tags for tags in self.items.values()) or self._in_dictionary(alias):
                # 別名にする名前が既にタグとして使われていれば、付いたアイテム・別名・親子ごと正式な名前へまとめる
                events.append({"at": now, "host": self.host, "op": "rename", "from": alias, "to": tag})
            events.append({"at": now, "host": self.host, "op": "alias", "tag": tag, "alias": alias})
            self._append(events)

    def remove_alias(self, alias: str) -> bool:
        """別名の登録を外す。外したら True。"""
        alias = normalize_tag(alias)
        with self.lock:
            self.reload_if_changed()
            if alias not in self.aliases:
                return False
            self._append([{"at": utcnow(), "host": self.host, "op": "unalias", "alias": alias}])
        return True

    def add_parent(self, tag: str, parent: str) -> None:
        """親子を登録する（tag が付けば parent も成り立つ）。循環になるものは断る（TagError）。"""
        tag, parent = normalize_tag(tag), normalize_tag(parent)
        with self.lock:
            self.reload_if_changed()
            tag, parent = self._canonical(tag), self._canonical(parent)
            if tag == parent or tag in self._ancestors_of(parent):
                raise TagError(f"親子が循環します: {tag} → {parent}")
            if parent in self.parents.get(tag, ()):
                return
            self._append([{"at": utcnow(), "host": self.host, "op": "parent", "tag": tag, "parent": parent}])

    def remove_parent(self, tag: str, parent: str) -> bool:
        """親子の登録を外す。外したら True。"""
        tag, parent = normalize_tag(tag), normalize_tag(parent)
        with self.lock:
            self.reload_if_changed()
            tag, parent = self._canonical(tag), self._canonical(parent)
            if parent not in self.parents.get(tag, ()):
                return False
            self._append([{"at": utcnow(), "host": self.host, "op": "unparent", "tag": tag, "parent": parent}])
        return True

    def dictionary(self) -> dict[str, dict[str, list[str]]]:
        """使われているか辞書に出てくるタグごとの、別名・親・子（画面の一覧用）。"""
        with self.lock:
            self.reload_if_changed()
            names = {t for tags in self.items.values() for t in tags} | set(self.aliases.values()) | set(self.parents)
            names |= {p for ps in self.parents.values() for p in ps}
            out: dict[str, dict[str, list[str]]] = {n: {"aliases": [], "parents": [], "children": []} for n in names}
            for alias, canon in self.aliases.items():
                out.setdefault(canon, {"aliases": [], "parents": [], "children": []})["aliases"].append(alias)
            for child, ps in self.parents.items():
                for parent in ps:
                    out[child]["parents"].append(parent)
                    out[parent]["children"].append(child)
            for d in out.values():
                for k in d:
                    d[k].sort()
            return out

    # --- 型付きの属性（F-MD-4） ---

    def define_attr(self, name: str, type_: str, choices: Sequence[str] = ()) -> AttrDef:
        """属性を定義する（同じ名前なら定義し直す）。択一は選択肢が2つ以上要る。"""
        name = normalize_tag(name)
        if type_ not in ATTR_TYPES:
            raise TagError(f"属性の型は {', '.join(ATTR_TYPES)} のどれかです: {type_}")
        opts = tuple(dict.fromkeys(" ".join(unicodedata.normalize("NFC", c).split()) for c in choices if str(c).strip()))
        if type_ == "choice" and len(opts) < 2:
            raise TagError(f"択一の属性には、選択肢が2つ以上要ります: {name}")
        d = AttrDef(name, type_, opts if type_ == "choice" else ())
        with self.lock:
            self.reload_if_changed()
            if self.attr_defs.get(name) == d:
                return d
            self._append([{"at": utcnow(), "host": self.host, "op": "attr-def", **d.to_dict()}])
        return d

    def undefine_attr(self, name: str) -> int:
        """属性の定義を消す（付いた値もすべて外れる）。値が付いていたアイテムの数を返す。"""
        name = normalize_tag(name)
        with self.lock:
            self.reload_if_changed()
            if name not in self.attr_defs:
                return 0
            n = sum(1 for vals in self.attrs.values() if name in vals)
            self._append([{"at": utcnow(), "host": self.host, "op": "attr-undef", "name": name}])
        return n

    def attr_defs_list(self) -> list[dict[str, Any]]:
        """属性の定義の一覧（名前順）と、値が直接付いているアイテムの数。"""
        with self.lock:
            self.reload_if_changed()
            counts = Counter(n for vals in self.attrs.values() for n in vals)
            return [{**d.to_dict(), "count": counts.get(d.name, 0)} for d in sorted(self.attr_defs.values(), key=lambda d: d.name)]

    def set_attr(self, root: str, paths: Iterable[str], name: str, value: Any, source: str = "manual") -> int:
        """属性の値を付ける（同じ名前の値は置き換わる。択一に2つの値が付くことは無い）。変えた件数を返す。"""
        name = normalize_tag(name)
        now = utcnow()
        with self.lock:
            self.reload_if_changed()
            d = self.attr_defs.get(name)
            if d is None:
                raise TagError(f"定義されていない属性です: {name}")
            v = parse_attr_value(d, value)
            events = [
                {"at": now, "host": self.host, "op": "attr-set", "root": root, "path": p, "name": name, "value": v, "source": source}
                for p in dict.fromkeys(_nk(x) for x in paths)
                if (cur := self.attrs.get((root, p), {}).get(name)) is None or cur.value != v
            ]
            self._append(events)
        return len(events)

    def unset_attr(self, root: str, paths: Iterable[str], name: str) -> int:
        """属性の値を外す（直接付いたものだけ）。外した件数を返す。"""
        name = normalize_tag(name)
        now = utcnow()
        with self.lock:
            self.reload_if_changed()
            events = [
                {"at": now, "host": self.host, "op": "attr-unset", "root": root, "path": p, "name": name}
                for p in dict.fromkeys(_nk(x) for x in paths)
                if name in self.attrs.get((root, p), {})
            ]
            self._append(events)
        return len(events)

    def attrs_of(self, root: str, path: str) -> dict[str, list[dict[str, Any]]]:
        """アイテムの属性。直接付いたもの（direct）と、祖先のフォルダから継承したもの（inherited。近い方が優先）。"""
        path = _nk(path)
        with self.lock:
            self.reload_if_changed()
            direct = {v.name: v for v in self.attrs.get((root, path), {}).values()}
            inherited: dict[str, dict[str, Any]] = {}
            for anc in _ancestors(path):  # ルートから近い順に上書きしていくので、最後は最も近い祖先の値
                for v in self.attrs.get((root, anc), {}).values():
                    if v.name not in direct:
                        inherited[v.name] = {"name": v.name, "value": v.value, "from": anc}
            return {
                "direct": [{"name": v.name, "value": v.value, "source": v.source} for v in sorted(direct.values(), key=lambda v: v.name)],
                "inherited": [inherited[k] for k in sorted(inherited)],
            }

    def attr_paths(self, root: str) -> dict[str, dict[str, Any]]:
        """ルートの中で、属性が直接付いたアイテム（相対パス → {名前 → 値}）。検索で継承を計算するのに使う。"""
        with self.lock:
            self.reload_if_changed()
            return {p: {v.name: v.value for v in vals.values()} for (r, p), vals in self.attrs.items() if r == root}

    # --- 保存した検索（F-SE-5） ---

    def save_search(self, name: str, query: str) -> None:
        """検索の条件を名前を付けて保存する（同じ名前なら置き換える）。"""
        name = normalize_tag(name)
        query = query.strip().lstrip("?")
        if not query:
            raise TagError("保存する条件が空です")
        with self.lock:
            self.reload_if_changed()
            if self.searches.get(name) == query:
                return
            self._append([{"at": utcnow(), "host": self.host, "op": "search-save", "name": name, "query": query}])

    def delete_search(self, name: str) -> bool:
        """保存した検索を消す。消したら True。"""
        name = normalize_tag(name)
        with self.lock:
            self.reload_if_changed()
            if name not in self.searches:
                return False
            self._append([{"at": utcnow(), "host": self.host, "op": "search-delete", "name": name}])
        return True

    def saved_searches(self) -> list[dict[str, str]]:
        """保存した検索の一覧（名前順）。"""
        with self.lock:
            self.reload_if_changed()
            return [{"name": n, "query": q} for n, q in sorted(self.searches.items())]

    # --- アイテム間の関係（F-MD-7） ---

    def relate(self, root: str, src: str, dst: str, kind: str = "関連") -> bool:
        """2つのアイテムの間に関係を張る（src から dst へ。例: 新しい版 → 前の版）。張ったら True。"""
        src, dst = _nk(src), _nk(dst)
        kind = normalize_tag(kind or "関連")
        if src == dst:
            raise TagError("同じアイテムどうしには関係を張れません")
        with self.lock:
            self.reload_if_changed()
            if (src, dst, kind) in self.relations.get(root, ()):
                return False
            self._append([{"at": utcnow(), "host": self.host, "op": "relate", "root": root, "from": src, "to": dst, "kind": kind}])
        return True

    def unrelate(self, root: str, src: str, dst: str, kind: str = "関連") -> bool:
        """関係を外す。外したら True。"""
        src, dst = _nk(src), _nk(dst)
        kind = normalize_tag(kind or "関連")
        with self.lock:
            self.reload_if_changed()
            if (src, dst, kind) not in self.relations.get(root, ()):
                return False
            self._append([{"at": utcnow(), "host": self.host, "op": "unrelate", "root": root, "from": src, "to": dst, "kind": kind}])
        return True

    def relations_of(self, root: str, path: str) -> dict[str, list[dict[str, str]]]:
        """アイテムの関係。自身から張ったもの（outgoing）と、ほかから張られたもの（incoming）。"""
        path = _nk(path)
        with self.lock:
            self.reload_if_changed()
            rels = sorted(self.relations.get(root, ()))
            return {
                "outgoing": [{"kind": k, "path": b} for a, b, k in rels if a == path],
                "incoming": [{"kind": k, "path": a} for a, b, k in rels if b == path],
            }

    def relation_kinds(self) -> list[str]:
        """関係の種類の候補（既定の候補と、使われているもの）。"""
        with self.lock:
            self.reload_if_changed()
            used = {k for rels in self.relations.values() for _a, _b, k in rels}
            return list(dict.fromkeys([*RELATION_KINDS, *sorted(used)]))

    def detach_relations_under(self, root: str, base: str) -> list[list[str]]:
        """片方の端が base とその配下にある関係を外し、外したもの（[元, 先, 種類] の一覧）を返す（隔離のとき）。"""
        base = _nk(base)
        now = utcnow()
        with self.lock:
            self.reload_if_changed()
            hit = sorted((a, b, k) for a, b, k in self.relations.get(root, ()) if _under(a, base) or _under(b, base))
            self._append([{"at": now, "host": self.host, "op": "unrelate", "root": root, "from": a, "to": b, "kind": k} for a, b, k in hit])
        return [[a, b, k] for a, b, k in hit]

    def attach_relations(self, root: str, detached: Iterable[Sequence[str]]) -> int:
        """detach_relations_under で外した関係を張り直す（取り消しのとき）。張った件数を返す。"""
        now = utcnow()
        with self.lock:
            self.reload_if_changed()
            have = self.relations.get(root, set())
            events = [
                {"at": now, "host": self.host, "op": "relate", "root": root, "from": a, "to": b, "kind": k}
                for a, b, k in (tuple(x) for x in detached)
                if (a, b, k) not in have
            ]
            self._append(events)
        return len(events)

    # --- 内容ハッシュでの移動の結び付けを見送ったもの（F-ID-2） ---

    def skip_relink(self, root: str, path: str, sha256: str) -> None:
        """「どれとも結び付けない」と決めた新しいファイルを記録する（確認の一覧に二度と出さない）。"""
        with self.lock:
            self.reload_if_changed()
            if (root, _nk(path), sha256) in self.relink_skips:
                return
            self._append([{"at": utcnow(), "host": self.host, "op": "relink-skip", "root": root, "path": _nk(path), "sha256": sha256}])

    def relink_skipped(self, root: str) -> set[tuple[str, str]]:
        """結び付けを見送った（新しいパス, 内容ハッシュ）の集合。"""
        with self.lock:
            self.reload_if_changed()
            return {(p, sha) for r, p, sha in self.relink_skips if r == root}

    # --- ディレクトリの型とディレクトリカード（F-DR-1〜4） ---

    def set_dir_type(self, root: str, path: str, type_: str) -> bool:
        """フォルダの型を設定する（"" で外して推定に任せる）。変えたら True。型の名前の検査は呼び出し側（dirtypes）で行う。"""
        path = _nk(path)
        with self.lock:
            self.reload_if_changed()
            if self.dir_types.get((root, path), "") == type_:
                return False
            self._append([{"at": utcnow(), "host": self.host, "op": "dir-type", "root": root, "path": path, "type": type_}])
        return True

    def dir_types_of(self, root: str) -> dict[str, str]:
        """ルートの中で、型を設定したフォルダ（相対パス → 型）。"""
        with self.lock:
            self.reload_if_changed()
            return {p: t for (r, p), t in self.dir_types.items() if r == root}

    def set_dir_card(self, root: str, path: str, description: str, naming: str) -> bool:
        """ディレクトリカード（何を置く場所か・命名規則）を書く（両方空なら外す）。変えたら True。"""
        path = _nk(path)
        card = {"description": " ".join(description.split())[:1000], "naming": " ".join(naming.split())[:200]}
        with self.lock:
            self.reload_if_changed()
            cur = self.dir_cards.get((root, path), {"description": "", "naming": ""})
            if cur == card:
                return False
            self._append([{"at": utcnow(), "host": self.host, "op": "dir-card", "root": root, "path": path, **card}])
        return True

    def dir_cards_of(self, root: str) -> dict[str, dict[str, str]]:
        """ルートの中のディレクトリカード（相対パス → {description, naming}）。"""
        with self.lock:
            self.reload_if_changed()
            return {p: dict(c) for (r, p), c in self.dir_cards.items() if r == root}

    def detach_dirmeta_under(self, root: str, base: str) -> dict[str, dict[str, Any]]:
        """base とその配下の型とカードを外し、外したもの（相対パス → {type, card}）を返す（隔離のとき）。"""
        base = _nk(base)
        now = utcnow()
        with self.lock:
            self.reload_if_changed()
            out: dict[str, dict[str, Any]] = {}
            events: list[dict[str, Any]] = []
            for (r, p), t in sorted(self.dir_types.items()):
                if r == root and _under(p, base):
                    out.setdefault(p, {})["type"] = t
                    events.append({"at": now, "host": self.host, "op": "dir-type", "root": root, "path": p, "type": ""})
            for (r, p), c in sorted(self.dir_cards.items()):
                if r == root and _under(p, base):
                    out.setdefault(p, {})["card"] = dict(c)
                    events.append({"at": now, "host": self.host, "op": "dir-card", "root": root, "path": p, "description": "", "naming": ""})
            self._append(events)
        return out

    def attach_dirmeta(self, root: str, detached: dict[str, dict[str, Any]]) -> int:
        """detach_dirmeta_under で外した型とカードを戻す（取り消しのとき）。"""
        n = 0
        for p, meta in detached.items():
            if meta.get("type"):
                n += self.set_dir_type(root, p, str(meta["type"]))
            card = meta.get("card")
            if card:
                n += self.set_dir_card(root, p, str(card.get("description", "")), str(card.get("naming", "")))
        return n

    # --- 書き出しと読み込み（F-MD-9） ---

    def export_events(self) -> list[dict[str, Any]]:
        """今の管理情報の全量を、読み込み直せるイベントの並びにする（属性の定義・別名・親子・タグ・属性・関係・保存した検索）。

        履歴（いつ付けて、いつ外したか）は含めず、今の状態だけを書く。
        """
        with self.lock:
            self.reload_if_changed()
            out: list[dict[str, Any]] = []
            out += [{"op": "attr-def", **d.to_dict()} for d in sorted(self.attr_defs.values(), key=lambda d: d.name)]
            out += [{"op": "alias", "tag": c, "alias": a} for a, c in sorted(self.aliases.items())]
            out += [{"op": "parent", "tag": c, "parent": p} for c, ps in sorted(self.parents.items()) for p in sorted(ps)]
            for (r, path), tags in sorted(self.items.items()):
                out += [{"op": "add", "root": r, "path": path, "tag": a.tag, "source": a.source} for a in sorted(tags.values(), key=lambda a: a.tag)]
            for (r, path), vals in sorted(self.attrs.items()):
                out += [{"op": "attr-set", "root": r, "path": path, "name": v.name, "value": v.value, "source": v.source} for v in sorted(vals.values(), key=lambda v: v.name)]
            for r, rels in sorted(self.relations.items()):
                out += [{"op": "relate", "root": r, "from": a, "to": b, "kind": k} for a, b, k in sorted(rels)]
            out += [{"op": "search-save", "name": n, "query": q} for n, q in sorted(self.searches.items())]
            out += [{"op": "dir-type", "root": r, "path": p, "type": t} for (r, p), t in sorted(self.dir_types.items())]
            out += [{"op": "dir-card", "root": r, "path": p, **c} for (r, p), c in sorted(self.dir_cards.items())]
            return out

    def import_events(self, events: Iterable[dict[str, Any]]) -> Counter[str]:
        """書き出したイベントを読み込む（この端末のログに、今の日時で追記する）。既にあるものは飛ばす。

        受け付けるのは export_events が書く種類だけ。種類ごとに、実際に足した件数を返す。不正な行は TagError。
        """
        counts: Counter[str] = Counter()
        for n, ev in enumerate(events, 1):
            op = ev.get("op")
            try:
                if op == "attr-def":
                    d = self.attr_defs.get(normalize_tag(str(ev["name"])))
                    new = self.define_attr(str(ev["name"]), str(ev["type"]), ev.get("choices") or ())
                    changed = d != new
                elif op == "alias":
                    changed = self.canonical(str(ev["alias"])) != self.canonical(str(ev["tag"]))
                    if changed:
                        self.add_alias(str(ev["tag"]), str(ev["alias"]))
                elif op == "parent":
                    before = self.dictionary().get(self.canonical(str(ev["tag"])), {}).get("parents", [])
                    self.add_parent(str(ev["tag"]), str(ev["parent"]))
                    changed = self.canonical(str(ev["parent"])) not in before
                elif op == "add":
                    changed = self.add(str(ev["root"]), [str(ev["path"])], str(ev["tag"]), str(ev.get("source", "manual"))) > 0
                elif op == "attr-set":
                    changed = self.set_attr(str(ev["root"]), [str(ev["path"])], str(ev["name"]), ev.get("value"), str(ev.get("source", "manual"))) > 0
                elif op == "relate":
                    changed = self.relate(str(ev["root"]), str(ev["from"]), str(ev["to"]), str(ev.get("kind") or "関連"))
                elif op == "search-save":
                    changed = str(ev["name"]) not in self.searches or self.searches[str(ev["name"])] != str(ev["query"])
                    self.save_search(str(ev["name"]), str(ev["query"]))
                elif op == "dir-type":
                    changed = self.set_dir_type(str(ev["root"]), str(ev["path"]), str(ev.get("type") or ""))
                elif op == "dir-card":
                    changed = self.set_dir_card(str(ev["root"]), str(ev["path"]), str(ev.get("description") or ""), str(ev.get("naming") or ""))
                else:
                    raise TagError(f"読み込めない種類です: {op}")
            except (KeyError, TypeError) as e:
                raise TagError(f"{n} 行目の項目が足りません: {e}") from e
            except TagError as e:
                raise TagError(f"{n} 行目: {e}") from e
            if changed:
                counts[str(op)] += 1
        return counts

    # --- タグでの絞り込み ---

    def tagged_paths(self, root: str, implied: bool = False) -> dict[str, set[str]]:
        """ルートの中で、タグごとに直接付いているアイテムの相対パス（検索用）。

        implied なら、子のタグが付いたアイテムを親のタグにも含める（親子。F-MD-3）。
        """
        with self.lock:
            self.reload_if_changed()
            out: dict[str, set[str]] = {}
            for (r, path), tags in self.items.items():
                if r == root:
                    for tag in tags:
                        out.setdefault(tag, set()).add(path)
            if implied:
                for tag, paths in list(out.items()):
                    for anc in self._ancestors_of(tag):
                        out.setdefault(anc, set()).update(paths)
            return out

    # --- 問い合わせ ---

    def tags_of(self, root: str, path: str) -> dict[str, list[dict[str, str]]]:
        """アイテムのタグ。直接付いたもの（direct）、祖先のフォルダから継承したもの（inherited）、
        親子から成り立つもの（implied。どのタグからか via を添える）。"""
        path = _nk(path)
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
            have = {x["tag"] for x in direct} | {x["tag"] for x in inherited}
            implied: dict[str, str] = {}
            for t in sorted(have):
                for anc in sorted(self._ancestors_of(t)):
                    if anc not in have and anc not in implied:
                        implied[anc] = t
            return {"direct": direct, "inherited": inherited, "implied": [{"tag": k, "via": v} for k, v in sorted(implied.items())]}

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
            tag = self._canonical(tag)
            return sorted(k for k, tags in self.items.items() if tag in tags and (root is None or k[0] == root))

    def tagged_under(self, root: str, path: str) -> dict[str, list[str]]:
        """path の配下で、直接タグが付いているアイテム（相対パス → タグの一覧。パスは NFC）。ファイルブラウザ用。"""
        path = _nk(path)
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
    for raw, _is_dir in entries:
        path = _nk(raw)
        if inc:
            hits = (has(path, s) for s in inc)
            if not (all(hits) if mode == "and" else any(hits)):
                continue
        if any(has(path, s) for s in exc):
            continue
        out.append(raw)
    return out
