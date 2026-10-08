"""クラウドの Web 画面で開くリンク（今は OneDrive 個人用）。

Linux 版 onedrive クライアントは、同期しているアイテムの OneDrive 上の ID と親子関係を
手元のデータベース（~/.config/onedrive/items.sqlite3）に持っている。これをたどって、
同期フォルダの中のパスから ID を引き、OneDrive の Web 画面や、ブラウザ版の Office で開くリンクを作る。

- クライアントが動いている間はデータベースがロックされているので、一時フォルダにコピーしてから読む（元には触れない）
- データベースのファイルが変わったときだけ読み直す
- 将来は Google Drive や pCloud なども、同じ形（リンクの一覧）で足せるようにしておく
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

log = logging.getLogger(__name__)

# ブラウザ版の Office で開ける種類
OFFICE_WEB_EXTS = frozenset({".docx", ".doc", ".xlsx", ".xls", ".xlsm", ".pptx", ".ppt", ".odt", ".ods", ".odp"})


@dataclass(frozen=True)
class Item:
    """OneDrive のアイテム1件。"""

    drive_id: str
    id: str
    type: str  # root / dir / file / remote


def _norm(name: str) -> str:
    """名前の比較用（Unicode の NFC。OneDrive は大文字・小文字を区別しない）。"""
    return unicodedata.normalize("NFC", name).casefold()


class OneDriveLinks:
    """onedrive クライアントのデータベースから、パスに当たる OneDrive のアイテムを引く。"""

    def __init__(self, config_dir: Path, sync_dir: Path) -> None:
        self.db = config_dir / "items.sqlite3"
        self.sync_dir = sync_dir
        self.lock = threading.Lock()
        self._stamp: tuple[float, int, float, int] | None = None
        self._children: dict[tuple[str, str], Item] = {}
        self._root: Item | None = None

    def _stamp_now(self) -> tuple[float, int, float, int]:
        st = self.db.stat()
        wal = self.db.with_name(self.db.name + "-wal")
        wst = wal.stat() if wal.exists() else None
        return (st.st_mtime, st.st_size, wst.st_mtime if wst else 0.0, wst.st_size if wst else 0)

    def _load(self) -> None:
        """データベースを一時フォルダにコピーして読み、（親 ID, 名前）→ アイテムの索引を作る。"""
        stamp = self._stamp_now()
        if stamp == self._stamp:
            return
        children: dict[tuple[str, str], Item] = {}
        root: Item | None = None
        with tempfile.TemporaryDirectory() as tmp:
            for suffix in ("", "-wal", "-shm"):
                src = self.db.with_name(self.db.name + suffix)
                if src.exists():
                    shutil.copyfile(src, Path(tmp) / f"items.sqlite3{suffix}")
            conn = sqlite3.connect(Path(tmp) / "items.sqlite3")
            try:
                for drive_id, item_id, name, kind, parent in conn.execute("SELECT driveId, id, name, type, parentId FROM item"):
                    item = Item(str(drive_id), str(item_id), str(kind))
                    if kind == "root" and parent is None and root is None:
                        root = item
                    elif parent is not None:
                        children[(str(parent), _norm(str(name)))] = item
            finally:
                conn.close()
        self._children, self._root, self._stamp = children, root, stamp
        log.info("onedrive クライアントのデータベースを読みました: %s 件", f"{len(children):,}")

    def find(self, path: Path) -> Item | None:
        """同期フォルダの中のパスに当たるアイテム。見つからなければ None。"""
        try:
            rel = path.parent.resolve().joinpath(path.name).relative_to(self.sync_dir.resolve())
        except ValueError:
            return None
        with self.lock:
            try:
                self._load()
            except (OSError, sqlite3.Error) as e:
                log.warning("onedrive クライアントのデータベースを読めません: %s", e)
                return None
            item = self._root
            for part in rel.parts:
                if item is None:
                    return None
                item = self._children.get((item.id, _norm(part)))
            return item

    def links(self, path: Path, is_dir: bool) -> list[dict[str, Any]]:
        """OneDrive の Web 画面で開くリンク（Office 文書なら、ブラウザ版の Office で開くリンクも）。"""
        item = self.find(path)
        if item is None or item.type == "root":
            return []
        cid = quote(item.drive_id, safe="")
        resid = quote(item.id, safe="!")
        out = [{"kind": "onedrive", "label": "OneDrive で開く", "url": f"https://onedrive.live.com/?cid={cid}&id={resid}"}]
        if not is_dir and path.suffix.lower() in OFFICE_WEB_EXTS:
            out.append({"kind": "office", "label": "ブラウザ版の Office で開く", "url": f"https://onedrive.live.com/edit.aspx?cid={cid}&resid={resid}"})
        return out
