"""テスト共通の部品。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tag_keeper.catalog import connect, ensure_root


def write(path: Path, content: str | bytes = "x") -> Path:
    """親フォルダを作ってからファイルを書く。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """データと設定の既定の置き場所を一時フォルダに向ける（利用者の本物のタグのログなどに書き込まないように）。"""
    base = tmp_path_factory.mktemp("xdg")
    monkeypatch.setenv("XDG_DATA_HOME", str(base / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / "config"))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """管理対象のルート（空のフォルダ）。"""
    r = tmp_path / "root"
    r.mkdir()
    return r


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """テスト用のカタログ。"""
    c = connect(tmp_path / "catalog.db")
    yield c
    c.close()


@pytest.fixture
def root_id(conn: sqlite3.Connection, root: Path) -> int:
    """登録済みのルートの ID。"""
    return ensure_root(conn, "test", root)


def live_paths(conn: sqlite3.Connection, root_id: int, *, files_only: bool = True) -> set[str]:
    """カタログ上で存在中のパスの集合。"""
    sql = "SELECT relpath FROM entries WHERE root_id = ? AND gone_at IS NULL"
    if files_only:
        sql += " AND is_dir = 0"
    return {r["relpath"] for r in conn.execute(sql, (root_id,))}
