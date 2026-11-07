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

import functools
import html
import logging
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
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


# ---------- 全文検索の索引のための抽出（要件 F-EX-1・F-SE-2） ----------
#
# 上の extract() は整理の提案のためのもの（先頭の数ページ・数千字、スキャンは画像のままモデルに見せる）。
# 索引のためには、文書全体の文字（上限つき）を取り出し、スキャンの PDF は tesseract で OCR にかける。

# Office 文書（マクロ付きのものも、中の XML の構成は同じ）
INDEX_OFFICE_PARTS = {
    ".docx": OFFICE_PARTS[".docx"], ".docm": OFFICE_PARTS[".docx"],
    ".pptx": OFFICE_PARTS[".pptx"], ".pptm": OFFICE_PARTS[".pptx"],
    ".xlsx": OFFICE_PARTS[".xlsx"], ".xlsm": OFFICE_PARTS[".xlsx"],
}
# OCR にかけられる画像（ocr_images を有効にしたときだけ）
OCR_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"})
# pdftotext で読むページ数の上限（巨大な PDF で時間がかかりすぎないように）
INDEX_PDF_PAGES = 300
# Office 文書の中の1つの XML の大きさの上限（壊れた・細工したファイルでメモリを使い切らないように）
MAX_OFFICE_PART = 50 * 1024 * 1024
# 和文の文字（OCR が文字の間に入れる空白を詰めるのに使う）
_CJK = "　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯"
_CJK_GAP = re.compile(f"(?<=[{_CJK}])[ \t]+(?=[{_CJK}])")


def index_exts(ocr_images: bool = False) -> frozenset[str]:
    """全文検索の索引に入れるファイルの種類（拡張子）。"""
    exts = {".pdf", *INDEX_OFFICE_PARTS, *TEXT_EXTS}
    if ocr_images:
        exts |= OCR_IMAGE_EXTS
    return frozenset(exts)


def normalize_text(text: str, limit: int, *, join_cjk: bool = False) -> str:
    """索引に入れる本文の表記をそろえる（NFKC、空白をまとめ、上限で切る）。

    NFKC で全角の英数字と半角のカナをそろえるので、検索語も同じように直してから探す。

    Args:
        text: 取り出した本文。
        limit: 上限（文字数）。
        join_cjk: 和文の文字どうしの間の空白を詰めるか。OCR と PDF から取り出した文字は、
            文字の間に空白が入りやすく「請 求 書」では探せないので詰める。書き手が入れた空白（Office・テキスト）は残す。
    """
    t = unicodedata.normalize("NFKC", text)
    if join_cjk:
        t = _CJK_GAP.sub("", t)
    return _squeeze(t, limit)


@functools.lru_cache(maxsize=1)
def tesseract_languages() -> frozenset[str] | None:
    """tesseract に入っている言語。tesseract が無ければ None。"""
    if shutil.which("tesseract") is None:
        return None
    try:
        res = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    # 1行目は「List of available languages in ...」
    return frozenset(line.strip() for line in res.stdout.splitlines()[1:] if line.strip())


def ocr_languages(wanted: str) -> str | None:
    """使う OCR の言語（"jpn+eng" のうち、入っているものだけ）。OCR を使えなければ None。"""
    langs = tesseract_languages()
    if not langs or shutil.which("pdftoppm") is None:
        return None
    have = [x for x in wanted.split("+") if x in langs]
    return "+".join(have) if have else None


def _niced(cmd: list[str]) -> list[str]:
    """他の処理の邪魔をしないよう、優先度を下げて動かす（nice があれば）。"""
    return ["nice", "-n", "19", *cmd] if shutil.which("nice") else cmd


def ocr_image(path: Path, lang: str, timeout: float = 180) -> str:
    """画像を tesseract で OCR にかけた文字を返す。読めなければ空。"""
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"}  # 1つのコアだけを使う
    res = subprocess.run(
        _niced(["tesseract", str(path), "stdout", "-l", lang]), capture_output=True, timeout=timeout, check=False, env=env
    )
    return res.stdout.decode("utf-8", errors="replace") if res.returncode == 0 else ""


def ocr_pdf(path: Path, lang: str, pages: int, timeout: float = 180) -> str:
    """PDF の先頭の pages ページを画像にして（300dpi・グレー）、OCR にかけた文字を返す。"""
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "page"
        res = subprocess.run(
            _niced(["pdftoppm", "-r", "300", "-gray", "-png", "-f", "1", "-l", str(pages), str(path), str(prefix)]),
            capture_output=True, timeout=timeout, check=False,
        )
        if res.returncode != 0:
            return ""
        return "\n".join(ocr_image(p, lang, timeout) for p in sorted(Path(tmp).glob("page*.png")))


def extract_text(
    path: Path,
    *,
    max_chars: int = 50_000,
    max_mb: int = 200,
    ocr_lang: str | None = None,
    ocr_pages: int = 3,
    ocr_images: bool = False,
) -> Extracted:
    """全文検索の索引に入れる本文を取り出す。例外にはせず、読めなければ method="error" で返す。

    Args:
        path: 対象のファイル。
        max_chars: 本文の上限（文字数）。
        max_mb: これより大きいファイルは読まない（MB）。
        ocr_lang: OCR に使う tesseract の言語（ocr_languages の結果）。None なら OCR しない。
        ocr_pages: OCR にかける PDF のページ数。
        ocr_images: 画像も OCR にかけるか。

    Returns:
        method は pdftotext / office / text / ocr（読めた）、scan（文字の無い PDF・画像で、OCR を使えるようになったら読み直す）、
        none（中身が空・読まない種類・大きすぎる）、error（壊れている・時間切れ）のどれか。
    """
    ext = path.suffix.lower()
    try:
        if path.stat().st_size > max_mb * 1024 * 1024:
            return Extracted("none", note=f"{max_mb}MB より大きいので読まない")
        if ext == ".pdf":
            if shutil.which("pdftotext") is None:
                # 入れたら読み直せるよう、保存しない印を返す
                return Extracted("unavailable", note="pdftotext がありません")
            # 裏で少しずつ進める処理なので、優先度を下げて動かす（画面の操作や同期の邪魔をしない）
            res = _run(_niced(["pdftotext", "-l", str(INDEX_PDF_PAGES), "-enc", "UTF-8", str(path), "-"]), timeout=180)
            text = normalize_text(_decode(res.stdout), max_chars, join_cjk=True) if res.returncode == 0 else ""
            if len(text) >= SCAN_THRESHOLD:
                return Extracted("pdftotext", text)
            if res.returncode != 0 and b"Incorrect password" in res.stderr:
                return Extracted("none", note="パスワードで保護された PDF")
            if ocr_lang:
                ocr = normalize_text(ocr_pdf(path, ocr_lang, ocr_pages), max_chars, join_cjk=True)
                return Extracted("ocr", ocr or text, note="" if ocr else "OCR で文字を読み取れなかった")
            return Extracted("scan", text, note="文字の無い PDF（OCR を使えるようになったら読み直す）")
        if ext in INDEX_OFFICE_PARTS:
            parts: list[str] = []
            with zipfile.ZipFile(path) as z:
                for info in sorted(z.infolist(), key=lambda i: i.filename):
                    if not INDEX_OFFICE_PARTS[ext].match(info.filename):
                        continue
                    if info.file_size > MAX_OFFICE_PART:
                        return Extracted("error", note=f"中の XML が大きすぎる: {info.filename}")
                    xml = z.read(info).decode("utf-8", errors="replace")
                    # 段落・行・セルの区切りを改行にしてから、タグを除き、文字参照（&amp; など）を戻す
                    xml = re.sub(r"</(w:p|a:p|si)>", "\n", xml)
                    parts.append(html.unescape(re.sub(r"<[^>]+>", "", xml)))
            return Extracted("office", normalize_text("\n".join(parts), max_chars))
        if ext in TEXT_EXTS:
            with path.open("rb") as f:
                data = f.read(max_chars * 4)
            return Extracted("text", normalize_text(_decode(data), max_chars))
        if ext in OCR_IMAGE_EXTS and ocr_images:
            if not ocr_lang:
                return Extracted("scan", note="画像（OCR を使えるようになったら読み直す）")
            return Extracted("ocr", normalize_text(ocr_image(path, ocr_lang), max_chars, join_cjk=True))
    except subprocess.TimeoutExpired:
        return Extracted("error", note="時間内に読み終わらなかった")
    except FileNotFoundError as e:
        # 道具（pdftotext など）が無い。入れたら読み直せるよう、保存しない印を返す
        if e.filename and Path(str(e.filename)).name in ("pdftotext", "pdftoppm", "tesseract", "nice"):
            return Extracted("unavailable", note=f"{e.filename} がありません")
        return Extracted("error", note=f"読めない: {e}")
    except (OSError, zipfile.BadZipFile, ValueError, RuntimeError) as e:
        return Extracted("error", note=f"読めない: {e}")
    return Extracted("none", note="内容を読まない種類")
