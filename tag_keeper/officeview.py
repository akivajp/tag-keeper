"""Office 文書（xlsx・docx・pptx）のプレビュー。

追加の道具なしで、中の XML から表示用の構造を取り出す（レイアウトは再現しない）。
- Excel: シートごとの表（数値の日付は、書式が日付なら日付にする）
- Word: 見出し・段落（太字・斜体）・表・貼られた画像
- PowerPoint: スライドごとの題名・文・画像

LibreOffice（soffice）が入っていれば、PDF に変換してレイアウトどおりに見せることもできる（to_pdf）。
変換した PDF は内容ハッシュをキーに保存し、2回目からは変換しない。

信頼できないファイルを開くので、DTD・実体参照を含む XML は読まず、展開後の大きさにも上限を設ける（ZIP 爆弾対策）。
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

log = logging.getLogger(__name__)

# 1つの部品（XML）の展開後の大きさの上限
MAX_PART_BYTES = 50 * 1024 * 1024
# 表示する量の上限（画面が重くならないように）
MAX_SHEETS, MAX_ROWS, MAX_COLS = 20, 500, 50
MAX_BLOCKS, MAX_SLIDES = 2000, 200

NS = {
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
}
OFFICE_EXTS = frozenset({".xlsx", ".xlsm", ".docx", ".docm", ".pptx", ".pptm"})
# 文書に埋め込まれた画像のうち、ブラウザで表示できるもの（EMF・WMF などは出さない）
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp"}
MAX_IMAGES = 50
MAX_IMAGE_BYTES = 20 * 1024 * 1024
# LibreOffice があれば PDF にできる種類（古い形式を含む）
CONVERTIBLE_EXTS = OFFICE_EXTS | {".xls", ".doc", ".ppt", ".odt", ".ods", ".odp", ".rtf"}


class OfficeError(ValueError):
    """Office 文書を読めないときの例外。"""


def _xml(z: zipfile.ZipFile, name: str) -> ET.Element | None:
    """ZIP の中の XML を読む。無ければ None。DTD を含むもの・大きすぎるものは OfficeError。"""
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_PART_BYTES:
        raise OfficeError(f"中の部品が大きすぎます: {name}")
    data = z.read(name)
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise OfficeError(f"DTD を含む XML は読みません: {name}")
    return ET.fromstring(data)


def _rels(z: zipfile.ZipFile, part: str) -> dict[str, str]:
    """部品の関連付け（rId → 部品のパス）。"""
    p = PurePosixPath(part)
    root = _xml(z, str(p.parent / "_rels" / f"{p.name}.rels"))
    out: dict[str, str] = {}
    if root is None:
        return out
    for r in root.findall("rel:Relationship", NS):
        target = r.get("Target", "")
        out[r.get("Id", "")] = target.lstrip("/") if target.startswith("/") else str(PurePosixPath(p.parent / target))
    return out


# --- Excel ---

_DATE_IDS = set(range(14, 23)) | {27, 30, 36, 45, 46, 47, 50, 57}


def _date_styles(z: zipfile.ZipFile) -> set[int]:
    """日付の書式を持つセルの書式番号（styles.xml の cellXfs の並び）。"""
    root = _xml(z, "xl/styles.xml")
    if root is None:
        return set()
    custom: set[int] = set()
    for f in root.findall("s:numFmts/s:numFmt", NS):
        code = re.sub(r'"[^"]*"|\[[^\]]*\]', "", f.get("formatCode", "")).lower()
        if re.search(r"[ymd]", code) and not re.fullmatch(r"[#0.,% ]*", code):
            custom.add(int(f.get("numFmtId", "0")))
    out: set[int] = set()
    xfs = root.find("s:cellXfs", NS)
    for i, xf in enumerate(xfs.findall("s:xf", NS) if xfs is not None else []):
        fid = int(xf.get("numFmtId", "0"))
        if fid in _DATE_IDS or fid in custom:
            out.add(i)
    return out


def _col_index(ref: str) -> int:
    """セル番地の列（A=0, B=1, …）。"""
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):  # type: ignore[union-attr]
        n = n * 26 + ord(ch) - 64
    return n - 1


def _excel_date(value: float) -> str:
    """Excel の日付の通し番号を ISO 形式にする（時刻があれば時刻も）。"""
    base = dt.datetime(1899, 12, 30)
    d = base + dt.timedelta(days=value)
    return d.strftime("%Y-%m-%d %H:%M" if value % 1 else "%Y-%m-%d")


def _xlsx(z: zipfile.ZipFile) -> dict[str, Any]:
    shared: list[str] = []
    sst = _xml(z, "xl/sharedStrings.xml")
    if sst is not None:
        for si in sst.findall("s:si", NS):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['s']}}}t")))
    dates = _date_styles(z)
    wb = _xml(z, "xl/workbook.xml")
    rels = _rels(z, "xl/workbook.xml")
    sheets = []
    for sh in (wb.findall("s:sheets/s:sheet", NS) if wb is not None else [])[:MAX_SHEETS]:
        part = rels.get(sh.get(f"{{{NS['r']}}}id", ""), "")
        root = _xml(z, part) if part else None
        rows: list[list[str]] = []
        truncated = False
        for row in root.findall("s:sheetData/s:row", NS) if root is not None else []:
            if len(rows) >= MAX_ROWS:
                truncated = True
                break
            cells: dict[int, str] = {}
            for c in row.findall("s:c", NS):
                col = _col_index(c.get("r", "A1"))
                if col >= MAX_COLS:
                    truncated = True
                    continue
                kind, v = c.get("t"), c.find("s:v", NS)
                text = v.text if v is not None and v.text is not None else ""
                if kind == "s" and text:
                    text = shared[int(text)] if int(text) < len(shared) else ""
                elif kind == "inlineStr":
                    text = "".join(t.text or "" for t in c.iter(f"{{{NS['s']}}}t"))
                elif kind == "b":
                    text = "TRUE" if text == "1" else "FALSE"
                elif kind is None and text and int(c.get("s", "0")) in dates:
                    try:
                        text = _excel_date(float(text))
                    except ValueError:
                        pass
                cells[col] = text
            if cells:
                width = max(cells) + 1
                rows.append([cells.get(i, "") for i in range(width)])
            else:
                rows.append([])
        sheets.append({"name": sh.get("name", ""), "rows": rows, "truncated": truncated})
    return {"kind": "xlsx", "sheets": sheets}


# --- Word ---


def _heading_styles(z: zipfile.ZipFile) -> dict[str, int]:
    """見出しのスタイル（スタイル ID → 見出しの段）。名前が heading N・見出し N・Title のもの。"""
    root = _xml(z, "word/styles.xml")
    out: dict[str, int] = {}
    if root is None:
        return out
    for st in root.findall("w:style", NS):
        name_el = st.find("w:name", NS)
        name = (name_el.get(f"{{{NS['w']}}}val", "") if name_el is not None else "").lower()
        m = re.search(r"(?:heading|見出し)\s*(\d)", name)
        level = int(m.group(1)) if m else (1 if name == "title" else 0)
        if level:
            out[st.get(f"{{{NS['w']}}}styleId", "")] = level
    return out


def _runs(p: ET.Element) -> list[dict[str, Any]]:
    """段落の中の文字の並び（太字・斜体つき）。"""
    out: list[dict[str, Any]] = []
    for r in p.iter(f"{{{NS['w']}}}r"):
        rpr = r.find("w:rPr", NS)
        bold = rpr is not None and rpr.find("w:b", NS) is not None and rpr.find("w:b", NS).get(f"{{{NS['w']}}}val", "1") not in ("0", "false")
        italic = rpr is not None and rpr.find("w:i", NS) is not None and rpr.find("w:i", NS).get(f"{{{NS['w']}}}val", "1") not in ("0", "false")
        text = ""
        for el in r:
            tag = el.tag.split("}")[-1]
            if tag == "t":
                text += el.text or ""
            elif tag == "tab":
                text += "\t"
            elif tag in ("br", "cr"):
                text += "\n"
        if text:
            if out and out[-1]["b"] == bold and out[-1]["i"] == italic:
                out[-1]["t"] += text
            else:
                out.append({"t": text, "b": bold, "i": italic})
    return out


def _docx(z: zipfile.ZipFile) -> dict[str, Any]:
    root = _xml(z, "word/document.xml")
    if root is None:
        raise OfficeError("本文（word/document.xml）がありません")
    headings = _heading_styles(z)
    rels = _rels(z, "word/document.xml")
    images = 0
    body = root.find("w:body", NS)
    blocks: list[dict[str, Any]] = []
    for el in list(body) if body is not None else []:
        if len(blocks) >= MAX_BLOCKS:
            blocks.append({"type": "more"})
            break
        tag = el.tag.split("}")[-1]
        if tag == "p":
            style = el.find("w:pPr/w:pStyle", NS)
            sid = style.get(f"{{{NS['w']}}}val", "") if style is not None else ""
            blocks.append({"type": "p", "level": headings.get(sid, 0), "runs": _runs(el)})
            # 段落に貼られた画像（スクリーンショットだけの文書もある）
            for part in _images(el, rels):
                if images < MAX_IMAGES:
                    blocks.append({"type": "image", "part": part})
                    images += 1
        elif tag == "tbl":
            rows = []
            for tr in el.findall("w:tr", NS)[:MAX_ROWS]:
                rows.append(["\n".join("".join(r["t"] for r in _runs(p)) for p in tc.findall("w:p", NS)) for tc in tr.findall("w:tc", NS)[:MAX_COLS]])
            blocks.append({"type": "table", "rows": rows})
    return {"kind": "docx", "blocks": blocks}


# --- PowerPoint ---


def _pptx(z: zipfile.ZipFile) -> dict[str, Any]:
    pres = _xml(z, "ppt/presentation.xml")
    rels = _rels(z, "ppt/presentation.xml")
    parts = [rels.get(s.get(f"{{{NS['r']}}}id", ""), "") for s in (pres.findall("p:sldIdLst/p:sldId", NS) if pres is not None else [])]
    if not any(parts):
        parts = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=lambda n: int(re.search(r"\d+", n).group(0)))  # type: ignore[union-attr]
    slides = []
    images = 0
    for part in parts[:MAX_SLIDES]:
        root = _xml(z, part) if part else None
        title, paras = "", []
        pics: list[str] = []
        if root is not None and images < MAX_IMAGES:
            pics = _images(root, _rels(z, part))[: max(0, min(6, MAX_IMAGES - images))]
            images += len(pics)
        for sp in root.iter(f"{{{NS['p']}}}sp") if root is not None else []:
            ph = sp.find("p:nvSpPr/p:nvPr/p:ph", NS)
            is_title = ph is not None and ph.get("type") in ("title", "ctrTitle")
            texts = ["".join(t.text or "" for t in p.iter(f"{{{NS['a']}}}t")) for p in sp.iter(f"{{{NS['a']}}}p")]
            texts = [x for x in texts if x.strip()]
            if is_title and texts and not title:
                title = " ".join(texts)
            else:
                paras.extend(texts)
        slides.append({"title": title, "paragraphs": paras, "images": pics})
    return {"kind": "pptx", "slides": slides}


def _images(el: ET.Element, rels: dict[str, str]) -> list[str]:
    """要素の中に貼られた画像の部品のパス（ブラウザで表示できる種類だけ）。"""
    out = []
    for blip in el.iter(f"{{{NS['a']}}}blip"):
        part = rels.get(blip.get(f"{{{NS['r']}}}embed", ""), "")
        if PurePosixPath(part).suffix.lower() in IMAGE_TYPES and part not in out:
            out.append(part)
    return out


def image_part(path: Path, part: str) -> tuple[bytes, str]:
    """文書に埋め込まれた画像を取り出す（種類と大きさを確かめる）。"""
    mime = IMAGE_TYPES.get(PurePosixPath(part).suffix.lower())
    if mime is None:
        raise OfficeError(f"画像ではありません: {part}")
    try:
        with zipfile.ZipFile(path) as z:
            info = z.getinfo(part)
            if info.file_size > MAX_IMAGE_BYTES:
                raise OfficeError(f"画像が大きすぎます: {part}")
            return z.read(part), mime
    except (zipfile.BadZipFile, KeyError) as e:
        raise OfficeError(f"画像を取り出せません: {part}") from e


def preview(path: Path) -> dict[str, Any]:
    """Office 文書を、画面に出す構造にする。読めなければ OfficeError。"""
    ext = path.suffix.lower()
    if ext not in OFFICE_EXTS:
        raise OfficeError(f"この種類は読めません: {ext}")
    try:
        with zipfile.ZipFile(path) as z:
            if ext in (".xlsx", ".xlsm"):
                return _xlsx(z)
            if ext in (".docx", ".docm"):
                return _docx(z)
            return _pptx(z)
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, IndexError) as e:
        raise OfficeError(f"Office 文書として読めません: {e}") from e


# --- LibreOffice による PDF への変換 ---


def soffice() -> str | None:
    """LibreOffice の実行ファイル（無ければ None）。"""
    return shutil.which("soffice") or shutil.which("libreoffice")


def to_pdf(path: Path, sha256: str, cache_dir: Path, timeout: float = 120) -> Path:
    """LibreOffice で PDF に変換する（内容ハッシュをキーに保存し、2回目からは変換しない）。"""
    exe = soffice()
    if exe is None:
        raise OfficeError("LibreOffice（soffice）が入っていません")
    out = cache_dir / f"{sha256}.pdf"
    if out.exists():
        return out
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"in{path.suffix.lower()}"
        shutil.copyfile(path, src)
        # 利用者の LibreOffice の設定を汚さないよう、使い捨ての設定フォルダで動かす
        profile = Path(tmp) / "profile"
        cmd = [exe, f"-env:UserInstallation=file://{profile}", "--headless", "--convert-to", "pdf", "--outdir", tmp, str(src)]
        try:
            subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as e:
            raise OfficeError("PDF への変換が時間内に終わりませんでした") from e
        pdf = Path(tmp) / "in.pdf"
        if not pdf.exists():
            raise OfficeError("PDF に変換できませんでした")
        shutil.move(str(pdf), out)
    return out
