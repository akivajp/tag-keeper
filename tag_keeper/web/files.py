"""ファイルブラウザの部品（パスの安全な解決・フォルダの一覧・ファイルの配信の種類の判定）。

画面からはルート名とルートからの相対パスだけを受け取り、ルートの外（.. やシンボリックリンクの先）には出ない。
ファイルを画面の中で開く（inline）のは、スクリプトを実行しない種類（PDF・画像・音声・動画・テキスト）だけにする。
HTML や SVG を同じオリジンで開くと、中のスクリプトが API を呼べてしまうため、それ以外はダウンロードにする。
"""

from __future__ import annotations

import datetime as dt
import mimetypes
import os
from pathlib import Path
from typing import Any


class PathError(ValueError):
    """ルートの外を指す・不正なパスのときの例外。"""


def clean_relpath(rel: str) -> str:
    """画面から受け取った相対パスを確かめて整える（"" はルート自身）。"""
    rel = rel.strip("/")
    if rel == "":
        return ""
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts) or "\x00" in rel:
        raise PathError(f"不正なパスです: {rel!r}")
    return rel


def resolve_within(base: Path, rel: str) -> Path:
    """base/rel の実体が base の中にあることを確かめて返す（シンボリックリンクで外へ出ない）。"""
    rel = clean_relpath(rel)
    path = base / rel if rel else base
    real = Path(os.path.realpath(path))
    base_real = Path(os.path.realpath(base))
    if real != base_real and base_real not in real.parents:
        raise PathError(f"ルートの外を指しています: {rel}")
    return path


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")


def list_live(path: Path) -> list[dict[str, Any]]:
    """フォルダの今の中身（フォルダが先、名前順）。シンボリックリンクは表示だけして、たどらない。"""
    out: list[dict[str, Any]] = []
    with os.scandir(path) as it:
        for de in it:
            try:
                st = de.stat(follow_symlinks=False)
            except OSError:
                continue
            is_dir = de.is_dir(follow_symlinks=False)
            out.append(
                {
                    "name": de.name,
                    "is_dir": is_dir,
                    "is_symlink": de.is_symlink(),
                    "size": None if is_dir else st.st_size,
                    "mtime": _iso(st.st_mtime),
                    "exists_now": True,
                }
            )
    out.sort(key=lambda e: (not e["is_dir"], e["name"].casefold()))
    return out


# 画面の中で開いてよい種類（スクリプトを実行しないもの）
_INLINE_PREFIXES = ("image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp", "audio/", "video/", "application/pdf")
_TEXT_EXTS = frozenset({".txt", ".md", ".csv", ".tsv", ".log", ".json", ".xml", ".html", ".htm", ".svg", ".py", ".js", ".ts", ".css", ".yaml", ".yml", ".toml", ".ini", ".rdp", ".set", ".mq4", ".mq5", ".sh"})


def serve_kind(path: Path) -> tuple[str, bool]:
    """配信するときの Content-Type と、画面の中で開いてよいか（inline）を返す。

    テキストの仲間（HTML・SVG を含む）は text/plain にして、ブラウザに解釈させない。
    """
    ext = path.suffix.lower()
    if ext in _TEXT_EXTS:
        return "text/plain", True
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if mime.startswith(_INLINE_PREFIXES) or mime == "application/pdf":
        return mime, True
    return mime, False
