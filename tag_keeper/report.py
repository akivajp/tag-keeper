"""整理候補の報告（衛生レポート）。

カタログの内容だけから作る。ファイルの中身は読まない。

- フォルダ単位の候補（アプリのデータ・作り直せる生成物）は、最も上のフォルダだけを1件として報告し、
  その配下はファイル単位の判定や重複の集計から外す。
- 拡張子の無いファイルの比率によるアプリのデータの判定は、名前で判定したフォルダを除いて数え直してから行い、
  条件を満たすフォルダのうち最も深いものだけを報告する
  （ゲームのデータを抱えた「ドキュメント」全体を候補にして、利用者の書類まで隠さないため）。
- 重複は、ハッシュを計算済みのファイルだけで判定する（推測では報告しない）。
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from tag_keeper import rules
from tag_keeper.config import HygieneConfig


@dataclass
class Finding:
    """整理候補の1件（ファイル、またはフォルダ全体）。"""

    category: str
    relpath: str
    is_dir: bool
    files: int
    size: int
    reason: str


@dataclass
class DuplicateGroup:
    """内容が同じファイルの組。"""

    sha256: str
    size: int
    paths: list[str]

    @property
    def waste(self) -> int:
        """1つを残して他を消した場合に空く容量。"""
        return self.size * (len(self.paths) - 1)


@dataclass
class Report:
    """1つのルートについての報告。"""

    root: str
    files: int
    dirs: int
    total_bytes: int
    hashed_files: int
    fanout: list[tuple[str, int]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    duplicates: list[DuplicateGroup] = field(default_factory=list)

    def by_category(self) -> dict[str, list[Finding]]:
        """候補をカテゴリごとに、表示順で返す。"""
        groups: dict[str, list[Finding]] = defaultdict(list)
        for f in self.findings:
            groups[f.category].append(f)
        return {c: groups[c] for c in rules.CATEGORY_ORDER if c in groups}

    def to_dict(self) -> dict[str, Any]:
        """JSON に書き出せる辞書にする。"""
        d = asdict(self)
        for g, gd in zip(self.duplicates, d["duplicates"]):
            gd["waste"] = g.waste
        return d


def parent_of(relpath: str) -> str:
    """親フォルダの相対パスを返す（ルート直下なら ""）。"""
    return relpath.rsplit("/", 1)[0] if "/" in relpath else ""


def ancestors(relpath: str) -> Iterator[str]:
    """祖先のフォルダを、ルート（""）から順に返す（自身は含まない）。"""
    yield ""
    parts = relpath.split("/")
    for i in range(1, len(parts)):
        yield "/".join(parts[:i])


def _matches_any(relpath: str, patterns: Iterable[str]) -> bool:
    """パス自身か祖先のどれかが、いずれかのパターンに一致するかを返す。"""
    pats = list(patterns)
    if not pats:
        return False
    candidates = [a for a in ancestors(relpath) if a] + [relpath]
    return any(fnmatchcase(c, p) for c in candidates for p in pats)


def _under(relpath: str, dirs: set[str]) -> bool:
    """relpath が dirs のいずれかの配下（または自身）にあるかを返す。"""
    if relpath in dirs:
        return True
    return any(a in dirs for a in ancestors(relpath) if a)


def build_report(
    conn: sqlite3.Connection,
    root_id: int,
    root_name: str,
    hygiene: HygieneConfig,
    *,
    top_fanout: int = 15,
) -> Report:
    """カタログからルートの報告を作る。

    Args:
        conn: カタログへの接続。
        root_id: ルートの ID。
        root_name: 表示用のルート名。
        hygiene: 検出ルールの設定（報告しないパス・端末名など）。
        top_fanout: 直下のファイル数が多いフォルダを何件まで載せるか。
    """
    rows = conn.execute(
        "SELECT relpath, is_dir, size, mtime_ns, sha256, hashed_size, hashed_mtime_ns"
        " FROM entries WHERE root_id = ? AND gone_at IS NULL",
        (root_id,),
    ).fetchall()
    files = [r for r in rows if not r["is_dir"]]
    dirs = sorted((r["relpath"] for r in rows if r["is_dir"]))
    allow = hygiene.allow
    report = Report(
        root=root_name,
        files=len(files),
        dirs=len(dirs),
        total_bytes=sum(r["size"] for r in files),
        hashed_files=sum(1 for r in files if _hash_is_current(r)),
    )

    # フォルダごとの集計（配下のファイル数・容量）と、空でないフォルダの集合
    desc_files: Counter[str] = Counter()
    desc_bytes: Counter[str] = Counter()
    non_empty: set[str] = set()
    for r in files:
        for a in ancestors(r["relpath"]):
            desc_files[a] += 1
            desc_bytes[a] += r["size"]
        non_empty.add(parent_of(r["relpath"]))
    for d in dirs:
        non_empty.add(parent_of(d))

    # 1. 名前で判定するフォルダ単位の候補（上から順に、最も上のものだけ）
    app_names = rules.DEFAULT_APP_DATA_NAMES | set(hygiene.app_data_names)
    flagged: set[str] = set()
    for d in dirs:
        if _under(d, flagged) or _matches_any(d, allow):
            continue
        name = d.rsplit("/", 1)[-1]
        if name in rules.REGENERABLE_DIRS:
            match = rules.Match(rules.CAT_REGENERABLE, f"{name} はビルドや実行で作り直せる")
        elif name in app_names:
            match = rules.Match(rules.CAT_APP_DATA, "既知のアプリのデータ")
        elif name.lower() in rules.CACHE_DIR_NAMES and desc_files[d] > 0:
            match = rules.Match(rules.CAT_APP_DATA, "キャッシュ")
        else:
            continue
        flagged.add(d)
        report.findings.append(
            Finding(match.category, d, True, desc_files[d], desc_bytes[d], match.reason)
        )

    # 2. 拡張子の無いファイルの比率による判定（名前で判定したフォルダの配下を除いて数え直す）
    rest_files: Counter[str] = Counter()
    rest_noext: Counter[str] = Counter()
    rest_bytes: Counter[str] = Counter()
    for r in files:
        if _under(r["relpath"], flagged):
            continue
        # Git の内部ファイル（.git/objects など）は拡張子が無いが、アプリのデータではない
        if rules.is_in_vcs_dir(r["relpath"]):
            continue
        noext = rules.has_no_extension(r["relpath"].rsplit("/", 1)[-1])
        for a in ancestors(r["relpath"]):
            rest_files[a] += 1
            rest_bytes[a] += r["size"]
            if noext:
                rest_noext[a] += 1
    qualifying = [
        d
        for d in dirs
        if rest_files[d] >= hygiene.app_data_min_files
        and rest_noext[d] * 2 >= rest_files[d]
        and not _matches_any(d, allow)
    ]
    # 条件を満たす子孫を持たない、最も深いものだけを残す
    deepest = [d for d in qualifying if not any(q.startswith(d + "/") for q in qualifying)]
    for d in deepest:
        flagged.add(d)
        report.findings.append(
            Finding(
                rules.CAT_APP_DATA,
                d,
                True,
                rest_files[d],
                rest_bytes[d],
                f"拡張子の無いファイルが {rest_files[d]:,} 件中 {rest_noext[d]:,} 件",
            )
        )

    # 3. ファイル単位の候補と重複（フォルダ単位の候補の配下と、報告しないパスは除く）
    by_hash: dict[str, list[sqlite3.Row]] = defaultdict(list)
    direct_files: Counter[str] = Counter()
    for r in files:
        rel = r["relpath"]
        if _under(rel, flagged) or _matches_any(rel, allow):
            continue
        direct_files[parent_of(rel)] += 1
        match = rules.classify_file(rel, r["size"], hygiene.device_names)
        if match is not None:
            report.findings.append(Finding(match.category, rel, False, 1, r["size"], match.reason))
        if r["size"] > 0 and _hash_is_current(r):
            by_hash[r["sha256"]].append(r)

    # 4. 空フォルダ
    for d in dirs:
        if d not in non_empty and not _under(d, flagged) and not _matches_any(d, allow):
            report.findings.append(Finding(rules.CAT_EMPTY_DIR, d, True, 0, 0, "中身が無い"))

    groups = [
        DuplicateGroup(sha, rs[0]["size"], sorted(r["relpath"] for r in rs))
        for sha, rs in by_hash.items()
        if len(rs) > 1
    ]
    report.duplicates = sorted(groups, key=lambda g: g.waste, reverse=True)
    report.fanout = direct_files.most_common(top_fanout)
    return report


def _hash_is_current(row: sqlite3.Row) -> bool:
    """ハッシュが計算済みで、計算後にファイルが変わっていないかを返す。"""
    return (
        row["sha256"] is not None
        and row["hashed_size"] == row["size"]
        and row["hashed_mtime_ns"] == row["mtime_ns"]
    )
