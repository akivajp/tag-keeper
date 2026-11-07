"""カタログ（SQLite）の作成と接続。

カタログは、走査で観測したファイルの一覧（パス・サイズ・更新日時・inode・ハッシュ）を持つ。
走査結果から作り直せる派生物として扱い、管理対象のファイルには一切書き込まない。

消えたファイルの行は削除せず、`gone_at` に消えた日時を記録して残す
（「過去にあったファイル」の検索や、移動の追跡に使うため）。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS roots (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    id              INTEGER PRIMARY KEY,
    root_id         INTEGER NOT NULL REFERENCES roots(id),
    relpath         TEXT NOT NULL,      -- ルートからの相対パス（区切りは /）
    is_dir          INTEGER NOT NULL,
    size            INTEGER NOT NULL,   -- フォルダは 0
    mtime_ns        INTEGER NOT NULL,
    inode           INTEGER NOT NULL,
    sha256          TEXT,               -- 未計算なら NULL
    hashed_size     INTEGER,            -- ハッシュを計算した時点のサイズと更新日時
    hashed_mtime_ns INTEGER,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    missing_count   INTEGER NOT NULL DEFAULT 0,  -- 続けて見えなかった走査の回数
    gone_at         TEXT,               -- 消えたと確定した日時。存在中は NULL
    gone_by         TEXT                -- 消えた理由（版 3 から）。scan（走査で見えなくなった） / plan（ツールが隔離した）。古い行は NULL
);
-- 存在中のパスは一意（消えた行は同じパスで何度でも残せる）
CREATE UNIQUE INDEX IF NOT EXISTS entries_live_path
    ON entries(root_id, relpath) WHERE gone_at IS NULL;
CREATE INDEX IF NOT EXISTS entries_live_inode
    ON entries(root_id, inode) WHERE gone_at IS NULL;
CREATE INDEX IF NOT EXISTS entries_sha256 ON entries(sha256);

CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY,
    root_id     INTEGER NOT NULL REFERENCES roots(id),
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    seen        INTEGER NOT NULL,
    added       INTEGER NOT NULL,
    changed     INTEGER NOT NULL,
    moved       INTEGER NOT NULL,
    gone        INTEGER NOT NULL,
    held        INTEGER NOT NULL,   -- 大量消失のため確定を保留した件数
    unreadable  INTEGER NOT NULL,   -- 読めなかったフォルダの数
    status      TEXT NOT NULL       -- committed / held
);

-- 移動・改名の履歴（版 2 から）。移動の前の版をたどるのと、タグを移動に追従させるのに使う。
-- フォルダごと動いたときは、そのフォルダの1行だけを記録する（配下は相対パスから求められる）
CREATE TABLE IF NOT EXISTS moves (
    id          INTEGER PRIMARY KEY,
    root_id     INTEGER NOT NULL REFERENCES roots(id),
    entry_id    INTEGER NOT NULL,
    old_relpath TEXT NOT NULL,
    new_relpath TEXT NOT NULL,
    is_dir      INTEGER NOT NULL,
    moved_at    TEXT NOT NULL,
    source      TEXT NOT NULL       -- scan（ツールの外での移動を検出） / plan（ツールが移した）
);
CREATE INDEX IF NOT EXISTS moves_new ON moves(root_id, new_relpath);

-- 抽出したテキスト（版 3 で作り直した）。内容ハッシュをキーにし、移動・改名・重複で抽出し直さない（F-EX-5）。
-- 全文検索の索引（extracts_fts）が id で行を指すので、id は INTEGER PRIMARY KEY にする（VACUUM で変わらないように）
CREATE TABLE IF NOT EXISTS extracts (
    id         INTEGER PRIMARY KEY,
    sha256     TEXT NOT NULL UNIQUE,
    method     TEXT NOT NULL,       -- pdftotext / office / text / ocr / none（文字が無い・読まない種類） / scan（OCR 待ち） / error
    text       TEXT NOT NULL,       -- 表記をそろえた（NFKC）本文。無ければ空
    note       TEXT NOT NULL DEFAULT '',  -- 読めなかった理由など
    created_at TEXT NOT NULL
);

-- 整理の提案（版 2 から）。内容ハッシュ・モデル・指示文の版ごとに保存し、同じ条件では作り直さない
CREATE TABLE IF NOT EXISTS suggestions (
    sha256         TEXT NOT NULL,
    model          TEXT NOT NULL,
    prompt_version INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    data           TEXT NOT NULL,   -- JSON
    PRIMARY KEY (sha256, model, prompt_version)
);
"""


def utcnow() -> str:
    """現在時刻を UTC の ISO 8601 文字列（秒まで）で返す。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CatalogVersionError(RuntimeError):
    """カタログのスキーマの版が、このプログラムと合わないときの例外。"""


# 全文検索の索引（FTS5 の trigram。日本語を単語に区切らずに、3文字組で探す）。
# 本文は extracts に1つだけ持ち、索引はそれを id で指す（外部コンテンツ）。extracts の変更はトリガーで索引に写す
FTS_SCHEMA = (
    "CREATE VIRTUAL TABLE extracts_fts USING fts5(text, content='extracts', content_rowid='id', tokenize='trigram')",
    (
        "CREATE TRIGGER extracts_fts_ai AFTER INSERT ON extracts BEGIN"
        " INSERT INTO extracts_fts(rowid, text) VALUES (new.id, new.text); END"
    ),
    (
        "CREATE TRIGGER extracts_fts_ad AFTER DELETE ON extracts BEGIN"
        " INSERT INTO extracts_fts(extracts_fts, rowid, text) VALUES ('delete', old.id, old.text); END"
    ),
    (
        "CREATE TRIGGER extracts_fts_au AFTER UPDATE ON extracts BEGIN"
        " INSERT INTO extracts_fts(extracts_fts, rowid, text) VALUES ('delete', old.id, old.text);"
        " INSERT INTO extracts_fts(rowid, text) VALUES (new.id, new.text); END"
    ),
)
FTS_TRIGGERS = ("extracts_fts_ai", "extracts_fts_ad", "extracts_fts_au")
# この SQLite では全文検索の索引を作れなかったカタログ（接続のたびに作り直しを試みて、警告を出し続けないように）
_FTS_UNAVAILABLE: set[str] = set()


def _statements(script: str) -> list[str]:
    """SCHEMA を文ごとに分ける（トランザクションの中で1文ずつ実行するため。SCHEMA の文の中に ; は無い）。"""
    return [s.strip() for s in script.split(";") if s.strip() and not all(
        line.strip().startswith("--") or not line.strip() for line in s.strip().splitlines()
    )]


def _migrate(conn: sqlite3.Connection, version: int) -> None:
    """古い版のカタログを今の版にする（BEGIN IMMEDIATE の中で呼ぶ）。

    - 版 1 → 2: 表を足すだけ（SCHEMA の CREATE ... IF NOT EXISTS で足りる）
    - 版 2 → 3: entries に gone_by を足し、extracts を id 付きで作り直す（版 2 までは書き込む処理が無く、ふつうは空）
    """
    if version in (1, 2):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(entries)")}
        if "gone_by" not in cols:
            conn.execute("ALTER TABLE entries ADD COLUMN gone_by TEXT")
    old_extracts = {r[1] for r in conn.execute("PRAGMA table_info(extracts)")}
    if old_extracts and "id" not in old_extracts:
        conn.execute("ALTER TABLE extracts RENAME TO extracts_v2")
    for stmt in _statements(SCHEMA):
        conn.execute(stmt)
    if old_extracts and "id" not in old_extracts:
        conn.execute(
            "INSERT INTO extracts(sha256, method, text, note, created_at)"
            " SELECT sha256, method, text, '', created_at FROM extracts_v2"
        )
        conn.execute("DROP TABLE extracts_v2")
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def fts_enabled(conn: sqlite3.Connection) -> bool:
    """全文検索の索引（extracts_fts）があり、使えるか。"""
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'extracts_fts'").fetchone()
    return row is not None and "trigger" in {
        r[0] for r in conn.execute("SELECT type FROM sqlite_master WHERE name = 'extracts_fts_ai'")
    }


def _ensure_fts(conn: sqlite3.Connection, key: str = "") -> None:
    """全文検索の索引を用意する。この SQLite が FTS5 の trigram に対応していなければ、索引なしで動かす。

    索引を作ったカタログを FTS5 の無い SQLite で開いたときは、トリガーだけを外す
    （extracts への書き込みが「no such module」で失敗しないように。全文検索は索引なしで行う）。
    """
    if key in _FTS_UNAVAILABLE:
        return
    has_table = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'extracts_fts'").fetchone() is not None
    if has_table:
        try:
            conn.execute("SELECT rowid FROM extracts_fts LIMIT 0").fetchall()
        except sqlite3.OperationalError as e:
            log.warning("全文検索の索引を使えません（この SQLite は FTS5 に対応していません）。索引なしで探します: %s", e)
            for name in FTS_TRIGGERS:
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            conn.commit()
            _FTS_UNAVAILABLE.add(key)
            return
        if fts_enabled(conn):
            return
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 別の接続が先に作ったかもしれないので、ロックを取ってから確かめ直す
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'extracts_fts'").fetchone() is None:
            for stmt in FTS_SCHEMA:
                conn.execute(stmt)
            # 既にある本文を索引に入れる
            conn.execute("INSERT INTO extracts_fts(extracts_fts) VALUES ('rebuild')")
        elif not fts_enabled(conn):
            # 索引の表はあるがトリガーが無い（FTS5 の無い環境で外した）。トリガーを戻して索引を作り直す
            for stmt in FTS_SCHEMA[1:]:
                conn.execute(stmt)
            conn.execute("INSERT INTO extracts_fts(extracts_fts) VALUES ('rebuild')")
        conn.commit()
    except sqlite3.OperationalError as e:
        conn.rollback()
        _FTS_UNAVAILABLE.add(key)
        log.warning("全文検索の索引（FTS5 の trigram）を作れません。全文検索は索引なしで行います: %s", e)


def connect(db_path: Path) -> sqlite3.Connection:
    """カタログに接続する。無ければ作成し、古い版なら今の版にする。

    Args:
        db_path: SQLite ファイルのパス。親フォルダが無ければ作る。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # 裏の処理（本文の抽出など）と書き込みが重なっても、すぐには失敗しないように待つ
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1, 2, SCHEMA_VERSION):
        conn.close()
        raise CatalogVersionError(
            f"カタログの版 {version} はこのプログラム（版 {SCHEMA_VERSION}）では扱えません: {db_path}"
        )
    if version != SCHEMA_VERSION:
        # 複数の接続が同時に移行しないよう、書き込みのロックを取ってから版を読み直す
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                _migrate(conn, version)
            conn.commit()
        except BaseException:
            conn.rollback()
            conn.close()
            raise
    _ensure_fts(conn, str(db_path))
    return conn


def ensure_root(conn: sqlite3.Connection, name: str, path: Path) -> int:
    """ルートを登録し、その ID を返す。登録済みでパスが変わっていれば更新する。

    パスが変わってもルートからの相対パスはそのまま使えるので、
    マシンの移行（要件 N-10）ではパスの更新だけで済む。
    """
    row = conn.execute("SELECT id, path FROM roots WHERE name = ?", (name,)).fetchone()
    if row is not None:
        if row["path"] != str(path):
            conn.execute("UPDATE roots SET path = ? WHERE id = ?", (str(path), row["id"]))
            conn.commit()
        return int(row["id"])
    cur = conn.execute("INSERT INTO roots(name, path) VALUES (?, ?)", (name, str(path)))
    conn.commit()
    return int(cur.lastrowid)


def record_move(
    conn: sqlite3.Connection,
    root_id: int,
    entry_id: int,
    old_relpath: str,
    new_relpath: str,
    is_dir: bool,
    source: str,
    moved_at: str | None = None,
) -> None:
    """移動・改名を1件記録する（コミットは呼び出し側で行う）。"""
    conn.execute(
        "INSERT INTO moves(root_id, entry_id, old_relpath, new_relpath, is_dir, moved_at, source)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (root_id, entry_id, old_relpath, new_relpath, int(is_dir), moved_at or utcnow(), source),
    )


def last_scan(conn: sqlite3.Connection, root_id: int) -> sqlite3.Row | None:
    """そのルートの直近の走査結果を返す。走査したことが無ければ None。"""
    return conn.execute(
        "SELECT * FROM scans WHERE root_id = ? ORDER BY id DESC LIMIT 1", (root_id,)
    ).fetchone()
