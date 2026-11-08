"""構造の見直しの提案（要件 F-OR-5）。カタログだけから作り、ファイルの中身は読まない。

見つけるもの（どれも人の判断が要るので、自動の操作にはしない）:

- fanout: 直下のファイルが多すぎるフォルダ
- similar: 意味が重なっていそうな兄弟のフォルダ（「請求書」と「請求書類」、「Photos」と「photo」など）
- numbering: 番号付きのフォルダの、番号の重複・欠番・桁数の不揃い
- dates: 日付で分けたフォルダの、名前の書式の混在（2025-01 と 202502 など）
- chain: 子が1つだけの入れ子の連鎖（a/b/c で a と b に中身が1つずつしかない）
- duplicate: 中身がまったく同じフォルダ（ハッシュを計算済みのファイルで判定）
- mixed: 中身の種類（書類・画像・動画・音声・圧縮ファイル）が混ざったフォルダ

保管・まとまり・アプリ管理・除外の型のフォルダの中、アプリのデータ・作り直せる生成物の中、隠しフォルダ、
「残す」と判断したものの中は見ない。
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from tag_keeper import dirtypes, fulltext

# 見直しの種類と表示名（画面とレポートの見出し）
KINDS: dict[str, str] = {
    "fanout": "直下のファイルが多すぎるフォルダ",
    "similar": "意味が重なっていそうな兄弟のフォルダ",
    "numbering": "番号付きのフォルダの番号の重複・欠番",
    "dates": "日付のフォルダの名前の書式の混在",
    "chain": "子が1つだけの入れ子の連鎖",
    "duplicate": "中身がまったく同じフォルダ",
    "mixed": "中身の種類が混ざったフォルダ",
}

# 混ざり方を見る種類（その他は数えない）
_MIX_KINDS = ("document", "image", "video", "audio", "archive")
_MIX_MIN_FILES = 30
_MIX_MIN_SHARE = 0.15
# 中身が同じとみなすフォルダの、ファイルの数の下限
_DUP_MIN_FILES = 2
# 日付の名前の書式（見分けるだけ。日付分割の推定とは別に、ここでは書式の違いを見る）
_DATE_FORMATS = [
    ("YYYY-MM-DD", re.compile(r"^(19|20)\d{2}-\d{2}-\d{2}")),
    ("YYYY_MM_DD", re.compile(r"^(19|20)\d{2}_\d{2}_\d{2}")),
    ("YYYYMMDD", re.compile(r"^(19|20)\d{6}(\D|$)")),
    ("YYYY-MM", re.compile(r"^(19|20)\d{2}-\d{2}(\D|$)")),
    ("YYYY_MM", re.compile(r"^(19|20)\d{2}_\d{2}(\D|$)")),
    ("YYYY.MM", re.compile(r"^(19|20)\d{2}\.\d{2}(\D|$)")),
    ("YYYYMM", re.compile(r"^(19|20)\d{4}(\D|$)")),
    ("YYYY年MM月", re.compile(r"^(19|20)\d{2}年\d{1,2}月")),
    ("YYYY年", re.compile(r"^(19|20)\d{2}年")),
    ("YYYY", re.compile(r"^(19|20)\d{2}(\D|$)")),
]
_NUMBER = re.compile(r"^(\d{1,3})[_ .\-)]")
# 似た名前の比較で落とす語尾（「類」「一覧」、英語の複数形）
_SUFFIXES = ("類", "一覧", "関係", "関連", "等", "など")


@dataclass
class Issue:
    """構造の見直しの提案1件。"""

    kind: str
    path: str  # 見直すフォルダ（"" はルート）
    detail: str
    paths: list[str] = field(default_factory=list)  # 関わるフォルダ（似た兄弟・同じ中身など）
    size: int = 0  # 関わるファイルの合計の大きさ（並べ替えの目安）

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "label": KINDS.get(self.kind, self.kind)}


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _skeleton(name: str) -> str:
    """似た名前の比較のための骨格（NFKC・小文字・数字と記号を除き、語尾と英語の複数形を落とす）。"""
    t = unicodedata.normalize("NFKC", name).casefold()
    t = re.sub(r"[\d\W_]+", "", t)
    for suf in _SUFFIXES:
        if t.endswith(suf) and len(t) > len(suf) + 1:
            t = t[: -len(suf)]
    if re.fullmatch(r"[a-z]+", t) and len(t) > 3:
        t = re.sub(r"(ies)$", "y", t)
        t = re.sub(r"(s)$", "", t)
    return t


def _date_format(name: str) -> str | None:
    return next((label for label, pat in _DATE_FORMATS if pat.match(name)), None)


def review(
    conn: sqlite3.Connection,
    root_id: int,
    *,
    types: dict[str, str] | None = None,
    skip: Iterable[str] = (),
    kept: Iterable[str] = (),
    fanout_max: int = 200,
    inbox_patterns: Iterable[str] = (),
    limit: int = 50,
) -> list[Issue]:
    """構造の見直しの提案を作る。

    Args:
        conn: カタログへの接続。
        root_id: ルートの ID。
        types: 付けたディレクトリの型（相対パス → 型）。
        skip: 見ないフォルダ（アプリのデータ・作り直せる生成物など。配下ごと）。
        kept: 「残す」と判断したパス（配下ごと見ない）。
        fanout_max: 直下のファイルがこれより多いフォルダを提案する。
        inbox_patterns: 受け皿とみなすフォルダ名（受け皿は直下のファイルが多くても当然なので、fanout から外す）。
        limit: 種類ごとの件数の上限（大きいものから）。
    """
    types = types or {}
    rows = conn.execute(
        "SELECT relpath, is_dir, size, sha256, hashed_size, hashed_mtime_ns, mtime_ns FROM entries WHERE root_id = ? AND gone_at IS NULL",
        (root_id,),
    ).fetchall()
    # 見ない部分: 型（保管・まとまり・アプリ管理・除外）、渡されたフォルダ、残すもの、隠しフォルダ
    closed = set(dirtypes.closed_dirs(conn, root_id, types)) | {p for p, t in types.items() if t == "archive"}
    closed |= {unicodedata.normalize("NFC", p) for p in skip} | {unicodedata.normalize("NFC", p) for p in kept}
    pats = [p.casefold() for p in inbox_patterns]

    def hidden(path: str) -> bool:
        if any(part.startswith(".") for part in path.split("/")):
            return True
        p = unicodedata.normalize("NFC", path)
        while True:
            if p in closed:
                return True
            if not p:
                return False
            p = _parent(p)

    dirs = sorted(r["relpath"] for r in rows if r["is_dir"] and not hidden(r["relpath"]))
    files = [r for r in rows if not r["is_dir"] and not hidden(r["relpath"])]
    child_dirs: dict[str, list[str]] = defaultdict(list)
    for d in dirs:
        child_dirs[_parent(d)].append(d)
    direct: dict[str, list[sqlite3.Row]] = defaultdict(list)
    subtree_size: Counter[str] = Counter()
    for r in files:
        direct[_parent(r["relpath"])].append(r)
        p = r["relpath"]
        while p:
            p = _parent(p)
            subtree_size[p] += r["size"]
    issues: dict[str, list[Issue]] = defaultdict(list)

    # fanout: 直下のファイルが多すぎる（受け皿は除く）
    for d, fs in direct.items():
        is_inbox = types.get(d) == "inbox" or (bool(d) and any(fnmatchcase(_name(d).casefold(), p) for p in pats))
        if len(fs) > fanout_max and not is_inbox:
            issues["fanout"].append(Issue("fanout", d, f"直下にファイルが {len(fs):,} 件", size=sum(r["size"] for r in fs)))

    for parent, kids in child_dirs.items():
        names = [_name(k) for k in kids]
        # similar: 骨格が同じ兄弟
        # 数字の部分も同じものだけを比べる（2021年_週報・2022年_週報のような、年や番号で分けたシリーズは別物）
        groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        for k in kids:
            sk = _skeleton(_name(k))
            digits = "".join(re.findall(r"\d+", unicodedata.normalize("NFKC", _name(k))))
            if len(sk) >= 2:
                groups[(sk, digits)].append(k)
        for sk, members in groups.items():
            if len(members) >= 2:
                issues["similar"].append(Issue(
                    "similar", parent, "名前が似ている: " + "、".join(_name(m) for m in members), members,
                    sum(subtree_size[m] for m in members),
                ))
        # numbering: 番号の重複・欠番・桁数の不揃い（番号付きの型か、子の過半数が番号付きのとき）
        numbered = [(int(m.group(1)), m.group(1), n) for n in names if (m := _NUMBER.match(n))]
        if numbered and (types.get(parent) == "numbered" or len(numbered) * 2 > len(names)) and len(numbered) >= 2:
            notes = []
            dup = [str(n) for n, c in Counter(n for n, _s, _x in numbered).items() if c > 1]
            if dup:
                notes.append("番号の重複: " + "、".join(sorted(dup, key=int)))
            nums = sorted({n for n, _s, _x in numbered})
            missing = [str(x) for x in range(nums[0], nums[-1] + 1) if x not in set(nums)]
            if missing and len(missing) <= 20:
                notes.append("欠番: " + "、".join(missing))
            widths = {len(s) for _n, s, _x in numbered}
            if len(widths) > 1:
                notes.append("桁数の不揃い: " + "・".join(f"{w} 桁" for w in sorted(widths)))
            if notes:
                issues["numbering"].append(Issue("numbering", parent, " ／ ".join(notes), [], subtree_size[parent]))
        # dates: 日付の名前の書式の混在（年だけのものは、年月のものと並んでもよいので数えない）
        formats = Counter(f for n in names if (f := _date_format(n)) and f not in ("YYYY", "YYYY年"))
        if len(formats) > 1 and sum(formats.values()) >= 2:
            issues["dates"].append(Issue(
                "dates", parent, "書式が混在: " + "・".join(f"{f}（{c}）" for f, c in formats.most_common()), [], subtree_size[parent]
            ))

    # chain: 中身が子のフォルダ1つだけのフォルダが2段以上続く（連鎖の一番上だけを出す）
    only_child = {d for d in dirs if not direct.get(d) and len(child_dirs.get(d, [])) == 1}
    for d in sorted(only_child):
        if _parent(d) in only_child:
            continue
        chain = [d]
        while chain[-1] in only_child:
            chain.append(child_dirs[chain[-1]][0])
        if len(chain) >= 3:
            issues["chain"].append(Issue("chain", d, "入れ子: " + " / ".join(_name(c) for c in chain), [chain[-1]], subtree_size[d]))

    # duplicate: 中身（相対パスと内容ハッシュの組）がまったく同じフォルダ
    sigs: dict[str, list[tuple[str, str]]] = defaultdict(list)
    unhashed: set[str] = set()
    for r in files:
        current = r["sha256"] is not None and (r["hashed_size"], r["hashed_mtime_ns"]) == (r["size"], r["mtime_ns"])
        p = r["relpath"]
        anc = _parent(p)
        while True:
            if not current:
                unhashed.add(anc)
            else:
                sigs[anc].append((p[len(anc) + 1 :] if anc else p, str(r["sha256"])))
            if not anc:
                break
            anc = _parent(anc)
    by_sig: dict[tuple[tuple[str, str], ...], list[str]] = defaultdict(list)
    for d, items in sigs.items():
        if d and d not in unhashed and len(items) >= _DUP_MIN_FILES:
            by_sig[tuple(sorted(items))].append(d)
    # 祖先と子孫の組（中身が同じに見えるのは入れ子だから）は除く
    tops: dict[tuple[tuple[str, str], ...], list[str]] = {}
    for sig, ds in by_sig.items():
        ds = sorted(ds)
        top = [d for d in ds if not any(d != o and (d.startswith(o + "/") or o.startswith(d + "/")) for o in ds)]
        if len(top) >= 2:
            tops[sig] = top
    in_group = {d for top in tops.values() for d in top}
    # 違いの出どころ（名前の後ろの共通部分を除いたもの）が同じ組は、1件にまとめる
    # （backup/App/bin と plugins/App/bin、backup/App/lib と plugins/App/lib … は、どれも backup と plugins の違い）
    merged: dict[tuple[str, ...], list[tuple[int, list[str]]]] = defaultdict(list)
    for sig, top in tops.items():
        # 親が同じ中身の組に入っているもの（A と B が同じなら A/x と B/x も同じ）は、親の組で説明がつくので出さない
        if all(_parent(d) in in_group for d in top):
            continue
        parts = [d.split("/") for d in top]
        common = 0
        while all(len(p) > common + 1 for p in parts) and len({p[-1 - common] for p in parts}) == 1:
            common += 1
        origins = tuple(sorted("/".join(p[: len(p) - common]) for p in parts))
        merged[origins].append((len(sig), top))
    for origins, groups in merged.items():
        groups.sort(key=lambda g: (-g[0], g[1]))
        n_files, top = groups[0]
        more = f"（ほかに同じ出どころの組が {len(groups) - 1:,} 組）" if len(groups) > 1 else ""
        issues["duplicate"].append(Issue(
            "duplicate", top[0], f"中身がまったく同じ（{n_files:,} 件）: " + "、".join(top) + more, top,
            subtree_size[top[0]] * (len(top) - 1),
        ))

    # mixed: 中身の種類が混ざったフォルダ
    for d, fs in direct.items():
        if len(fs) < _MIX_MIN_FILES:
            continue
        kinds = Counter(next((k for k in _MIX_KINDS if fulltext.ext_of(r["relpath"]) in fulltext.KINDS[k]), "other") for r in fs)
        major = [k for k in _MIX_KINDS if kinds[k] / len(fs) >= _MIX_MIN_SHARE]
        if len(major) >= 3:
            labels = {"document": "書類", "image": "画像", "video": "動画", "audio": "音声", "archive": "圧縮ファイル"}
            issues["mixed"].append(Issue(
                "mixed", d, "混ざっている: " + "・".join(f"{labels[k]} {kinds[k]}" for k in major), [], sum(r["size"] for r in fs)
            ))

    out: list[Issue] = []
    for kind in KINDS:
        out += sorted(issues.get(kind, []), key=lambda i: (-i.size, i.path))[:limit]
    return out
