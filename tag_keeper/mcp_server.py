"""AI エージェント（Claude Code・Claude Desktop など）から使う MCP サーバー（tag-keeper mcp）。

標準入出力（stdio）で MCP を話し、カタログの検索・アイテムの詳細・抽出した本文の読み出し・
タグと属性の付け外しを、ツールとしてエージェントに渡す。

- 中身は既存の部品（find_entries・TagStore・タグでの絞り込み）を薄く包むだけ。Web の画面・CLI と同じ結果になる。
- ファイルそのものには書き込まない（移動・改名・削除のツールは置かない）。書き込むのはタグのログだけで、
  エージェントが付けたものは出所 "agent" として記録する（人が付けた "manual"、読み取りの "ai" と見分けられる）。
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

from tag_keeper import __version__
from tag_keeper.catalog import connect, last_scan
from tag_keeper.config import Config, ConfigError, RootConfig, load_config
from tag_keeper.fulltext import KINDS, find_entries, query_terms, snippets
from tag_keeper.tags import TagError, TagStore
from tag_keeper.tags import search as tags_search
from tag_keeper.web.files import PathError, clean_relpath, resolve_within
# 属性の条件の解析と照合は、Web の画面の検索と同じものを使う（結果をそろえるため）
from tag_keeper.web.server import _attrs_match, _parse_attr_cond

log = logging.getLogger(__name__)

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
- ファイルの移動・改名・削除はこのサーバーではできない（利用者が Web の画面で行う）。
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
    ) -> dict[str, Any]:
        """アイテムを探す（Web の画面の検索と同じ条件）。root を省くと全ルートを探す。"""
        config = self._config()
        store = self._store(config)
        words = [w for w in (words or []) if w.strip()]
        include = [store.canonical(t) for t in (tags or []) if t.strip()]
        exclude = [store.canonical(t) for t in (exclude_tags or []) if t.strip()]
        conds = [_parse_attr_cond(a) for a in (attrs or []) if a.strip()]
        if kind and kind not in KINDS:
            raise ValueError(f"種類が不正です: {kind}（使えるもの: {', '.join(sorted(KINDS))}）")
        if not (words or text.strip() or kind or include or exclude or conds or under or after or before
                or min_size is not None or max_size is not None):
            raise ValueError("探す条件（words・text・kind・tags・exclude_tags・attrs・under・after・before・大きさ のどれか）を指定してください")
        limit = max(1, min(int(limit), MAX_RESULTS))
        under_rel = clean_relpath(under)
        terms = query_terms(text)
        roots = [self._root(config, root)] if root else list(config.roots)
        items: list[dict[str, Any]] = []
        total = 0
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
                        after=after, before=before, min_size=min_size, max_size=max_size,
                    )
                except ValueError as e:
                    raise ValueError(f"日付は YYYY-MM-DD で指定してください（{e}）") from None
                info = {r["relpath"]: r for r in rows}
                paths = list(info)
                if include or exclude:
                    paths = tags_search(
                        ((p, bool(info[p]["is_dir"])) for p in paths),
                        store.tagged_paths(rc.name, implied=True),
                        include, "or" if any_tag else "and", exclude,
                    )
                if conds:
                    defs = {d["name"]: d for d in store.attr_defs_list()}
                    values = store.attr_paths(rc.name)
                    paths = [p for p in paths if _attrs_match(p, conds, values, defs)]
                total += len(paths)
                # 返す件数の上限は、全ルートを通して数える
                shown = paths[: max(limit - len(items), 0)]
                snips = snippets(conn, [info[p]["sha256"] for p in shown if info[p]["sha256"]], terms) if terms else {}
                for p in shown:
                    r = info[p]
                    items.append({
                        "root": rc.name,
                        "path": p,
                        "is_dir": bool(r["is_dir"]),
                        "size": r["size"],
                        "modified": _iso(r["mtime_ns"]),
                        "tags": _flat_tags(store.tags_of(rc.name, p)),
                        "attrs": _flat_attrs(store.attrs_of(rc.name, p)),
                        **({"snippet": snips[r["sha256"]]} if r["sha256"] in snips else {}),
                    })
        finally:
            conn.close()
        return {"total": total, "returned": len(items), "items": items}

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
        """タグ（件数・別名・親）・属性の定義・保存した検索を返す。"""
        store = self._store(self._config())
        # 先に辞書を読み、続けて件数を読む（CLI の tags と同じ順）
        dictionary = store.dictionary()
        counts = store.all_tags()
        tags = []
        for tag in sorted(set(counts) | set(dictionary)):
            d = dictionary.get(tag, {})
            entry: dict[str, Any] = {"tag": tag, "count": counts.get(tag, 0)}
            if d.get("aliases"):
                entry["aliases"] = d["aliases"]
            if d.get("parents"):
                entry["parents"] = d["parents"]
            tags.append(entry)
        return {"tags": tags, "attributes": store.attr_defs_list(), "saved_searches": store.saved_searches()}

    # --- 書き込み（タグのログだけ。ファイルには触れない） ---

    def add_tags(self, root: str, paths: list[str], tags: list[str]) -> dict[str, Any]:
        """アイテムにタグを付ける（別名は正式な名前になる）。付けた件数をタグごとに返す。"""
        rc, rels, store = self._targets(root, paths)
        added = {}
        for tag in _nonempty(tags, "tags"):
            n = store.add(rc.name, rels, tag, source="agent")
            # 同じタグ（別名を含む）が重ねて渡されても、件数は足し上げる
            key = store.canonical(tag)
            added[key] = added.get(key, 0) + n
        log.info("エージェントがタグを付けました: %s %s", rc.name, added)
        return {"root": rc.name, "paths": rels, "added": added}

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

    def _targets(self, root: str, paths: list[str], *, must_exist: bool = True) -> tuple[RootConfig, list[str], TagStore]:
        config = self._config()
        rc = self._root(config, root)
        rels = [self._relpath(rc, p, must_exist=must_exist) for p in _nonempty(paths, "paths")]
        return rc, rels, self._store(config)


def _nonempty(values: list[str] | None, name: str) -> list[str]:
    out = [v for v in (values or []) if v.strip()]
    if not out:
        raise ValueError(f"{name} を1つ以上指定してください")
    return out


def _iso(mtime_ns: int | None) -> str | None:
    if mtime_ns is None:
        return None
    return datetime.fromtimestamp(mtime_ns / 1e9, timezone.utc).astimezone().isoformat(timespec="seconds")


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

    def guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
        """想定内のエラー（ルート・パス・タグの誤り）を、エージェントが読める ToolError にする。"""

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except (ConfigError, PathError, TagError, ValueError, OSError) as e:
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
        """
        return tools.search(root, words, text, kind, tags, exclude_tags, any_tag, attrs, under, after, before,
                            min_size, max_size, folders, limit)

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
        """既存のタグ（件数・別名・親）、属性の定義（型・選択肢）、保存した検索を一覧する。タグを付ける前に確かめる。"""
        return tools.list_tags()

    @server.tool(annotations=writes)
    @guarded
    def add_tags(root: str, paths: list[str], tags: list[str]) -> dict[str, Any]:
        """ファイル・フォルダにタグを付ける（ファイル自体は変えない）。別名は正式な名前になる。フォルダに付けると配下にも効く。

        Args:
            root: ルートの名前。
            paths: ルートからの相対パス（複数可）。
            tags: 付けるタグ（複数可。「分類:値」の形も使える）。
        """
        return tools.add_tags(root, paths, tags)

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

    return server


def run_stdio(config_path: Path | None, db_path: Path) -> None:
    """MCP サーバーを標準入出力で動かす（エージェントのクライアントが子プロセスとして起動する）。"""
    server = build_server(AgentTools(config_path, db_path))
    log.info("MCP サーバーを標準入出力で動かします（カタログ: %s）", db_path)
    server.run("stdio")
