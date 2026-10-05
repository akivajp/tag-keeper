"""置く価値の薄いファイルの検出ルール（要件 F-OR-1）。

ここに置くのは名前とサイズだけで判定できるルールで、ファイルの中身は読まない。
フォルダ単位の判定（アプリのデータ・生成物・空フォルダ）は report モジュールで行う。
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass

# カテゴリ（報告での表示順）
CAT_APP_DATA = "アプリのデータ"
CAT_REGENERABLE = "作り直せる生成物"
CAT_VM_IMAGE = "仮想マシンのディスク"
CAT_INSTALLER = "インストーラー・ディスクイメージ"
CAT_CONFLICT = "競合コピー・複製"
CAT_OFFICE_LOCK = "Office のロックファイル"
CAT_OS_JUNK = "OS・アプリが作るゴミ"
CAT_TEMP = "一時ファイル・バックアップ"
CAT_EMPTY_FILE = "空ファイル"
CAT_EMPTY_DIR = "空フォルダ"

CATEGORY_ORDER = [
    CAT_APP_DATA,
    CAT_REGENERABLE,
    CAT_VM_IMAGE,
    CAT_INSTALLER,
    CAT_CONFLICT,
    CAT_OFFICE_LOCK,
    CAT_OS_JUNK,
    CAT_TEMP,
    CAT_EMPTY_FILE,
    CAT_EMPTY_DIR,
]

# コマンドラインでカテゴリを指定するための短い名前
CATEGORY_KEYS = {
    "app-data": CAT_APP_DATA,
    "regenerable": CAT_REGENERABLE,
    "vm-image": CAT_VM_IMAGE,
    "installer": CAT_INSTALLER,
    "conflict": CAT_CONFLICT,
    "office-lock": CAT_OFFICE_LOCK,
    "os-junk": CAT_OS_JUNK,
    "temp": CAT_TEMP,
    "empty-file": CAT_EMPTY_FILE,
    "empty-dir": CAT_EMPTY_DIR,
}

# 作り直せる生成物のフォルダ名（中身ごと候補にする）
REGENERABLE_DIRS = frozenset(
    {
        "node_modules", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        ".tox", ".gradle", ".next", ".nuxt", "bower_components", ".parcel-cache", ".terraform",
    }
)

# アプリのデータとみなすフォルダ名（Windows の「ドキュメント」に作られやすいもの）
DEFAULT_APP_DATA_NAMES = frozenset(
    {
        "DMMGames", "My Games", "StreamFab", "Steam", "SteamLibrary", "steamapps",
        "Epic Games", "Rockstar Games", "Battle.net",
    }
)

# キャッシュとみなすフォルダ名（小文字で比較）
CACHE_DIR_NAMES = frozenset({"cache", "caches", "code cache", "gpucache", "shadercache", "inetcache"})

# OS やアプリが作るゴミのファイル名（小文字で比較）
OS_JUNK_NAMES = frozenset({"thumbs.db", "ehthumbs.db", ".ds_store", "desktop.ini"})

# 一時ファイル・ダウンロード途中のファイル・自動バックアップの拡張子
TEMP_EXTS = frozenset({".tmp", ".temp", ".crdownload", ".part", ".partial", ".download", ".bak", ".dmp"})

# 仮想マシンのディスクイメージ
VM_IMAGE_EXTS = frozenset({".vdi", ".vmdk", ".vhd", ".vhdx", ".qcow2"})

# 常に「再入手できるもの」とみなす拡張子
INSTALLER_EXTS = frozenset({".iso", ".msi", ".dmg", ".pkg", ".deb", ".rpm", ".appimage"})

# .exe はポータブルアプリの本体も多いので、インストーラーらしいものだけを拾う
_INSTALLER_NAME = re.compile(r"setup|install", re.IGNORECASE)
_DOWNLOAD_DIRS = frozenset({"downloads", "ダウンロード"})

# Office のロックファイル（~$report.docx、.~lock.report.odt#）
_OFFICE_LOCK = re.compile(r"^(~\$|\.~lock\..*#$)")

# 競合コピー・複製の名前（拡張子を除いた部分に照合する）
_CONFLICT_PATTERNS = [
    ("Linux 版 onedrive クライアントの競合コピー", re.compile(r"-safeBackup-\d+$")),
    ("pCloud の競合コピー", re.compile(r"\[conflicted\]|\.conflicted$", re.IGNORECASE)),
    ("rclone bisync の競合コピー", re.compile(r"\.conflict\d+$")),
    ("競合コピー", re.compile(r"_conflict( \(\d+\))?$", re.IGNORECASE)),
    ("番号付きの複製", re.compile(r" \(\d+\)$")),
    ("コピー", re.compile(r"(のコピー| - コピー| - copy| copy)( \(\d+\))?$", re.IGNORECASE)),
]


@dataclass(frozen=True)
class Match:
    """ルールに一致した結果。"""

    category: str
    reason: str


def split_ext(name: str) -> tuple[str, str]:
    """名前を（拡張子を除いた部分, 小文字の拡張子）に分ける。"""
    stem, ext = os.path.splitext(name)
    return stem, ext.lower()


def classify_file(
    relpath: str, size: int, device_names: Iterable[str] = ()
) -> Match | None:
    """ファイル1件を、名前とサイズで分類する。どれにも当たらなければ None。

    Args:
        relpath: ルートからの相対パス（区切りは /）。
        size: サイズ（バイト）。
        device_names: 競合コピーの名前に付く端末名（例: "report-zefat.xlsx" の "zefat"）。
    """
    name = relpath.rsplit("/", 1)[-1]
    stem, ext = split_ext(name)
    lower = name.lower()

    if _OFFICE_LOCK.match(name):
        return Match(CAT_OFFICE_LOCK, "Office が編集中に作る一時ファイル")
    if lower in OS_JUNK_NAMES:
        return Match(CAT_OS_JUNK, "OS やアプリが自動で作るファイル")
    if ext in VM_IMAGE_EXTS:
        return Match(CAT_VM_IMAGE, "巨大で、同期やバックアップに向かない")
    if ext in INSTALLER_EXTS:
        return Match(CAT_INSTALLER, "再入手できる可能性が高い")
    if ext == ".exe":
        parts = {p.lower() for p in relpath.split("/")[:-1]}
        if _INSTALLER_NAME.search(stem) or parts & _DOWNLOAD_DIRS:
            return Match(CAT_INSTALLER, "インストーラーらしい名前か、ダウンロードフォルダにある")
    if ext in TEMP_EXTS:
        return Match(CAT_TEMP, f"{ext} は一時ファイルや自動バックアップに使われる")
    for reason, pattern in _CONFLICT_PATTERNS:
        if pattern.search(stem) or pattern.search(name):
            return Match(CAT_CONFLICT, reason)
    for device in device_names:
        if device and stem.lower().endswith("-" + device.lower()):
            return Match(CAT_CONFLICT, f"端末名（{device}）付きの競合コピー")
    if size == 0:
        return Match(CAT_EMPTY_FILE, "中身が空")
    return None


def conflict_original(name: str, device_names: Iterable[str] = ()) -> str | None:
    """競合コピー・複製の名前から、元のファイルの名前を推定する。推定できなければ None。

    例: "report (1).pdf" → "report.pdf"、"plan-zefat.xlsx" → "plan.xlsx"、
    "note-zefat-safeBackup-0001.one" → "note.one"、"a.txt.conflict1" → "a.txt"
    """
    stem, ext = os.path.splitext(name)  # 元の名前なので、拡張子の大文字・小文字はそのまま使う
    base: str | None = None
    for _reason, pattern in _CONFLICT_PATTERNS:
        if pattern.search(stem):
            stem = pattern.sub("", stem).rstrip()
            base = stem
            break
        if pattern.search(name):
            # 拡張子の後ろに印が付く形（a.txt.conflict1）
            stem, ext = os.path.splitext(pattern.sub("", name).rstrip())
            base = stem
            break
    # 端末名の印（単独で付く場合と、onedrive クライアントの -safeBackup- の前に付く場合）
    for device in device_names:
        suffix = "-" + device.lower()
        if device and stem.lower().endswith(suffix) and len(stem) > len(suffix):
            base = stem[: -len(suffix)]
            break
    return base + ext if base else None


# バージョン管理システムが中身を管理するフォルダ
VCS_DIRS = frozenset({".git", ".svn", ".hg"})


def is_in_vcs_dir(relpath: str) -> bool:
    """バージョン管理システムの管理フォルダ（.git など）の中にあるかを返す。"""
    return any(part in VCS_DIRS for part in relpath.split("/")[:-1])


def has_no_extension(name: str) -> bool:
    """拡張子の無い名前かを返す（隠しファイルの先頭の . は拡張子とみなさない）。"""
    return split_ext(name)[1] == ""
