"""ファイルの内容の抽出（要件 F-EX-1 の一部）。

整理の提案（名前と移動先）に使うため、ファイルの種類ごとに本文のテキストか、モデルに見せる画像を取り出す。
外部のサービスには何も送らない。使う道具は標準ライブラリと poppler（pdftotext・pdftoppm）だけ。

- PDF: pdftotext で最初の数ページの文字を取り出す。文字がほとんど無ければスキャンとみなし、
  1ページ目を pdftoppm で画像にする
- Office（docx・xlsx・pptx）: ZIP の中の XML から文字だけを取り出す
- テキスト（txt・md・csv など）: 文字コードを推測して読む
- 画像（jpg・png など）: そのまま画像としてモデルに見せる
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

TEXT_EXTS = frozenset(
    {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".log", ".ini", ".yaml", ".yml", ".toml", ".rdp", ".set", ".py", ".mq4", ".mq5"}
)
IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"})
OFFICE_PARTS = {
    ".docx": re.compile(r"^word/(document|header\d*|footer\d*)\.xml$"),
    ".pptx": re.compile(r"^ppt/slides/slide\d+\.xml$"),
    ".xlsx": re.compile(r"^xl/sharedStrings\.xml$"),
}
# 文字が無いとみなす（スキャンの PDF とみなす）本文の長さ
SCAN_THRESHOLD = 30
# モデルに見せる画像の大きさの上限（バイト）
MAX_IMAGE_BYTES = 20 * 1024 * 1024
# 読み取るページ数
PDF_PAGES = 3


@dataclass
class Extracted:
    """抽出の結果。"""

    method: str  # pdftotext / office / text / image / pdf-image / none
    text: str = ""
    image: bytes | None = None  # モデルに見せる画像（JPEG か PNG）。無ければ None
    note: str = ""


def _run(cmd: list[str], timeout: float = 60) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)


def _decode(data: bytes) -> str:
    """文字コードを推測して読む（UTF-8 → Shift_JIS 系 → 置き換え）。"""
    for enc in ("utf-8-sig", "cp932", "euc-jp"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _squeeze(text: str, limit: int) -> str:
    """連続する空白をまとめ、上限の長さに切る。"""
    text = re.sub(r"[ \t　]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()[:limit]


def pdf_first_page_png(path: Path, dpi: int = 110) -> bytes | None:
    """PDF の1ページ目を PNG にする（スキャンの PDF をモデルに見せるため）。"""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "page"
        res = _run(["pdftoppm", "-png", "-r", str(dpi), "-f", "1", "-l", "1", "-singlefile", str(path), str(out)])
        png = out.with_suffix(".png")
        if res.returncode != 0 or not png.exists():
            return None
        return png.read_bytes()


def extract(path: Path, max_chars: int = 4000, use_images: bool = True) -> Extracted:
    """ファイルの本文か画像を取り出す。読めない種類なら method="none" を返す（例外にしない）。"""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            res = _run(["pdftotext", "-l", str(PDF_PAGES), "-layout", str(path), "-"])
            text = _squeeze(_decode(res.stdout), max_chars) if res.returncode == 0 else ""
            if len(text) >= SCAN_THRESHOLD:
                return Extracted("pdftotext", text)
            if use_images:
                png = pdf_first_page_png(path)
                if png is not None:
                    return Extracted("pdf-image", text, png, "文字の無い PDF（スキャン）")
            return Extracted("none", text, note="文字を取り出せない PDF")
        if ext in OFFICE_PARTS:
            parts: list[str] = []
            with zipfile.ZipFile(path) as z:
                for name in sorted(z.namelist()):
                    if OFFICE_PARTS[ext].match(name):
                        xml = z.read(name).decode("utf-8", errors="replace")
                        # 段落・行の区切りを改行にしてから、タグを除く
                        xml = re.sub(r"</(w:p|a:p|si)>", "\n", xml)
                        parts.append(re.sub(r"<[^>]+>", "", xml))
            return Extracted("office", _squeeze("\n".join(parts), max_chars))
        if ext in TEXT_EXTS:
            with path.open("rb") as f:
                data = f.read(max_chars * 4)
            return Extracted("text", _squeeze(_decode(data), max_chars))
        if ext in IMAGE_EXTS:
            if not use_images:
                return Extracted("none", note="画像（モデルに見せない設定）")
            if path.stat().st_size > MAX_IMAGE_BYTES:
                return Extracted("none", note="画像が大きすぎる")
            return Extracted("image", image=path.read_bytes())
    except (OSError, zipfile.BadZipFile, subprocess.TimeoutExpired) as e:
        log.warning("内容を読めませんでした: %s (%s)", path, e)
        return Extracted("none", note=f"読めない: {e}")
    return Extracted("none", note="内容を読まない種類")
