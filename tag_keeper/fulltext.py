"""全文検索（要件 F-SE-1・F-SE-2・F-EX-5・F-EX-6）。

- 本文は内容ハッシュをキーに、カタログの extracts 表へ保存する（移動・改名・重複のときに抽出し直さない）
- 索引は SQLite の FTS5（trigram）。日本語を単語に区切らず、3文字組で探す。2文字以下の語は、索引を使わずに本文を探す
- 抽出するのは、ハッシュを計算済みのファイル。書類（PDF・Office）を優先し、新しいものから順に行う。
  Web の画面を動かしている間は、裏のスレッド（ExtractWorker）が少しずつ進める。ジョブの枠は使わない
  （書き込むのは extracts 表だけで、ツリーにもカタログの一覧にも触れないため）
- 作り直せる生成物（node_modules など）・バージョン管理の中身・アプリのデータの中は、索引に入れない
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tag_keeper import extract, media, rules
from tag_keeper.catalog import connect, fts_enabled, utcnow
from tag_keeper.config import Config, ExtractConfig

log = logging.getLogger(__name__)

# 索引に入れないフォルダ（中のテキストは検索の邪魔になり、索引を大きくするだけなので）
_SKIP_DIRS = rules.REGENERABLE_DIRS | rules.VCS_DIRS

# 検索の「種類」で絞り込むときの拡張子
KINDS: dict[str, frozenset[str]] = {
    "pdf": frozenset({".pdf"}),
    "office": frozenset(
        {".docx", ".docm", ".doc", ".xlsx", ".xlsm", ".xls", ".pptx", ".pptm", ".ppt", ".odt", ".ods", ".odp", ".rtf"}
    ),
    "text": extract.TEXT_EXTS,
    "image": frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".svg"}),
    "video": media.NATIVE_VIDEO | media.CONVERT_VIDEO,
    "audio": frozenset({".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}),
    "archive": frozenset({".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".lzh"}),
}
# 書類 = PDF・Office・テキスト
KINDS["document"] = KINDS["pdf"] | KINDS["office"] | KINDS["text"]

# trigram の索引で探せる語の長さの下限（これより短い語は、索引を使わずに本文を探す）
TRIGRAM = 3


def ext_of(relpath: str) -> str:
    """パスの拡張子（小文字）。隠しファイルの先頭の . は拡張子とみなさない。"""
    return rules.split_ext(relpath.rsplit("/", 1)[-1])[1].lower()


def _skipped(relpath: str, app_data: frozenset[str]) -> bool:
    """索引に入れないフォルダの中にあるか。"""
    return any(p in _SKIP_DIRS or p in app_data for p in relpath.split("/")[:-1])


def _priority(ext: str) -> int:
    """抽出の順番（小さいほど先）: 書類 → テキスト → 画像。"""
    if ext == ".pdf" or ext in extract.INDEX_OFFICE_PARTS:
        return 0
    if ext in extract.TEXT_EXTS:
        return 1
    return 2


def _like_escape(s: str) -> str:
    """LIKE の特殊文字（% と _）を、ESCAPE '\\' で文字どおりに扱えるようにする。"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass(frozen=True)
class Pending:
    """本文をまだ抽出していないファイル（同じ内容のものは1件にまとめる）。"""

    sha256: str
    relpath: str
    size: int
    mtime_ns: int


def pending(
    conn: sqlite3.Connection,
    root_id: int,
    cfg: ExtractConfig,
    *,
    ocr_ready: bool,
    app_data: Iterable[str] = (),
    limit: int | None = None,
    skip: Iterable[str] = (),
) -> list[Pending]:
    """本文をまだ抽出していないファイル。書類を優先し、新しいものから順に返す。

    ハッシュが今の中身のもの（計算した後に変わっていないもの）だけを対象にする。
    OCR を使えるなら、以前に OCR を使えずに後回しにしたもの（method = "scan"）も含める。

    Args:
        conn: カタログへの接続。
        root_id: ルートの ID。
        cfg: 抽出の設定。
        ocr_ready: OCR を使えるか。
        app_data: アプリのデータとみなすフォルダ名（中は索引に入れない）。
        limit: 返す件数の上限。
        skip: 飛ばす内容ハッシュ（この実行では読めなかったものなど）。
    """
    exts = sorted(extract.index_exts(cfg.ocr and cfg.ocr_images))
    # 拡張子での絞り込みは SQL で行う（索引に入れない種類のファイルが多くても、毎回 Python で全件を見ないように）
    like = " OR ".join("e.relpath LIKE ?" for _ in exts)
    rows = conn.execute(
        "SELECT e.sha256, e.relpath, e.size, e.mtime_ns FROM entries e"
        " LEFT JOIN extracts x ON x.sha256 = e.sha256"
        " WHERE e.root_id = ? AND e.gone_at IS NULL AND e.is_dir = 0 AND e.sha256 IS NOT NULL"
        " AND e.hashed_size IS e.size AND e.hashed_mtime_ns IS e.mtime_ns"
        f" AND (x.id IS NULL OR (x.method = 'scan' AND ?)) AND ({like})",
        (root_id, int(ocr_ready), *(f"%{x}" for x in exts)),
    )
    app = frozenset(app_data)
    skipped = set(skip)
    want = set(exts)
    best: dict[str, Pending] = {}
    for r in rows:
        sha, rel = str(r["sha256"]), str(r["relpath"])
        if sha in skipped or ext_of(rel) not in want or _skipped(rel, app):
            continue
        cur = best.get(sha)
        if cur is None or r["mtime_ns"] > cur.mtime_ns:
            best[sha] = Pending(sha, rel, int(r["size"]), int(r["mtime_ns"]))
    items = sorted(best.values(), key=lambda p: (_priority(ext_of(p.relpath)), -p.mtime_ns, p.relpath))
    return items[:limit] if limit else items


def store(conn: sqlite3.Connection, sha256: str, ex: extract.Extracted) -> None:
    """抽出の結果を保存する（同じ内容ハッシュのものは置き換える。索引はトリガーで更新される）。"""
    with conn:
        conn.execute(
            "INSERT INTO extracts(sha256, method, text, note, created_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(sha256) DO UPDATE SET method = excluded.method, text = excluded.text,"
            " note = excluded.note, created_at = excluded.created_at",
            (sha256, ex.method, ex.text, ex.note, utcnow()),
        )


def extract_item(root: Path, item: Pending, cfg: ExtractConfig, ocr_lang: str | None) -> extract.Extracted | None:
    """1件の本文を取り出す。読む前後でファイルが変わっていれば None（ハッシュを計算し直してから読む）。"""
    path = root / item.relpath
    try:
        before = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if (before.st_size, before.st_mtime_ns) != (item.size, item.mtime_ns):
        return None
    ex = extract.extract_text(
        path,
        max_chars=cfg.max_chars,
        max_mb=cfg.max_mb,
        ocr_lang=ocr_lang,
        ocr_pages=cfg.ocr_pages,
        ocr_images=cfg.ocr_images,
    )
    try:
        after = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        return None
    return ex


def app_data_names(config: Config) -> frozenset[str]:
    """アプリのデータとみなすフォルダ名（既定と、設定で足したもの）。"""
    return rules.DEFAULT_APP_DATA_NAMES | frozenset(config.hygiene.app_data_names)


@dataclass
class ExtractResult:
    """まとめて抽出した結果の件数。"""

    done: int = 0
    ocr: int = 0
    errors: int = 0
    skipped: int = 0  # 読んでいる間に変わった・道具が無いなど、今回は保存しなかったもの


def run_pending(
    conn: sqlite3.Connection,
    root_id: int,
    root: Path,
    config: Config,
    *,
    limit: int | None = None,
    on_start: Callable[[int], None] | None = None,
    on_item: Callable[[Pending, extract.Extracted | None], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> ExtractResult:
    """抽出していないファイルの本文を、まとめて抽出して保存する（CLI の extract）。"""
    cfg = config.extract
    ocr_lang = extract.ocr_languages(cfg.ocr_lang) if cfg.ocr else None
    items = pending(conn, root_id, cfg, ocr_ready=ocr_lang is not None, app_data=app_data_names(config), limit=limit)
    if on_start is not None:
        on_start(len(items))
    res = ExtractResult()
    for it in items:
        if should_stop is not None and should_stop():
            break
        ex = extract_item(root, it, cfg, ocr_lang)
        if ex is None or ex.method == "unavailable":
            res.skipped += 1
        else:
            store(conn, it.sha256, ex)
            res.done += 1
            res.ocr += ex.method == "ocr"
            res.errors += ex.method == "error"
        if on_item is not None:
            on_item(it, ex)
    return res


def index_status(conn: sqlite3.Connection, root_id: int, config: Config) -> dict[str, Any]:
    """索引の状況（画面の表示用）: 抽出済みの数・待っている数・OCR を使えるか・索引があるか。"""
    cfg = config.extract
    ocr_lang = extract.ocr_languages(cfg.ocr_lang) if cfg.ocr else None
    waiting = len(pending(conn, root_id, cfg, ocr_ready=ocr_lang is not None, app_data=app_data_names(config)))
    (indexed,) = conn.execute(
        "SELECT COUNT(DISTINCT x.sha256) FROM extracts x JOIN entries e ON e.sha256 = x.sha256"
        " WHERE e.root_id = ? AND e.gone_at IS NULL AND x.method IN ('pdftotext', 'office', 'text', 'ocr', 'transcript', 'describe')",
        (root_id,),
    ).fetchone()
    (scans,) = conn.execute(
        "SELECT COUNT(DISTINCT x.sha256) FROM extracts x JOIN entries e ON e.sha256 = x.sha256"
        " WHERE e.root_id = ? AND e.gone_at IS NULL AND x.method = 'scan'",
        (root_id,),
    ).fetchone()
    return {
        "enabled": cfg.enabled,
        "indexed": indexed,
        "pending": waiting,
        "waiting_ocr": 0 if ocr_lang else scans,
        "ocr": ocr_lang,
        "ocr_enabled": cfg.ocr,
        "fts": fts_enabled(conn),
    }


# ---------- 探す ----------


def query_terms(text: str) -> list[str]:
    """検索語を、索引の本文と同じ表記（NFKC）にそろえて、空白で区切る。"""
    return [w for w in unicodedata.normalize("NFKC", text).split() if w]


def text_match_sql(conn: sqlite3.Connection, terms: list[str]) -> tuple[str, list[Any]]:
    """本文に terms をすべて含む内容ハッシュを返す副問い合わせ（SQL と引数）。

    3文字以上の語は FTS5 の索引（trigram）で、それより短い語（「税金」など）は本文の LIKE で探す。
    索引が無いときは、すべて LIKE で探す（遅いが、結果は同じ）。大文字・小文字は区別しない。
    """
    conds: list[str] = []
    args: list[Any] = []
    long_terms = [w for w in terms if len(w) >= TRIGRAM]
    short_terms = [w for w in terms if len(w) < TRIGRAM]
    if long_terms and fts_enabled(conn):
        conds.append("x.id IN (SELECT rowid FROM extracts_fts WHERE extracts_fts MATCH ?)")
        args.append(" ".join('"' + w.replace('"', '""') + '"' for w in long_terms))
    else:
        short_terms = terms
    for w in short_terms:
        conds.append("x.text LIKE ? ESCAPE '\\'")
        args.append(f"%{_like_escape(w)}%")
    return "SELECT x.sha256 FROM extracts x WHERE " + " AND ".join(conds or ["0"]), args


def _day_start_ns(day: str) -> int:
    """YYYY-MM-DD の日の始まり（この端末の時刻の 0 時）を、ナノ秒の UNIX 時刻にする。不正なら ValueError。"""
    import datetime as dt

    return int(dt.datetime.combine(dt.date.fromisoformat(day.strip()), dt.time()).timestamp()) * 1_000_000_000


def find_entries(
    conn: sqlite3.Connection,
    root_id: int,
    *,
    words: Iterable[str] = (),
    text: str = "",
    kind: str = "",
    files_only: bool = True,
    under: str = "",
    after: str = "",
    before: str = "",
    min_size: int | None = None,
    max_size: int | None = None,
    gone: bool = False,
) -> list[sqlite3.Row]:
    """名前・パス・本文・種類・場所・日付・大きさで、アイテムを探す（タグと属性での絞り込みは呼び出し側で行う）。

    Args:
        conn: カタログへの接続。
        root_id: ルートの ID。
        words: パス（名前とフォルダ）に含む語。すべて含むものを返す。大文字・小文字は区別しない。
        text: 本文に含む語（空白区切りで、すべて含むもの）。
        kind: 種類（KINDS のキー）。指定するとファイルだけを返す。
        files_only: フォルダを除くか。
        under: このフォルダの配下だけ（ルートからの相対パス。"" はすべて）。
        after: この日（YYYY-MM-DD、この端末の時刻）以降に更新されたものだけ。
        before: この日（を含む）までに更新されたものだけ。
        min_size: これ以上の大きさ（バイト）のファイルだけ。
        max_size: これ以下の大きさのファイルだけ。
        gone: 今は無いもの（消えた・動いた前のパス）も含める（F-VS-4）。今あるパスと同じものは除き、同じパスは最後の記録だけ。

    Returns:
        relpath・is_dir・size・mtime_ns・sha256・gone_at を持つ行（パスの順）。
    """
    sql = "SELECT relpath, is_dir, size, mtime_ns, sha256, gone_at FROM entries WHERE root_id = ?"
    args: list[Any] = [root_id]
    if not gone:
        sql += " AND gone_at IS NULL"
    if files_only or kind or text.strip() or min_size is not None or max_size is not None:
        sql += " AND is_dir = 0"
    for w in words:
        # パスの表記は NFC のまま比べる（ファイル名を NFKC にすると、元の名前と食い違うため）
        sql += " AND relpath LIKE ? ESCAPE '\\'"
        args.append(f"%{_like_escape(unicodedata.normalize('NFC', w))}%")
    if under:
        # LIKE は % や _ を含むフォルダ名で誤るので、先頭の部分文字列で比べる
        sql += " AND (relpath = ? OR substr(relpath, 1, ?) = ?)"
        args.extend([under, len(under) + 1, under + "/"])
    if after:
        sql += " AND mtime_ns >= ?"
        args.append(_day_start_ns(after))
    if before:
        import datetime as dt

        next_day = (dt.date.fromisoformat(before.strip()) + dt.timedelta(days=1)).isoformat()
        sql += " AND mtime_ns < ?"
        args.append(_day_start_ns(next_day))
    if min_size is not None:
        sql += " AND size >= ?"
        args.append(min_size)
    if max_size is not None:
        sql += " AND size <= ?"
        args.append(max_size)
    terms = query_terms(text)
    if terms:
        sub, sub_args = text_match_sql(conn, terms)
        sql += f" AND sha256 IN ({sub})"
        args.extend(sub_args)
    rows = conn.execute(sql + " ORDER BY relpath, gone_at", args).fetchall()
    if kind:
        exts = KINDS.get(kind, frozenset())
        rows = [r for r in rows if ext_of(r["relpath"]) in exts]
    if gone:
        # 今あるパスはその行だけ。今は無いパスは、最後に消えた記録だけを残す
        live = {r["relpath"] for r in rows if r["gone_at"] is None}
        latest: dict[str, sqlite3.Row] = {}
        for r in rows:
            if r["gone_at"] is None or r["relpath"] in live:
                continue
            cur = latest.get(r["relpath"])
            if cur is None or str(r["gone_at"]) > str(cur["gone_at"]):
                latest[r["relpath"]] = r
        rows = [r for r in rows if r["gone_at"] is None] + list(latest.values())
        rows.sort(key=lambda r: r["relpath"])
    return rows


def snippets(conn: sqlite3.Connection, shas: Iterable[str], terms: list[str], width: int = 120) -> dict[str, str]:
    """本文の中で、最初の検索語の前後を切り出す（結果の一覧に、どこで一致したかを見せるため）。"""
    shas = list(dict.fromkeys(shas))
    if not shas or not terms:
        return {}
    term = terms[0].lower()
    out: dict[str, str] = {}
    for i in range(0, len(shas), 500):
        chunk = shas[i : i + 500]
        marks = ",".join("?" * len(chunk))
        # 本文の全体を Python に持ってこないよう、切り出しは SQL で行う（lower は ASCII だけを小文字にする）
        for r in conn.execute(
            "SELECT sha256, instr(lower(text), ?) AS pos, substr(text, max(1, instr(lower(text), ?) - 40), ?) AS snip,"
            f" length(text) AS n FROM extracts WHERE sha256 IN ({marks})",
            (term, term, width, *chunk),
        ):
            if not r["pos"]:
                continue
            s = " ".join(str(r["snip"]).split())
            start = max(1, int(r["pos"]) - 40)
            out[str(r["sha256"])] = ("…" if start > 1 else "") + s + ("…" if start + width <= int(r["n"]) else "")
    return out


# ---------- 裏で抽出し続ける ----------


class ExtractWorker:
    """Web の画面を動かしている間、裏で本文を抽出し続ける（[extract] enabled）。

    抽出するものが無くなったら idle 秒待つ。ルートを最新にした後などは poke() で起こす。
    この実行の中で読めなかったもの（道具が無い・読んでいる間に変わった）は、同じ内容ハッシュでは試し直さない。
    """

    def __init__(
        self,
        db_path: Path,
        config: Callable[[], Config],
        *,
        idle: float = 300.0,
        batch: int = 20,
        first: float = 30.0,
    ) -> None:
        """
        Args:
            db_path: カタログのパス。
            config: 今の設定を返す関数（設定の変更を再起動なしで反映するため、毎回読む）。
            idle: 抽出するものが無いときに待つ秒数。
            batch: 1回にまとめて選ぶ件数。
            first: 起動してから最初に始めるまでの秒数。
        """
        self.db_path = db_path
        self.config = config
        self.idle = idle
        self.batch = batch
        self.first = first
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._skip: set[str] = set()
        self._thread: threading.Thread | None = None
        self.current: str | None = None
        self.done = 0

    def start(self) -> None:
        """裏のスレッドを始める。"""
        self._thread = threading.Thread(target=self._loop, name="extract", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """裏のスレッドを止める（今の1件を読み終えたところで止まる）。"""
        self._stop.set()
        self._wake.set()

    def poke(self) -> None:
        """待っている間でも、すぐに抽出するものを探し直させる。"""
        self._wake.set()

    def _loop(self) -> None:
        if self._stop.wait(self.first):
            return
        while not self._stop.is_set():
            worked = False
            try:
                worked = self.run_once()
            except Exception as e:  # noqa: BLE001 - 一時的な失敗（カタログのロックなど）で止めない
                log.warning("本文の抽出に失敗しました: %s", e)
            if not worked:
                self._wake.wait(self.idle)
                self._wake.clear()

    def run_once(self) -> bool:
        """抽出していないものを1まとまり抽出する。何か抽出したら True。"""
        config = self.config()
        cfg = config.extract
        if not cfg.enabled:
            return False
        ocr_lang = extract.ocr_languages(cfg.ocr_lang) if cfg.ocr else None
        conn = connect(self.db_path)
        try:
            for rc in config.roots:
                row = conn.execute("SELECT id FROM roots WHERE name = ?", (rc.name,)).fetchone()
                if row is None:
                    continue
                items = pending(
                    conn, int(row["id"]), cfg, ocr_ready=ocr_lang is not None,
                    app_data=app_data_names(config), limit=self.batch, skip=self._skip,
                )
                for it in items:
                    if self._stop.is_set():
                        return True
                    self.current = f"{rc.name}/{it.relpath}"
                    ex = extract_item(rc.path, it, cfg, ocr_lang)
                    if ex is None or ex.method == "unavailable":
                        self._skip.add(it.sha256)
                        continue
                    store(conn, it.sha256, ex)
                    self.done += 1
                if items:
                    return True
            return False
        finally:
            self.current = None
            conn.close()
