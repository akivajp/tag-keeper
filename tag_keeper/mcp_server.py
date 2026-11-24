"""AI エージェント（Claude Code・Claude Desktop など）から使う MCP サーバー（tag-keeper mcp）。

標準入出力（stdio）で MCP を話し、カタログの検索・アイテムの詳細・抽出した本文の読み出し・
タグと属性の付け外し・移動と改名・削除（隔離）を、ツールとしてエージェントに渡す。

- 中身は既存の部品（find_entries・TagStore・タグでの絞り込み・整理プラン）を薄く包むだけ。Web の画面・CLI と同じ結果になる。
- タグは、エージェントが付けたものを出所 "agent" として記録する（人が付けた "manual"、読み取りの "ai" と見分けられる）。
- 移動・改名・削除は、Web の画面と同じく1回ごとの整理プランとして実行する（実行直前の確認・前後のスナップショット・
  実行記録・取り消し）。削除は完全には消さず、隔離フォルダへ移すだけ（隔離を空にするツールは置かない）。
- タグのログは追記専用で、別のプロセスが書いたものも読み直すので、Web の画面のサーバーと同時に動かしてよい。
- 標準出力は MCP の通信に使うため、ログは標準エラー（rich）にだけ出す。

ツールの中身は `AgentTools` にまとめ、MCP の SDK（任意の依存 `mcp`）が無くてもテストできるようにしている。
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tag_keeper import __version__, rules
from tag_keeper.catalog import connect, last_scan, utcnow
from tag_keeper.config import Config, ConfigError, RootConfig, load_config
from tag_keeper.fulltext import KINDS, ORDERS, find_entries, query_terms, snippets
from tag_keeper.plan import (
    ACTION_MOVE,
    ACTION_QUARANTINE,
    CAT_MOVE,
    Plan,
    PlanError,
    PlanOp,
    RunResult,
    SnapshotError,
    Snapper,
    apply_plan,
    fingerprint,
    journal_path,
    new_plan_id,
    undo_plan,
    write_plan,
)
from tag_keeper.syncguard import SyncError, make_guard
from tag_keeper.tags import TagError, TagStore, normalize_tag
from tag_keeper.tags import search as tags_search
from tag_keeper.web.files import PathError, clean_relpath, resolve_within
# 属性の条件の解析と照合は、Web の画面の検索と同じものを使う（結果をそろえるため）
from tag_keeper.web.server import _attrs_match, _parse_attr_cond

log = logging.getLogger(__name__)

# 削除（隔離）のプランのカテゴリ（Web の画面のファイルブラウザからの削除と同じ表示名）
CAT_DELETE = "削除（隔離フォルダへ）"
# 1回の呼び出しで動かせる件数の上限（取り違えで大量に動かさないため）
MAX_FILE_OPS = 500

# エージェントの文脈を食いつぶさないための上限
MAX_RESULTS = 500
MAX_TEXT_CHARS = 200_000

# エージェントへの使い方の説明（MCP の instructions として渡る）
INSTRUCTIONS = """\
tag-keeper のカタログ（既存のフォルダ構成のまま、ファイルにタグ・属性を付けて管理する仕組み）を操作する。
- まず list_roots でルート（管理しているフォルダの単位）の名前を確かめる。パスはすべてルートからの相対パス（"/" 区切り）。
- 探すときは search。名前の語・本文の語・種類・タグ・属性・場所・日付・大きさを組み合わせられる。
- 1件の詳細（付いているタグ・属性・同じ内容のファイル）は get_item、抽出した本文は read_text で読む。
- タグを付ける前に list_tags で既存のタグを確かめ、なるべく既存のタグ（または別名）を使う。むやみに新しいタグを作らない。
- フォルダに付けたタグ・属性は配下のアイテムにも効く（継承）。まとめて付けるときはフォルダに付けるとよい。
- ファイルごとに違うタグを付けるときは、add_tags の items にまとめて1回で渡す。
- まだタグが1つも効いていないもの（未整理）は、search の untagged=true で探せる。件数が多ければ next_offset で続きを読む。
- 表記ゆれは define_tag の aliases で別名にまとめ、分類の上下は parents で表す。属性は define_attr で定義してから set_attr で付ける。
- 移動・改名は move_items、削除は delete_items。削除は隔離フォルダへ移すだけで、どちらも undo_plan で取り消せる。
  利用者に確かめずに消さない。迷うものは消さずに「状態:削除候補」のようなタグを付けて、利用者に判断を任せる。
"""


class AgentTools:
    """エージェントに渡すツールの中身（MCP の SDK に依存しない）。

    設定ファイルはツールを呼ぶたびに読み直す（Web の画面と同じく、編集を再起動なしで反映するため）。
    タグのストアは、ログの置き場所ごとに1つを使い回す（読み直しは TagStore が変更を見て行う）。
    """

    def __init__(self, config_path: Path | None, db_path: Path) -> None:
        """
        Args:
            config_path: 設定ファイル（None なら既定の場所）。
            db_path: カタログのデータベース。
        """
        self.config_path = config_path
        self.db_path = db_path
        self._stores: dict[Path, TagStore] = {}

    # --- 内部の部品 ---

    def _config(self) -> Config:
        return load_config(self.config_path)

    def _store(self, config: Config) -> TagStore:
        d = config.tags.dir
        if d not in self._stores:
            self._stores[d] = TagStore(d)
        return self._stores[d]

    @staticmethod
    def _root(config: Config, name: str) -> RootConfig:
        rc = config.find_root(name)
        if rc is None:
            names = "、".join(r.name for r in config.roots) or "（なし）"
            raise ConfigError(f"ルートがありません: {name}（設定済みのルート: {names}）")
        return rc

    @staticmethod
    def _relpath(rc: RootConfig, raw: str, *, must_exist: bool = True) -> str:
        """エージェントから受け取ったパスを、ルートからの相対パスに整える（ルートの外・.. は拒む）。"""
        raw = raw.strip()
        p = Path(raw)
        if p.is_absolute():
            # ルートの中の絶対パスも受け付ける（エージェントが手元のパスをそのまま渡すことがあるため）
            try:
                raw = p.relative_to(rc.path).as_posix()
            except ValueError as e:
                raise PathError(f"ルート {rc.name} の外を指しています: {raw}") from e
            raw = "" if raw == "." else raw
        rel = clean_relpath(raw)
        if must_exist and not resolve_within(rc.path, rel).exists():
            raise PathError(f"ありません: {rc.name}/{rel}")
        return rel

    # --- 読み取り ---

    def list_roots(self) -> list[dict[str, Any]]:
        """設定済みのルートと、カタログの状態（件数・最後の走査）を返す。"""
        config = self._config()
        conn = connect(self.db_path)
        try:
            out = []
            for rc in config.roots:
                row = conn.execute("SELECT id FROM roots WHERE name = ?", (rc.name,)).fetchone()
                files = 0
                scanned = None
                if row is not None:
                    files = int(
                        conn.execute(
                            "SELECT count(*) FROM entries WHERE root_id = ? AND gone_at IS NULL AND is_dir = 0", (row["id"],)
                        ).fetchone()[0]
                    )
                    scan = last_scan(conn, int(row["id"]))
                    scanned = scan["finished_at"] if scan is not None else None
                out.append({"name": rc.name, "path": str(rc.path), "files": files, "last_scan": scanned, "scanned": row is not None})
            return out
        finally:
            conn.close()

    def search(
        self,
        root: str = "",
        words: list[str] | None = None,
        text: str = "",
        kind: str = "",
        tags: list[str] | None = None,
        exclude_tags: list[str] | None = None,
        any_tag: bool = False,
        attrs: list[str] | None = None,
        under: str = "",
        after: str = "",
        before: str = "",
        min_size: int | None = None,
        max_size: int | None = None,
        folders: bool = False,
        limit: int = 50,
        untagged: bool = False,
        direct_only: bool = False,
        offset: int = 0,
        order: str = "",
    ) -> dict[str, Any]:
        """アイテムを探す（Web の画面の検索と同じ条件）。root を省くと全ルートを探す。

        untagged はタグが1つも効いていないもの、direct_only はフォルダからの継承を見ずに、直接付いたタグだけで判定する。
        offset は先頭から飛ばす件数（既定はルート名・パスの順に並ぶので、next_offset を渡せば続きが読める）。
        order は並び順（""・"mtime"・"added"。fulltext.ORDERS）。指定すると全ルートを通して新しい順に並べ、ほかの条件が無くても探せる。
        """
        config = self._config()
        store = self._store(config)
        words = [w for w in (words or []) if w.strip()]
        include = [store.canonical(t) for t in (tags or []) if t.strip()]
        exclude = [store.canonical(t) for t in (exclude_tags or []) if t.strip()]
        conds = [_parse_attr_cond(a) for a in (attrs or []) if a.strip()]
        if kind and kind not in KINDS:
            raise ValueError(f"種類が不正です: {kind}（使えるもの: {', '.join(sorted(KINDS))}）")
        if not (words or text.strip() or kind or include or exclude or conds or under or after or before
                or min_size is not None or max_size is not None or untagged or order):
            raise ValueError("探す条件（words・text・kind・tags・exclude_tags・untagged・attrs・under・after・before・大きさ のどれか）を指定してください")
        if untagged and include:
            raise ValueError("untagged（タグ無し）と tags は一緒に指定できません")
        if order not in ORDERS:
            raise ValueError(f"並び順が不正です: {order}（使えるもの: mtime・added・空）")
        limit = max(1, min(int(limit), MAX_RESULTS))
        offset = max(0, int(offset))
        under_rel = clean_relpath(under)
        terms = query_terms(text)
        roots = [self._root(config, root)] if root else list(config.roots)
        # 当てはまったもの（ルート名, パス, 行）。並び順はルートの順のあと、各ルートの中で order の順
        matched: list[tuple[str, str, Any]] = []
        conn = connect(self.db_path)
        try:
            for rc in roots:
                row = conn.execute("SELECT id FROM roots WHERE name = ?", (rc.name,)).fetchone()
                if row is None:
                    # まだ走査していないルートは飛ばす
                    continue
                try:
                    rows = find_entries(
                        conn, int(row["id"]), words=words, text=text, kind=kind, files_only=not folders, under=under_rel,
                        after=after, before=before, min_size=min_size, max_size=max_size, order=order,
                    )
                except ValueError as e:
                    raise ValueError(f"日付は YYYY-MM-DD で指定してください（{e}）") from None
                info = {r["relpath"]: r for r in rows}
                paths = list(info)
                if include or exclude or untagged:
                    paths = tags_search(
                        ((p, bool(info[p]["is_dir"])) for p in paths),
                        store.tagged_paths(rc.name, implied=True),
                        include, "or" if any_tag else "and", exclude,
                        inherit=not direct_only, untagged=untagged,
                    )
                if conds:
                    defs = {d["name"]: d for d in store.attr_defs_list()}
                    values = store.attr_paths(rc.name)
                    paths = [p for p in paths if _attrs_match(p, conds, values, defs)]
                matched.extend((rc.name, p, info[p]) for p in paths)
            if order and len(roots) > 1:
                # 全ルートを通して新しい順にする（同じ日時は、設定のルートの順・パスの順のまま。sort は安定）
                col = "mtime_ns" if order == "mtime" else "first_seen"
                matched.sort(key=lambda m: m[2][col], reverse=True)
            total = len(matched)
            shown = matched[offset : offset + limit]
            snips = snippets(conn, [m[2]["sha256"] for m in shown if m[2]["sha256"]], terms) if terms else {}
            items: list[dict[str, Any]] = []
            for name, p, r in shown:
                items.append({
                    "root": name,
                    "path": p,
                    "is_dir": bool(r["is_dir"]),
                    "size": r["size"],
                    "modified": _iso(r["mtime_ns"]),
                    # カタログに初めて現れた日時（この端末の時刻帯）
                    "added": _iso_text(r["first_seen"]),
                    "tags": _flat_tags(store.tags_of(name, p)),
                    "attrs": _flat_attrs(store.attrs_of(name, p)),
                    **({"snippet": snips[r["sha256"]]} if r["sha256"] in snips else {}),
                })
        finally:
            conn.close()
        out: dict[str, Any] = {"total": total, "offset": offset, "returned": len(items), "items": items}
        if offset + len(items) < total:
            # 続きがあれば、次に渡す offset を添える
            out["next_offset"] = offset + len(items)
        return out

    def get_item(self, root: str, path: str) -> dict[str, Any]:
        """ファイル・フォルダ1件の詳細（タグ・属性・関係・同じ内容のファイル・抽出した本文の有無）を返す。"""
        config = self._config()
        rc = self._root(config, root)
        rel = self._relpath(rc, path)
        store = self._store(config)
        full = resolve_within(rc.path, rel)
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT e.sha256, e.size, e.mtime_ns, e.first_seen FROM entries e JOIN roots r ON r.id = e.root_id"
                " WHERE r.name = ? AND e.relpath = ? AND e.gone_at IS NULL",
                (rc.name, rel),
            ).fetchone()
            same: list[str] = []
            extract = None
            if row is not None and row["sha256"]:
                same = [
                    r["relpath"]
                    for r in conn.execute(
                        "SELECT e.relpath FROM entries e JOIN roots r ON r.id = e.root_id"
                        " WHERE r.name = ? AND e.sha256 = ? AND e.gone_at IS NULL AND e.relpath != ? LIMIT 20",
                        (rc.name, row["sha256"], rel),
                    )
                ]
                x = conn.execute("SELECT method, length(text) AS n FROM extracts WHERE sha256 = ?", (row["sha256"],)).fetchone()
                if x is not None:
                    extract = {"method": x["method"], "chars": x["n"]}
        finally:
            conn.close()
        out: dict[str, Any] = {
            "root": rc.name,
            "path": rel,
            "is_dir": full.is_dir(),
            "in_catalog": row is not None,
            # 直接付いたものと、フォルダから継承したものを分けて返す
            "tags": store.tags_of(rc.name, rel),
            "attrs": store.attrs_of(rc.name, rel),
            "relations": store.relations_of(rc.name, rel),
            "same_content": same,
            "extracted_text": extract,
        }
        if row is not None:
            out.update({"size": row["size"], "modified": _iso(row["mtime_ns"]), "first_seen": row["first_seen"]})
        if full.is_dir():
            # フォルダなら直下の一覧も添える（多すぎるときは先頭だけ）
            children = sorted(full.iterdir(), key=lambda c: (not c.is_dir(), c.name))
            out["children"] = [c.name + ("/" if c.is_dir() else "") for c in children[:200]]
            out["children_total"] = len(children)
        return out

    def read_text(self, root: str, path: str, offset: int = 0, max_chars: int = 20_000) -> dict[str, Any]:
        """ファイルから抽出した本文（全文検索の索引にあるもの。書き起こし・OCR を含む）を返す。"""
        config = self._config()
        rc = self._root(config, root)
        rel = self._relpath(rc, path)
        conn = connect(self.db_path)
        try:
            x = conn.execute(
                "SELECT x.method, x.text FROM entries e JOIN roots r ON r.id = e.root_id JOIN extracts x ON x.sha256 = e.sha256"
                " WHERE r.name = ? AND e.relpath = ? AND e.gone_at IS NULL",
                (rc.name, rel),
            ).fetchone()
        finally:
            conn.close()
        if x is None or x["text"] is None:
            raise ValueError(f"本文がまだ抽出されていません: {rc.name}/{rel}（対応していない形式か、抽出がまだのもの）")
        text: str = x["text"]
        offset = max(0, int(offset))
        max_chars = max(1, min(int(max_chars), MAX_TEXT_CHARS))
        part = text[offset: offset + max_chars]
        return {
            "root": rc.name, "path": rel, "method": x["method"], "total_chars": len(text),
            "offset": offset, "text": part, "truncated": offset + len(part) < len(text),
        }

    def list_tags(self) -> dict[str, Any]:
        """タグ（件数・別名・親）・属性の定義・保存した検索を返す。

        count は直接付いているアイテム（ファイル・フォルダ）の数、files はフォルダからの継承と親子を含めて
        そのタグが効いているファイルの数（そのタグで search したときの件数）。
        """
        config = self._config()
        store = self._store(config)
        # 先に辞書を読み、続けて件数を読む（CLI の tags と同じ順）
        dictionary = store.dictionary()
        counts = store.all_tags()
        effective = self._effective_counts(config, store)
        tags = []
        for tag in sorted(set(counts) | set(dictionary)):
            d = dictionary.get(tag, {})
            entry: dict[str, Any] = {"tag": tag, "count": counts.get(tag, 0), "files": effective.get(tag, 0)}
            if d.get("aliases"):
                entry["aliases"] = d["aliases"]
            if d.get("parents"):
                entry["parents"] = d["parents"]
            tags.append(entry)
        return {"tags": tags, "attributes": store.attr_defs_list(), "saved_searches": store.saved_searches()}

    def _effective_counts(self, config: Config, store: TagStore) -> dict[str, int]:
        """タグごとの、継承と親子を含めて効いているファイルの数（全ルートの合計）。"""
        out: dict[str, int] = {}
        conn = connect(self.db_path)
        try:
            for rc in config.roots:
                files = [
                    r["relpath"]
                    for r in conn.execute(
                        "SELECT e.relpath FROM entries e JOIN roots r ON r.id = e.root_id"
                        " WHERE r.name = ? AND e.gone_at IS NULL AND e.is_dir = 0",
                        (rc.name,),
                    )
                ]
                for tag, n in store.effective_counts(rc.name, files).items():
                    out[tag] = out.get(tag, 0) + n
        finally:
            conn.close()
        return out

    # --- 書き込み（タグのログ） ---

    def add_tags(
        self, root: str, paths: list[str] | None = None, tags: list[str] | None = None,
        items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """アイテムにタグを付ける（別名は正式な名前になる）。付けた件数をタグごとに返す。

        paths と tags の組を1つ渡すか、items（[{"paths": [...], "tags": [...]}, …]）で組をいくつも渡す。
        どれかの組が不正なら、何も付けずにエラーにする（途中まで付いた状態を残さない）。
        """
        config = self._config()
        rc = self._root(config, root)
        store = self._store(config)
        groups: list[tuple[list[str], list[str]]] = []
        if paths or tags:
            groups.append((list(paths or []), list(tags or [])))
        for i, it in enumerate(items or []):
            if not isinstance(it, dict):
                raise ValueError(f"items[{i}] は {{\"paths\": [...], \"tags\": [...]}} の形にしてください")
            one = it.get("paths") or ([it["path"]] if it.get("path") else [])
            groups.append((list(one), list(it.get("tags") or [])))
        if not groups:
            raise ValueError("paths と tags、または items を指定してください")
        # 先に全部を確かめる（パスの誤りは、1つも付ける前に知らせる）
        checked = [
            ([self._relpath(rc, p) for p in _nonempty(ps, "paths")], _nonempty(ts, "tags")) for ps, ts in groups
        ]
        added: dict[str, int] = {}
        touched: list[str] = []
        for rels, ts in checked:
            for tag in ts:
                n = store.add(rc.name, rels, tag, source="agent")
                # 同じタグ（別名を含む）が重ねて渡されても、件数は足し上げる
                key = store.canonical(tag)
                added[key] = added.get(key, 0) + n
            touched += [r for r in rels if r not in touched]
        log.info("エージェントがタグを付けました: %s %s", rc.name, added)
        return {"root": rc.name, "paths": touched, "added": added}

    def remove_tags(self, root: str, paths: list[str], tags: list[str]) -> dict[str, Any]:
        """アイテムからタグを外す（直接付いたものだけ。継承したものは元のフォルダから外す）。"""
        rc, rels, store = self._targets(root, paths, must_exist=False)
        removed = {}
        for tag in _nonempty(tags, "tags"):
            key = store.canonical(tag)
            removed[key] = removed.get(key, 0) + store.remove(rc.name, rels, tag)
        log.info("エージェントがタグを外しました: %s %s", rc.name, removed)
        return {"root": rc.name, "paths": rels, "removed": removed}

    def set_attr(self, root: str, paths: list[str], name: str, value: str) -> dict[str, Any]:
        """属性の値を付ける（定義済みの属性だけ。型と選択肢に合わない値は拒む）。"""
        rc, rels, store = self._targets(root, paths)
        n = store.set_attr(rc.name, rels, name, value, source="agent")
        log.info("エージェントが属性を付けました: %s %s=%s（%d 件）", rc.name, name, value, n)
        return {"root": rc.name, "paths": rels, "name": name, "value": value, "changed": n}

    def unset_attr(self, root: str, paths: list[str], name: str) -> dict[str, Any]:
        """属性の値を外す。"""
        rc, rels, store = self._targets(root, paths, must_exist=False)
        n = store.unset_attr(rc.name, rels, name)
        return {"root": rc.name, "paths": rels, "name": name, "changed": n}

    # --- 書き込み（タグの辞書・属性の定義） ---

    def define_tag(
        self, tag: str, aliases: list[str] | None = None, parents: list[str] | None = None,
        remove_aliases: list[str] | None = None, remove_parents: list[str] | None = None,
    ) -> dict[str, Any]:
        """タグの別名・親を登録する（または外す）。登録後のそのタグの辞書（別名・親・子）を返す。

        別名に既に使われているタグを渡すと、そのタグが付いたアイテムは正式な名前へまとめられる。
        親を登録すると、tag が付いたものは親のタグでも探せるようになる（循環するものは断る）。
        """
        config = self._config()
        store = self._store(config)
        canon = store.canonical(tag)
        if not canon:
            raise ValueError("tag を指定してください")
        if not (aliases or parents or remove_aliases or remove_parents):
            raise ValueError("aliases・parents・remove_aliases・remove_parents のどれかを指定してください")
        # 外すものを先に、足すものを後に（入れ替えを1回で頼めるように）
        own_aliases = set(store.dictionary().get(canon, {}).get("aliases", []))
        for a in remove_aliases or []:
            if not a.strip():
                continue
            # ほかのタグの別名を、取り違えて外さない
            if normalize_tag(a) not in own_aliases:
                raise TagError(f"{canon} の別名ではありません: {a}")
            store.remove_alias(a)
        for p in remove_parents or []:
            if p.strip():
                store.remove_parent(canon, p)
        for a in aliases or []:
            if a.strip():
                store.add_alias(canon, a)
        for p in parents or []:
            if p.strip():
                store.add_parent(canon, p)
        entry = store.dictionary().get(canon, {"aliases": [], "parents": [], "children": []})
        log.info("エージェントがタグの辞書を変えました: %s %s", canon, entry)
        return {"tag": canon, **entry}

    def rename_tag(self, old: str, new: str) -> dict[str, Any]:
        """タグの名前を変える（全ルートのすべてのアイテムと、別名・親子の中の名前も）。変わったアイテムの数を返す。

        new が既にあるタグなら、old はそのタグへ統合される（後から分けられない）。
        """
        store = self._store(self._config())
        if not old.strip() or not new.strip():
            raise ValueError("old と new を指定してください")
        src, dest = store.canonical(old), store.canonical(new)
        if src not in store.dictionary():
            raise ValueError(f"そのタグはありません: {old}")
        merged = dest in store.dictionary()
        n = store.rename_tag(src, dest)
        log.info("エージェントがタグの名前を変えました: %s → %s（%d 件）", src, dest, n)
        return {"from": src, "to": dest, "changed": n, "merged": merged}

    def define_attr(self, name: str, type: str, choices: list[str] | None = None) -> dict[str, Any]:  # noqa: A002
        """属性を定義する（同じ名前なら定義し直す）。type は choice・flag・date・number・text。choice は選択肢を2つ以上。"""
        store = self._store(self._config())
        if not name.strip():
            raise ValueError("name を指定してください")
        d = store.define_attr(name, type, choices or ())
        log.info("エージェントが属性を定義しました: %s", d.to_dict())
        return d.to_dict()

    # --- 書き込み（ファイルを動かす。1回ごとの整理プランとして実行する） ---

    def move_items(self, root: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        """ファイル・フォルダを移動・改名する（items は [{"path": 今の場所, "dest": 新しい場所（名前を含む）}, …]）。

        タグ・属性・関係は移動に追従する。移動先のフォルダが無ければ作る。既にあるものは上書きしない。
        """
        config = self._config()
        rc = self._root(config, root)
        if not items or not all(isinstance(it, dict) for it in items):
            raise ValueError('items は [{"path": ..., "dest": ...}] の形で1つ以上指定してください')
        if len(items) > MAX_FILE_OPS:
            raise ValueError(f"1回に動かせるのは {MAX_FILE_OPS} 件までです（{len(items)} 件）")
        ops: list[PlanOp] = []
        dests: set[str] = set()
        for it in items:
            src = self._relpath(rc, str(it.get("path", "")))
            dest = self._relpath(rc, str(it.get("dest", "")), must_exist=False)
            if not src or not dest:
                raise PathError("ルートそのものは動かせません")
            if dest == src:
                continue
            if dest.startswith(src + "/"):
                raise PathError(f"フォルダをそれ自身の中へは移せません: {src}")
            problem = rules.cloud_name_problem(dest)
            if problem:
                raise PathError(f"{dest}: {problem}")
            clash = rules.case_conflict(rc.path, dest, src)
            if rules.name_key(dest) in dests or (rc.path / dest).exists() or (rc.path / dest).is_symlink() or clash:
                raise PathError(f"移動先に同じ名前のものがあります: {clash or dest}")
            dests.add(rules.name_key(dest))
            ops.append(self._op(rc, ACTION_MOVE, src, CAT_MOVE, str(it.get("reason", "") or "エージェントによる移動")[:200], dest))
        if not ops:
            raise ValueError("動かすものがありません（名前も場所も変わらない）")
        return self._run_plan(config, rc, ops, "move")

    def delete_items(self, root: str, paths: list[str], reason: str = "") -> dict[str, Any]:
        """ファイル・フォルダを削除する（完全には消さず、隔離フォルダへ移す。undo_plan で元に戻せる）。"""
        config = self._config()
        rc = self._root(config, root)
        rels = list(dict.fromkeys(self._relpath(rc, p) for p in _nonempty(paths, "paths")))
        if "" in rels:
            raise PathError("ルートそのものは削除できません")
        if len(rels) > MAX_FILE_OPS:
            raise ValueError(f"1回に削除できるのは {MAX_FILE_OPS} 件までです（{len(rels)} 件）")
        # 選んだフォルダの中にあるものは、フォルダごと移るので重ねて扱わない
        rels = [r for r in rels if not any(r.startswith(o + "/") for o in rels)]
        why = (reason or "エージェントによる削除")[:200]
        ops = [self._op(rc, ACTION_QUARANTINE, r, CAT_DELETE, why) for r in rels]
        return self._run_plan(config, rc, ops, "delete")

    def undo_plan(self, plan_id: str) -> dict[str, Any]:
        """move_items・delete_items（や、ほかの整理プラン）の実行を取り消し、動かしたものを元の場所へ戻す。"""
        config = self._config()
        if not journal_path(self._journal_dir, plan_id).exists():
            raise ValueError(f"実行記録がありません: {plan_id}")
        conn = connect(self.db_path)
        try:
            result = undo_plan(
                conn, plan_id, journal_dir=self._journal_dir,
                snapshot=Snapper(config.plan.snapper_config) if config.plan.snapper_config else None,
                tags=self._store(config),
            )
        finally:
            conn.close()
        log.info("エージェントがプランを取り消しました: %s（戻した %d 件）", plan_id, len(result.done))
        return {"plan_id": plan_id, **_outcomes(result, "restored")}

    @property
    def _journal_dir(self) -> Path:
        """実行記録の置き場所（CLI・Web の画面と同じ、カタログの隣の journal/）。"""
        return Path(self.db_path).parent / "journal"

    @staticmethod
    def _op(rc: RootConfig, action: str, rel: str, category: str, reason: str, dest: str = "") -> PlanOp:
        """今の状態（inode・件数・大きさ・更新日時）を控えた操作を作る。シンボリックリンクは扱わない。"""
        full = resolve_within(rc.path, rel)
        fp = fingerprint(full)
        if fp is None or full.is_symlink():
            raise PathError(f"ありません: {rc.name}/{rel}")
        return PlanOp(action, rel, full.is_dir(), fp.files, fp.size, fp.inode, fp.mtime_ns, category, reason, dest=dest)

    def _run_plan(self, config: Config, rc: RootConfig, ops: list[PlanOp], kind: str) -> dict[str, Any]:
        """操作を1つの整理プランとして書き出し、すぐ実行する（Web の画面のファイル操作と同じ流れ）。"""
        plans_dir = Path(self.db_path).parent / "plans"
        plan = Plan(id=new_plan_id(rc.name, f"agent-{kind}"), root=rc.name, root_path=rc.path, created_at=utcnow(), ops=ops)
        base, n = plan.id, 1
        while (plans_dir / f"{plan.id}.toml").exists() or journal_path(self._journal_dir, plan.id).exists():
            n += 1
            plan.id = f"{base}-{n}"
        write_plan(plan, plans_dir / f"{plan.id}.toml")
        conn = connect(self.db_path)
        try:
            result = apply_plan(
                conn, plan,
                quarantine_dir=config.plan.quarantine_dir,
                journal_dir=self._journal_dir,
                snapshot=Snapper(config.plan.snapper_config) if config.plan.snapper_config else None,
                sync=make_guard(rc.sync_client, rc.sync_service, rc.sync_guard),
                tags=self._store(config),
            )
        finally:
            conn.close()
        log.info("エージェントがプランを実行しました: %s（%d 件）", plan.id, len(result.done))
        out: dict[str, Any] = {"root": rc.name, "plan_id": plan.id, **_outcomes(result, "done")}
        out["undo"] = f"取り消すには undo_plan(plan_id=\"{plan.id}\")"
        if result.sync_error:
            out["sync_error"] = result.sync_error
        return out

    def _targets(self, root: str, paths: list[str], *, must_exist: bool = True) -> tuple[RootConfig, list[str], TagStore]:
        config = self._config()
        rc = self._root(config, root)
        rels = [self._relpath(rc, p, must_exist=must_exist) for p in _nonempty(paths, "paths")]
        return rc, rels, self._store(config)


def _outcomes(result: RunResult, key: str) -> dict[str, Any]:
    """実行・取り消しの結果を、できたもの（パスの一覧）と飛ばしたもの（理由つき）に分ける。"""
    return {
        key: [o.path for o in result.done],
        "skipped": [{"path": o.path, "reason": o.detail} for o in result.skipped],
    }


def _nonempty(values: list[str] | None, name: str) -> list[str]:
    out = [v for v in (values or []) if v.strip()]
    if not out:
        raise ValueError(f"{name} を1つ以上指定してください")
    return out


def _iso(mtime_ns: int | None) -> str | None:
    if mtime_ns is None:
        return None
    return datetime.fromtimestamp(mtime_ns / 1e9, timezone.utc).astimezone().isoformat(timespec="seconds")


def _iso_text(stamp: str | None) -> str | None:
    """カタログに残した UTC の ISO 8601 文字列を、この端末の時刻帯にそろえる（modified と同じ形にするため）。"""
    if not stamp:
        return None
    return datetime.fromisoformat(stamp).astimezone().isoformat(timespec="seconds")


def _flat_tags(tags: dict[str, list[dict[str, str]]]) -> list[str]:
    """tags_of の結果（直接・継承・親子の別）を、検索結果向けに効いているタグの一覧へまとめる。"""
    return sorted({t["tag"] for group in ("direct", "inherited", "implied") for t in tags.get(group, [])})


def _flat_attrs(attrs: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """attrs_of の結果（直接・継承の別）を、検索結果向けに「名前 → 値」へまとめる（直接付いたものを優先）。"""
    out: dict[str, Any] = {}
    for group in ("inherited", "direct"):
        for a in attrs.get(group, []):
            out[a["name"]] = a["value"]
    return out


def build_server(tools: AgentTools) -> Any:
    """ツールを MCP サーバー（mcp の MCPServer）に登録して返す。

    Raises:
        ImportError: 任意の依存 mcp が入っていない。
    """
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    server = MCPServer("tag-keeper", instructions=INSTRUCTIONS, version=__version__)
    read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    writes = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    # ファイルを動かすもの（取り消せるが、同じ呼び出しを繰り返すと結果が変わる）
    moves = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
    deletes = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)

    def guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
        """想定内のエラー（ルート・パス・タグの誤り）を、エージェントが読める ToolError にする。"""

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except (ConfigError, PathError, TagError, PlanError, SnapshotError, SyncError, ValueError, OSError) as e:
                raise ToolError(str(e)) from e

        return wrapper

    @server.tool(annotations=read_only)
    @guarded
    def list_roots() -> list[dict[str, Any]]:
        """管理しているルート（フォルダの単位）の名前・場所・ファイル数・最後の走査を一覧する。ほかのツールの root にはこの name を使う。"""
        return tools.list_roots()

    @server.tool(annotations=read_only)
    @guarded
    def search(
        root: str = "",
        words: list[str] | None = None,
        text: str = "",
        kind: str = "",
        tags: list[str] | None = None,
        exclude_tags: list[str] | None = None,
        any_tag: bool = False,
        attrs: list[str] | None = None,
        under: str = "",
        after: str = "",
        before: str = "",
        min_size: int | None = None,
        max_size: int | None = None,
        folders: bool = False,
        limit: int = 50,
        untagged: bool = False,
        direct_only: bool = False,
        offset: int = 0,
        order: str = "",
    ) -> dict[str, Any]:
        """ファイル（folders=true ならフォルダも）を探す。条件はすべて AND で、少なくとも1つ要る。

        Args:
            root: 探すルートの名前。空なら全ルート。
            words: 名前・フォルダのパスに含む語（すべて含むもの。大文字・小文字は区別しない）。
            text: 本文に含む語（空白区切りで、すべて含むもの。抽出済みの本文・書き起こし・OCR が対象）。
            kind: 種類（document・pdf・office・text・image・audio・video・archive。document は PDF・Office・テキスト）。
            tags: 付いているタグ（既定はすべて含むもの。別名・親子・フォルダからの継承を考慮）。
            exclude_tags: 付いていないタグ。
            any_tag: true なら tags のどれかを含むもの。
            attrs: 属性の条件（「名前」で付いているもの、「名前=値」「名前>=値」「名前<値」など。文字の属性の = は部分一致）。
            under: このフォルダの配下だけ（ルートからの相対パス）。
            after: この日（YYYY-MM-DD）以降に更新されたもの。
            before: この日（YYYY-MM-DD）より前に更新されたもの。
            min_size: この大きさ（バイト）以上。
            max_size: この大きさ（バイト）以下。
            folders: フォルダも結果に含める。
            limit: 返す件数の上限（最大 500）。total には全件数が入る。
            untagged: true なら、タグが1つも効いていないもの（未整理）だけ。tags とは一緒に使えない。
            direct_only: true なら、フォルダからの継承を見ず、そのアイテムに直接付いたタグだけで tags・exclude_tags・untagged を判定する。
            offset: 先頭から飛ばす件数。結果に next_offset があれば、それを渡すと続きが返る（既定の並びはルート・パスの順）。
            order: 並び順。"mtime" は更新日時の新しい順、"added" はカタログに現れた（追加された）日時の新しい順で、全ルートを通して並べる。
                指定すれば、ほかの条件が無くても全件から新しい順に返す（最近の追加・更新を見るとき）。結果の added が追加日時。
        """
        return tools.search(root, words, text, kind, tags, exclude_tags, any_tag, attrs, under, after, before,
                            min_size, max_size, folders, limit, untagged, direct_only, offset, order)

    @server.tool(annotations=read_only)
    @guarded
    def get_item(root: str, path: str) -> dict[str, Any]:
        """ファイル・フォルダ1件の詳細を返す: 直接付いた・継承したタグと属性、関係、同じ内容のファイル、本文の抽出状況。フォルダなら直下の一覧も。"""
        return tools.get_item(root, path)

    @server.tool(annotations=read_only)
    @guarded
    def read_text(root: str, path: str, offset: int = 0, max_chars: int = 20000) -> dict[str, Any]:
        """ファイルから抽出した本文（PDF・Office 文書・テキスト・OCR・書き起こし）を読む。長い本文は offset で続きを読む。"""
        return tools.read_text(root, path, offset, max_chars)

    @server.tool(annotations=read_only)
    @guarded
    def list_tags() -> dict[str, Any]:
        """既存のタグ（別名・親）、属性の定義（型・選択肢）、保存した検索を一覧する。タグを付ける前に確かめる。

        count は直接付いているアイテムの数、files はフォルダからの継承を含めてそのタグが効いているファイルの数。
        """
        return tools.list_tags()

    @server.tool(annotations=writes)
    @guarded
    def add_tags(
        root: str, paths: list[str] | None = None, tags: list[str] | None = None,
        items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """ファイル・フォルダにタグを付ける（ファイル自体は変えない）。別名は正式な名前になる。フォルダに付けると配下にも効く。

        Args:
            root: ルートの名前。
            paths: ルートからの相対パス（複数可）。
            tags: 付けるタグ（複数可。「分類:値」の形も使える）。paths のすべてに付ける。
            items: ファイルごとに違うタグを付けるときの組の一覧（[{"paths": [...], "tags": [...]}, …]）。
                paths・tags の代わりに（または併せて）渡すと、1回の呼び出しで済む。
        """
        return tools.add_tags(root, paths, tags, items)

    @server.tool(annotations=writes)
    @guarded
    def remove_tags(root: str, paths: list[str], tags: list[str]) -> dict[str, Any]:
        """ファイル・フォルダからタグを外す（直接付いたものだけ。継承したタグは元のフォルダから外す）。"""
        return tools.remove_tags(root, paths, tags)

    @server.tool(annotations=writes)
    @guarded
    def set_attr(root: str, paths: list[str], name: str, value: str) -> dict[str, Any]:
        """ファイル・フォルダに属性の値を付ける（list_tags の attributes にある定義済みの属性だけ。型と選択肢に合う値）。"""
        return tools.set_attr(root, paths, name, value)

    @server.tool(annotations=writes)
    @guarded
    def unset_attr(root: str, paths: list[str], name: str) -> dict[str, Any]:
        """ファイル・フォルダから属性の値を外す。"""
        return tools.unset_attr(root, paths, name)

    @server.tool(annotations=writes)
    @guarded
    def define_tag(
        tag: str, aliases: list[str] | None = None, parents: list[str] | None = None,
        remove_aliases: list[str] | None = None, remove_parents: list[str] | None = None,
    ) -> dict[str, Any]:
        """タグの別名・親（分類の上下）を登録する、または外す。登録後のそのタグの別名・親・子を返す。

        Args:
            tag: 対象のタグ（別名を渡すと正式な名前に読み替える）。
            aliases: 別名にする名前（表記ゆれなど）。既にタグとして使われている名前なら、付いたアイテムごと tag へまとめる。
            parents: 親にするタグ。tag が付いたものは親のタグでも探せるようになる（循環するものは断る）。
            remove_aliases: 外す別名。
            remove_parents: 外す親。
        """
        return tools.define_tag(tag, aliases, parents, remove_aliases, remove_parents)

    @server.tool(annotations=deletes)
    @guarded
    def rename_tag(old: str, new: str) -> dict[str, Any]:
        """タグの名前を変える（全ルートのすべてのアイテム、別名・親子の中の名前も）。

        new が既にあるタグなら old はそれへ統合され、後から分けられない（結果の merged が true）。統合は利用者に確かめてから行う。
        """
        return tools.rename_tag(old, new)

    @server.tool(annotations=writes)
    @guarded
    def define_attr(name: str, type: str, choices: list[str] | None = None) -> dict[str, Any]:  # noqa: A002
        """属性を定義する（set_attr で値を付ける前に要る。同じ名前なら定義し直す）。

        Args:
            name: 属性の名前（例: 金額・期限・状態）。
            type: choice（択一）・flag（あり/なし）・date（YYYY-MM-DD）・number（数）・text（文字）のどれか。
            choices: choice の選択肢（2つ以上）。
        """
        return tools.define_attr(name, type, choices)

    @server.tool(annotations=moves)
    @guarded
    def move_items(root: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        """ファイル・フォルダを移動・改名する。タグ・属性は追従し、undo_plan で取り消せる。既にある名前へは上書きしない。

        Args:
            root: ルートの名前。
            items: [{"path": 今の相対パス, "dest": 新しい相対パス（ファイル名・フォルダ名まで含める）}, …]。
                移動先のフォルダが無ければ作る。名前だけ変えるなら、同じフォルダの中の新しい名前を dest にする。
        """
        return tools.move_items(root, items)

    @server.tool(annotations=deletes)
    @guarded
    def delete_items(root: str, paths: list[str], reason: str = "") -> dict[str, Any]:
        """ファイル・フォルダを削除する。完全には消さず隔離フォルダへ移すだけで、undo_plan で元に戻せる（同期先のクラウドからは消える）。

        利用者が削除を頼んだものか、同じ内容の写しが別にあると確かめたものだけに使う。迷うものはタグで印を付けて利用者に任せる。

        Args:
            root: ルートの名前。
            paths: 削除するものの相対パス（フォルダは中身ごと）。
            reason: 削除の理由（プランの画面に残る）。
        """
        return tools.delete_items(root, paths, reason)

    @server.tool(annotations=moves)
    @guarded
    def undo_plan(plan_id: str) -> dict[str, Any]:
        """move_items・delete_items の実行を取り消す（結果の plan_id を渡す）。元の場所に別のものがあれば、上書きせずに飛ばす。"""
        return tools.undo_plan(plan_id)

    return server


def run_stdio(config_path: Path | None, db_path: Path) -> None:
    """MCP サーバーを標準入出力で動かす（エージェントのクライアントが子プロセスとして起動する）。"""
    server = build_server(AgentTools(config_path, db_path))
    log.info("MCP サーバーを標準入出力で動かします（カタログ: %s）", db_path)
    server.run("stdio")
