"""カタログ（SQLite）の作成と接続。

カタログは、走査で観測したファイルの一覧（パス・サイズ・更新日時・inode・ハッシュ）を持つ。
走査結果から作り直せる派生物として扱い、管理対象のファイルには一切書き込まない。

消えたファイルの行は削除せず、`gone_at` に消えた日時を記録して残す
（「過去にあったファイル」の検索や、移動の追跡に使うため）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2

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
    gone_at         TEXT                -- 消えたと確定した日時。存在中は NULL
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

-- 抽出したテキスト（版 2 から）。内容ハッシュをキーにし、移動・改名・重複で抽出し直さない（F-EX-5）
CREATE TABLE IF NOT EXISTS extracts (
    sha256     TEXT PRIMARY KEY,
    method     TEXT NOT NULL,       -- pdftotext / office / text / image / none
    text       TEXT NOT NULL,
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


def connect(db_path: Path) -> sqlite3.Connection:
    """カタログに接続する。無ければ作成する。

    Args:
        db_path: SQLite ファイルのパス。親フォルダが無ければ作る。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    # 版 1 からは表を足すだけで移行できる
    if version not in (0, 1, SCHEMA_VERSION):
        conn.close()
        raise CatalogVersionError(
            f"カタログの版 {version} はこのプログラム（版 {SCHEMA_VERSION}）では扱えません: {db_path}"
        )
    conn.executescript(SCHEMA)
    if version != SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
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
