"""整理候補の報告（衛生レポート）。

カタログの内容だけから作る。ファイルの中身は読まない。

- フォルダ単位の候補（アプリのデータ・作り直せる生成物）は、最も上のフォルダだけを1件として報告し、
  その配下はファイル単位の判定や重複の集計から外す。
- 拡張子の無いファイルの比率によるアプリのデータの判定は、名前で判定したフォルダを除いて数え直してから行い、
  条件を満たすフォルダのうち最も深いものだけを報告する
  （ゲームのデータを抱えた「ドキュメント」全体を候補にして、利用者の書類まで隠さないため）。
- 重複は、ハッシュを計算済みのファイルだけで判定する（推測では報告しない）。
- 競合コピー・複製は、同じフォルダに元のファイルがあるものだけを報告する。
  元が無ければ、それが今の唯一の版（または意図して名前を付けた控え）なので、置く価値が薄いとは言えない。
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
    # 人が中身を確かめてから判断すべきもの（元のファイルと内容が異なる競合コピーなど）。
    # 整理プランには既定で含めない
    needs_review: bool = False


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
    kept: Iterable[str] = (),
    old_versions: dict[str, str] | None = None,
    now_ns: int | None = None,
) -> Report:
    """カタログからルートの報告を作る。

    Args:
        conn: カタログへの接続。
        root_id: ルートの ID。
        root_name: 表示用のルート名。
        hygiene: 検出ルールの設定（報告しないパス・端末名など）。
        top_fanout: 直下のファイル数が多いフォルダを何件まで載せるか。
        kept: 利用者が「残す」と判断したパス（配下ごと報告しない。F-OR-2）。
        old_versions: 「前の版」の関係で後継版があるとされた旧版（旧版 → 後継版）。
        now_ns: 今の時刻（ナノ秒。「長く触れていない」の判定に使う。テスト用）。
    """
    import time
    import unicodedata

    disabled = {rules.CATEGORY_KEYS.get(k, k) for k in hygiene.disabled}

    def enabled(cat: str) -> bool:
        return cat not in disabled
    rows = conn.execute(
        "SELECT relpath, is_dir, size, mtime_ns, sha256, hashed_size, hashed_mtime_ns"
        " FROM entries WHERE root_id = ? AND gone_at IS NULL",
        (root_id,),
    ).fetchall()
    files = [r for r in rows if not r["is_dir"]]
    dirs = sorted((r["relpath"] for r in rows if r["is_dir"]))
    allow = hygiene.allow
    # 「残す」と判断したもの（パスは NFC で記録しているので、カタログのパスもそろえて比べる）
    kept_set = {unicodedata.normalize("NFC", p) for p in kept}

    def is_kept(relpath: str) -> bool:
        if not kept_set:
            return False
        p = unicodedata.normalize("NFC", relpath)
        return p in kept_set or any(a in kept_set for a in ancestors(p) if a)

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
        if _under(d, flagged) or _matches_any(d, allow) or is_kept(d):
            continue
        name = d.rsplit("/", 1)[-1]
        if name in rules.REGENERABLE_DIRS and enabled(rules.CAT_REGENERABLE):
            match = rules.Match(rules.CAT_REGENERABLE, f"{name} はビルドや実行で作り直せる")
        elif name in app_names and enabled(rules.CAT_APP_DATA):
            match = rules.Match(rules.CAT_APP_DATA, "既知のアプリのデータ")
        elif name.lower() in rules.CACHE_DIR_NAMES and desc_files[d] > 0 and enabled(rules.CAT_APP_DATA):
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
        and not is_kept(d)
        and enabled(rules.CAT_APP_DATA)
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
    by_path = {r["relpath"]: r for r in files}
    by_hash: dict[str, list[sqlite3.Row]] = defaultdict(list)
    direct_files: Counter[str] = Counter()
    for r in files:
        rel = r["relpath"]
        if _under(rel, flagged) or _matches_any(rel, allow) or is_kept(rel):
            continue
        direct_files[parent_of(rel)] += 1
        match = rules.classify_file(rel, r["size"], hygiene.device_names)
        if match is not None and not enabled(match.category):
            match = None
        needs_review = False
        if match is not None and match.category == rules.CAT_CONFLICT:
            match, needs_review = _check_conflict_original(r, by_path, match, hygiene.device_names)
        if match is not None:
            report.findings.append(
                Finding(match.category, rel, False, 1, r["size"], match.reason, needs_review)
            )
        if r["size"] > 0 and _hash_is_current(r):
            by_hash[r["sha256"]].append(r)

    # 4. 空フォルダ
    for d in dirs:
        if d not in non_empty and not _under(d, flagged) and not _matches_any(d, allow) and not is_kept(d) and enabled(rules.CAT_EMPTY_DIR):
            report.findings.append(Finding(rules.CAT_EMPTY_DIR, d, True, 0, 0, "中身が無い"))

    reported = {f.relpath for f in report.findings}

    def open_for(rel: str) -> bool:
        """ほかのカテゴリでまだ報告しておらず、除外・残すの対象でもないか。"""
        return rel not in reported and not _under(rel, flagged) and not _matches_any(rel, allow) and not is_kept(rel)

    # 5. 展開済みの圧縮ファイル（同じフォルダに、拡張子を除いた名前のフォルダがあるもの。中身が全部そろっているかは人が確かめる）
    dir_set = set(dirs)
    if enabled(rules.CAT_EXTRACTED):
        for r in files:
            rel = r["relpath"]
            low = rel.lower()
            ext = next((e for e in rules.ARCHIVE_EXTS if low.endswith(e)), None)
            if ext is None or not open_for(rel):
                continue
            folder = rel[: -len(ext)]
            if folder in dir_set and desc_files[folder] > 0:
                report.findings.append(Finding(
                    rules.CAT_EXTRACTED, rel, False, 1, r["size"],
                    f"展開したフォルダ（{folder.rsplit('/', 1)[-1]}/、{desc_files[folder]:,} 件）が隣にある。中身がそろっているか確認が必要", True,
                ))
                reported.add(rel)

    # 6. 後継版がある旧版（「前の版」の関係を張ったもの。後継版が今もあるときだけ）
    live = {r["relpath"]: r for r in rows}
    if enabled(rules.CAT_OLD_VERSION):
        nfc_live = {unicodedata.normalize("NFC", p): p for p in live}
        for old, newer in sorted((old_versions or {}).items()):
            o, n = nfc_live.get(old), nfc_live.get(newer)
            if o is None or n is None or not open_for(o):
                continue
            row = live[o]
            files_n = desc_files[o] if row["is_dir"] else 1
            size_n = desc_bytes[o] if row["is_dir"] else row["size"]
            report.findings.append(Finding(rules.CAT_OLD_VERSION, o, bool(row["is_dir"]), files_n, size_n, f"後継版: {n}"))
            reported.add(o)

    # 7. 大きくて長く触れていないもの（消すのではなく、保管先の見直しの候補。プランには既定で含めない）
    if enabled(rules.CAT_STALE_LARGE):
        now = now_ns if now_ns is not None else time.time_ns()
        cutoff = now - hygiene.stale_days * 86400 * 1_000_000_000
        limit = hygiene.stale_min_mb * 1024 * 1024
        for r in files:
            rel = r["relpath"]
            if r["size"] >= limit and r["mtime_ns"] < cutoff and open_for(rel):
                days = (now - r["mtime_ns"]) // (86400 * 1_000_000_000)
                report.findings.append(Finding(
                    rules.CAT_STALE_LARGE, rel, False, 1, r["size"], f"{days:,} 日触れていない。保管先の見直しの候補", True,
                ))

    groups = [
        DuplicateGroup(sha, rs[0]["size"], sorted(r["relpath"] for r in rs))
        for sha, rs in by_hash.items()
        if len(rs) > 1
    ]
    report.duplicates = sorted(groups, key=lambda g: g.waste, reverse=True)
    report.fanout = direct_files.most_common(top_fanout)
    return report


def _check_conflict_original(
    row: sqlite3.Row,
    by_path: dict[str, sqlite3.Row],
    match: rules.Match,
    device_names: Iterable[str],
) -> tuple[rules.Match | None, bool]:
    """競合コピー・複製の元のファイルを探し、理由に内容の比較結果を足す。

    Returns:
        （理由を足した一致結果, 人の確認が要るか）。元のファイルが無ければ（None, False）。
        内容が同じと確かめられたものだけを、確認不要とする。
    """
    rel = row["relpath"]
    original = rules.conflict_original(rel.rsplit("/", 1)[-1], device_names)
    if original is None:
        return None, False
    parent = parent_of(rel)
    orig = by_path.get(f"{parent}/{original}" if parent else original)
    if orig is None:
        return None, False
    if _hash_is_current(row) and _hash_is_current(orig) and row["sha256"] == orig["sha256"]:
        return rules.Match(match.category, f"{match.reason}。元のファイルと内容が同じ"), False
    if _hash_is_current(row) and _hash_is_current(orig):
        note = "元のファイルと内容が異なる。中身の確認が必要"
    else:
        note = "元のファイルがある（内容は未比較）"
    return rules.Match(match.category, f"{match.reason}。{note}"), True


def _hash_is_current(row: sqlite3.Row) -> bool:
    """ハッシュが計算済みで、計算後にファイルが変わっていないかを返す。"""
    return (
        row["sha256"] is not None
        and row["hashed_size"] == row["size"]
        and row["hashed_mtime_ns"] == row["mtime_ns"]
    )


def user_report(conn: sqlite3.Connection, root_id: int, root_name: str, config: Any, store: Any, *, top_fanout: int = 15) -> Report:
    """利用者の判断（「残す」・「前の版」の関係）を反映した報告（画面・CLI・プランの作成で使う）。

    Args:
        config: tag-keeper 全体の設定（Config）。
        store: タグのログ（TagStore）。
    """
    return build_report(
        conn, root_id, root_name, config.hygiene, top_fanout=top_fanout,
        kept=store.kept_of(root_name), old_versions=store.old_versions(root_name),
    )


def structure_issues(conn: sqlite3.Connection, root_id: int, root_name: str, config: Any, store: Any, report: Report) -> list[Any]:
    """構造の見直しの提案（F-OR-5）。報告でアプリのデータ・作り直せる生成物としたフォルダの中は見ない。"""
    from tag_keeper import structure

    skip = [f.relpath for f in report.findings if f.is_dir and f.category in (rules.CAT_APP_DATA, rules.CAT_REGENERABLE)]
    return structure.review(
        conn, root_id, types=store.dir_types_of(root_name), skip=skip, kept=store.kept_of(root_name),
        fanout_max=config.hygiene.fanout_max, inbox_patterns=config.organize.inbox_patterns,
    )
