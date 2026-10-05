"""ファイルの内容ハッシュ（SHA-256）の計算。

走査では中身を読まないため、ハッシュは別の手順で計算する。
計算済みのものは、サイズと更新日時が変わっていない限り計算し直さない。
計算の途中でファイルが変わった場合は、結果を保存しない（次回に計算し直す）。
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# まとめて保存する単位（件数）。途中で止めても、ここまでの計算結果は残る
COMMIT_EVERY = 200


@dataclass
class HashResult:
    """ハッシュ計算の結果の件数。"""

    hashed: int = 0
    hashed_bytes: int = 0
    skipped_changed: int = 0  # 計算前後でサイズや更新日時が変わったもの
    skipped_error: int = 0  # 読めなかったもの


def pending(conn: sqlite3.Connection, root_id: int) -> list[sqlite3.Row]:
    """ハッシュの計算が必要なファイル（未計算か、計算後に変わったもの）を返す。"""
    return conn.execute(
        "SELECT id, relpath, size, mtime_ns FROM entries"
        " WHERE root_id = ? AND gone_at IS NULL AND is_dir = 0"
        " AND (sha256 IS NULL OR hashed_size IS NOT size OR hashed_mtime_ns IS NOT mtime_ns)"
        " ORDER BY relpath",
        (root_id,),
    ).fetchall()


def sha256_file(path: Path, on_chunk: Callable[[int], None] | None = None) -> str:
    """ファイルの SHA-256 を16進文字列で返す。

    Args:
        path: 対象のファイル。
        on_chunk: 読み込んだバイト数を、読むたびに渡して呼ぶ（進捗表示用）。
    """
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
            if on_chunk is not None:
                on_chunk(len(chunk))
    return h.hexdigest()


def hash_root(
    conn: sqlite3.Connection,
    root_id: int,
    root: Path,
    *,
    on_start: Callable[[int, int], None] | None = None,
    on_bytes: Callable[[int], None] | None = None,
) -> HashResult:
    """ルートのファイルのうち、ハッシュが必要なものを計算してカタログに保存する。

    Args:
        conn: カタログへの接続。
        root_id: ルートの ID。
        root: ルートの絶対パス。
        on_start: 計算を始める前に（件数, 合計バイト数）を渡して呼ぶ。
        on_bytes: 読み込んだバイト数を渡して呼ぶ。
    """
    rows = pending(conn, root_id)
    if on_start is not None:
        on_start(len(rows), sum(r["size"] for r in rows))
    res = HashResult()
    batch: list[tuple] = []
    for row in rows:
        path = root / row["relpath"]
        try:
            before = os.stat(path, follow_symlinks=False)
            if (before.st_size, before.st_mtime_ns) != (row["size"], row["mtime_ns"]):
                # 走査の後に変わった。次の走査で属性を更新してから計算する
                res.skipped_changed += 1
                if on_bytes is not None:
                    on_bytes(row["size"])
                continue
            digest = sha256_file(path, on_bytes)
            after = os.stat(path, follow_symlinks=False)
        except OSError as e:
            log.warning("読めませんでした: %s (%s)", path, e.strerror)
            res.skipped_error += 1
            if on_bytes is not None:
                on_bytes(row["size"])
            continue
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            # 計算の途中で書き換わった
            res.skipped_changed += 1
            continue
        batch.append((digest, row["size"], row["mtime_ns"], row["id"]))
        res.hashed += 1
        res.hashed_bytes += row["size"]
        if len(batch) >= COMMIT_EVERY:
            _save(conn, batch)
            batch.clear()
    _save(conn, batch)
    return res


def _save(conn: sqlite3.Connection, batch: list[tuple]) -> None:
    """計算したハッシュをまとめて保存する。"""
    if not batch:
        return
    with conn:
        conn.executemany(
            "UPDATE entries SET sha256 = ?, hashed_size = ?, hashed_mtime_ns = ? WHERE id = ?",
            batch,
        )
