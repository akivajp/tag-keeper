"""ディレクトリのローカルな意味（要件 F-DR-1〜3）と、走査から外すフォルダ（F-SC-6・入れ子のルート）。

- 型は利用者が付けたもの（タグのログの dir-type）を優先し、付けていなければ中身から推定する。推定は記録しない
- 付けた型は配下に継承する（まとまり・アプリ管理・受け皿・保管・除外）。日付分割・番号付きは、そのフォルダの子の並び方を
  表す型なので継承しない。「通常」を付けると、上から継承した型をそこで止められる（子で上書き）
- 型ごとの振る舞い:
  - 除外: 走査しない
  - まとまり・アプリ管理: 中のフォルダを移動先の候補にせず、中のファイルを受け皿として扱わない
  - 受け皿: 名前に関係なく、整理の画面の対象にする
  - 保管・日付分割・番号付き: 構造の見直し（report.structure）で使う
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase

from tag_keeper.config import Config, RootConfig

# 型の名前（画面の表示名）。並びは画面の選択肢の順
DIR_TYPES: dict[str, str] = {
    "normal": "通常",
    "bundle": "まとまり",
    "app": "アプリ管理",
    "dated": "日付分割",
    "numbered": "番号付き",
    "inbox": "受け皿",
    "archive": "保管",
    "exclude": "除外",
}
# 配下に継承する型
INHERITED = frozenset({"bundle", "app", "inbox", "archive", "exclude"})
# 中を移動先の候補・受け皿にしない型
CLOSED = frozenset({"bundle", "app", "exclude"})

# 推定の手がかり: このフォルダかファイルが直下にあれば、その型とみなす
_BUNDLE_MARKERS = {".git": "Git のリポジトリ", ".svn": "Subversion の作業コピー", ".hg": "Mercurial のリポジトリ"}
_APP_MARKERS = {".obsidian": "Obsidian の Vault", "zotero.sqlite": "Zotero のライブラリ", "metadata.db": "Calibre のライブラリ"}
_APP_SUFFIXES = {".lrcat": "Lightroom のカタログ"}
# 日付らしい名前（2026・2026-10・202610・2026_10・2026年・2026年10月・2026-10-08 など）
_DATED = re.compile(r"^(19|20)\d{2}([-_.]?(0[1-9]|1[0-2])([-_.]?\d{2})?|年(\d{1,2}月)?)?(\D.*)?$")
# 番号付きの名前（01_xxx・1. xxx・001-xxx）
_NUMBERED = re.compile(r"^(\d{1,3})[_ .\-)]")
# 日付分割・番号付きとみなす、子のフォルダの割合と数の下限
_MIN_SHARE = 0.6
_MIN_CHILDREN = 2


@dataclass(frozen=True)
class DirType:
    """フォルダの型。how は set（付けた）・inherited（祖先から継承）・inferred（推定）・none（通常）。"""

    type: str
    how: str
    source: str = ""  # 継承したときは元のフォルダ、推定したときは理由

    def to_dict(self) -> dict[str, str]:
        return {"type": self.type, "label": DIR_TYPES.get(self.type, self.type), "how": self.how, "source": self.source}


def _ancestors(path: str) -> list[str]:
    """近い順の祖先（自身を含まない。"" はルート）。"""
    if not path:
        return []
    parts = path.split("/")
    return ["/".join(parts[:i]) for i in range(len(parts) - 1, 0, -1)] + [""]


def explicit_type(path: str, types: dict[str, str]) -> DirType | None:
    """付けた型（自身か、継承する型を付けた最も近い祖先）。無ければ None。「通常」の祖先は継承を止める。"""
    if path in types:
        return DirType(types[path], "set")
    for anc in _ancestors(path):
        t = types.get(anc)
        if t is None or t in ("dated", "numbered"):
            continue
        if t == "normal":
            return None
        if t in INHERITED:
            return DirType(t, "inherited", anc)
    return None


def _children(conn: sqlite3.Connection, root_id: int, path: str) -> tuple[list[str], list[str]]:
    """フォルダの直下の（フォルダ名, ファイル名）。範囲の比較で索引を使う。"""
    if path:
        lo, hi = path + "/", path + "0"  # "/" の次の文字は "0"
        rows = conn.execute(
            "SELECT relpath, is_dir FROM entries WHERE root_id = ? AND gone_at IS NULL AND relpath >= ? AND relpath < ?",
            (root_id, lo, hi),
        )
        start = len(lo)
    else:
        rows = conn.execute("SELECT relpath, is_dir FROM entries WHERE root_id = ? AND gone_at IS NULL AND instr(relpath, '/') = 0", (root_id,))
        start = 0
    dirs: list[str] = []
    files: list[str] = []
    for r in rows:
        name = str(r["relpath"])[start:]
        if "/" in name:
            continue
        (dirs if r["is_dir"] else files).append(name)
    return dirs, files


def infer_type(conn: sqlite3.Connection, root_id: int, path: str, inbox_patterns: Iterable[str] = ()) -> DirType:
    """中身からフォルダの型を推定する（記録はしない）。当てはまらなければ通常。"""
    name = path.rsplit("/", 1)[-1]
    if path and any(fnmatchcase(name.casefold(), p.casefold()) for p in inbox_patterns):
        return DirType("inbox", "inferred", "受け皿らしい名前")
    dirs, files = _children(conn, root_id, path)
    for marker, why in _BUNDLE_MARKERS.items():
        if marker in dirs:
            return DirType("bundle", "inferred", why)
    for marker, why in _APP_MARKERS.items():
        if marker in dirs or marker in files:
            return DirType("app", "inferred", why)
    for f in files:
        for suffix, why in _APP_SUFFIXES.items():
            if f.lower().endswith(suffix):
                return DirType("app", "inferred", why)
    visible = [d for d in dirs if not d.startswith(".")]
    if len(visible) >= _MIN_CHILDREN:
        dated = sum(1 for d in visible if _DATED.match(d))
        if dated >= _MIN_CHILDREN and dated / len(visible) >= _MIN_SHARE:
            return DirType("dated", "inferred", f"子のフォルダの {dated}/{len(visible)} が日付の名前")
        numbered = sum(1 for d in visible if _NUMBERED.match(d))
        if numbered >= _MIN_CHILDREN and numbered / len(visible) >= _MIN_SHARE:
            return DirType("numbered", "inferred", f"子のフォルダの {numbered}/{len(visible)} が番号付き")
    return DirType("normal", "none")


def effective_type(
    conn: sqlite3.Connection, root_id: int, path: str, types: dict[str, str], inbox_patterns: Iterable[str] = ()
) -> DirType:
    """フォルダの型（付けた型 → 継承した型 → 推定の順）。"""
    return explicit_type(path, types) or infer_type(conn, root_id, path, inbox_patterns)


def closed_dirs(conn: sqlite3.Connection, root_id: int, types: dict[str, str]) -> list[str]:
    """中を移動先の候補・受け皿にしないフォルダ（まとまり・アプリ管理・除外。付けたものと、目印から推定したもの）。

    「通常」を付けたフォルダは、目印があっても推定しない（利用者の判断を優先する）。
    """
    out = {p for p, t in types.items() if t in CLOSED}
    markers = list(_BUNDLE_MARKERS) + list(_APP_MARKERS)
    # 名前（パスの最後）が目印のもの。目印の名前に LIKE の特殊文字（% と _）は無い
    conds = " OR ".join("relpath = ? OR relpath LIKE ?" for _ in markers)
    args = [x for m in markers for x in (m, f"%/{m}")]
    for r in conn.execute(f"SELECT relpath FROM entries WHERE root_id = ? AND gone_at IS NULL AND ({conds})", (root_id, *args)):
        rel = str(r["relpath"])
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if parent and types.get(parent) not in ("normal",) and parent not in out:
            out.add(parent)
    # 入れ子（まとまりの中のまとまり）は、外側だけにする
    ordered = sorted(out, key=lambda p: (p.count("/"), p))
    top: list[str] = []
    for p in ordered:
        if not any(p == t or p.startswith(t + "/") for t in top):
            top.append(p)
    return top


def inbox_dirs(types: dict[str, str]) -> list[str]:
    """受け皿の型を付けたフォルダ。"""
    return sorted(p for p, t in types.items() if t == "inbox")


def scan_exclusions(config: Config, rc: RootConfig, types: dict[str, str]) -> dict[str, str]:
    """走査しないフォルダ（ルートからの相対パス → 理由）。

    - 「除外」の型を付けたフォルダ
    - 複製として登録したフォルダ（[[roots]] の replicas。F-SC-6）
    - このルートの中にある、別のルート（入れ子のルート。それぞれのルートとして管理するので、二重に数えない）
    """
    out: dict[str, str] = {}
    for p, t in types.items():
        if t == "exclude" and p:
            out[p] = "除外の型"
    for p, original in rc.replicas.items():
        out[p.strip("/")] = f"{original} の複製"
    base = rc.path.resolve()
    for other in config.roots:
        if other.name == rc.name:
            continue
        try:
            rel = other.path.resolve().relative_to(base).as_posix()
        except ValueError:
            continue
        if rel not in ("", "."):
            out[rel] = f"ルート {other.name}"
    return out
