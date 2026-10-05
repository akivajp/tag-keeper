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

SCHEMA_VERSION = 1

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
    if version not in (0, SCHEMA_VERSION):
        conn.close()
        raise CatalogVersionError(
            f"カタログの版 {version} はこのプログラム（版 {SCHEMA_VERSION}）では扱えません: {db_path}"
        )
    conn.executescript(SCHEMA)
    if version == 0:
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


def last_scan(conn: sqlite3.Connection, root_id: int) -> sqlite3.Row | None:
    """そのルートの直近の走査結果を返す。走査したことが無ければ None。"""
    return conn.execute(
        "SELECT * FROM scans WHERE root_id = ? ORDER BY id DESC LIMIT 1", (root_id,)
    ).fetchone()
