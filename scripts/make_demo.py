"""README のスクリーンショット用の見本のデータを作る（架空のファイルだけで、利用者のファイルは使わない）。

    uv run python scripts/make_demo.py DEMO_DIR

DEMO_DIR の下に、見本のツリー（root/）と設定ファイル（config.toml）を作る。続けて次のように使う:

    uv run tag-keeper --config DEMO_DIR/config.toml --db DEMO_DIR/data/catalog.db scan demo
    uv run tag-keeper --config DEMO_DIR/config.toml --db DEMO_DIR/data/catalog.db serve --port 8093

PDF・PNG・xlsx・docx は、外部のライブラリを使わずに最小限の形で書く。
"""

from __future__ import annotations

import argparse
import struct
import zipfile
import zlib
from pathlib import Path


def pdf(lines: list[str]) -> bytes:
    """文字だけの1ページの PDF（pdftotext で読める）。"""
    text = "BT /F1 13 Tf 72 740 Td 18 TL " + " ".join(f"({ln.replace('(', '[').replace(')', ']')}) '" for ln in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(text)} >>\nstream\n{text}\nendstream",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def png(width: int, height: int, top: tuple[int, int, int], bottom: tuple[int, int, int], sun: bool = True) -> bytes:
    """空のようなグラデーションに、太陽と山の影を描いた PNG。"""
    rows = []
    for y in range(height):
        f = y / (height - 1)
        sky = tuple(int(top[i] + (bottom[i] - top[i]) * f) for i in range(3))
        row = bytearray([0])
        for x in range(width):
            c = sky
            if sun and (x - width * 0.7) ** 2 + (y - height * 0.35) ** 2 < (height * 0.12) ** 2:
                c = (255, 224, 140)
            mountain = height * (0.72 - 0.18 * abs(((x / width) * 3) % 2 - 1))
            if y > mountain:
                c = (40, 60, 70)
            row += bytes(c)
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


def xlsx(path: Path, rows: list[list[object]], sheet: str = "Budget") -> None:
    """表1枚の Excel ブック（1列目が日付なら日付の書式にする）。"""
    strings: list[str] = []

    def cell(ref: str, v: object) -> str:
        if isinstance(v, str):
            strings.append(v)
            return f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>'
        if isinstance(v, tuple):  # (Excel の日付の通し番号,)
            return f'<c r="{ref}" s="1"><v>{v[0]}</v></c>'
        return f'<c r="{ref}"><v>{v}</v></c>'

    body = "".join(
        f'<row r="{i}">' + "".join(cell(f"{chr(65 + j)}{i}", v) for j, v in enumerate(r)) + "</row>" for i, r in enumerate(rows, 1)
    )
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns}><sheets><sheet name="{sheet}" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        z.writestr("xl/styles.xml", f'<styleSheet {ns}><cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>')
        z.writestr("xl/sharedStrings.xml", f"<sst {ns}>" + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData>{body}</sheetData></worksheet>")


def docx(path: Path, blocks: list[tuple[str, str]]) -> None:
    """見出し（h1・h2）と段落だけの Word 文書。"""
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    paras = []
    for kind, text in blocks:
        style = {"h1": '<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>', "h2": '<w:pPr><w:pStyle w:val="Heading2"/></w:pPr>'}.get(kind, "")
        run = f"<w:r><w:rPr><w:b/></w:rPr><w:t>{text}</w:t></w:r>" if kind == "b" else f"<w:r><w:t>{text}</w:t></w:r>"
        paras.append(f"<w:p>{style}{run}</w:p>")
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "word/styles.xml",
            f'<w:styles {w}><w:style w:styleId="Heading1"><w:name w:val="heading 1"/></w:style><w:style w:styleId="Heading2"><w:name w:val="heading 2"/></w:style></w:styles>',
        )
        z.writestr("word/document.xml", f"<w:document {w}><w:body>{''.join(paras)}</w:body></w:document>")


def write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def main() -> None:
    """見本のツリーと設定ファイルを作る。"""
    ap = argparse.ArgumentParser(description="README のスクリーンショット用の見本のデータを作る")
    ap.add_argument("demo_dir", type=Path, help="見本を作るフォルダ（root/ と config.toml ができる）")
    ap.add_argument("--model", default="gemma4:31b-cloud", help="整理の提案に使う ollama のモデル")
    args = ap.parse_args()
    d: Path = args.demo_dir.resolve()
    r = d / "root"

    invoice = lambda who, no, date, amount: pdf([f"{who}", "", f"INVOICE  No. {no}", f"Date: {date}", "", "Bill to: Alex Morgan", "", f"Web hosting (annual plan)      {amount}", f"Total due                      {amount}", "", "Thank you for your business."])  # noqa: E731
    write(r / "Finance" / "Invoices" / "2025" / "20250114_Northwind_hosting_invoice.pdf", invoice("Northwind Hosting Ltd.", "NW-2025-0114", "2025-01-14", "USD 240.00"))
    write(r / "Finance" / "Invoices" / "2025" / "20250312_Contoso_domain_invoice.pdf", invoice("Contoso Domains", "CD-88213", "2025-03-12", "USD 18.00"))
    write(r / "Finance" / "Receipts" / "2025" / "20250601_Fabrikam_office_chair_receipt.pdf", pdf(["Fabrikam Furniture", "", "RECEIPT", "Date: 2025-06-01", "", "Ergonomic office chair     USD 329.00", "Paid by card"]))
    write(r / "Finance" / "Bank statements" / "20250430_Woodgrove_statement_April.pdf", pdf(["Woodgrove Bank", "", "Account statement - April 2025", "Opening balance   USD 4,210.33", "Closing balance   USD 5,032.18"]))
    xlsx(
        r / "Projects" / "2025_Website_Redesign" / "budget.xlsx",
        [["Date", "Item", "Vendor", "Amount (USD)", "Status"],
         [(45671,), "Design workshop", "Tailspin Studio", 1200, "Paid"],
         [(45699,), "Hosting (annual)", "Northwind Hosting", 240, "Paid"],
         [(45728,), "Domain renewal", "Contoso Domains", 18, "Paid"],
         [(45778,), "Stock photos", "Litware Images", 95, "Pending"],
         [(45809,), "Accessibility audit", "Adatum Consulting", 800, "Planned"],
         ["", "", "Total", 2353, ""]],
    )
    docx(
        r / "Projects" / "2025_Website_Redesign" / "proposal.docx",
        [("h1", "Website redesign proposal"), ("p", "Prepared by Alex Morgan, March 2025."), ("h2", "Goals"),
         ("p", "Make the site faster, easier to navigate and accessible on every device."), ("h2", "Timeline"),
         ("b", "April: design workshop and wireframes."), ("p", "May to June: build, content migration and testing."), ("h2", "Budget"),
         ("p", "See budget.xlsx in the same folder.")],
    )
    write(r / "Projects" / "2025_Website_Redesign" / "meeting_notes.md", "# Kick-off meeting\n\n- Agreed on a two-column layout\n- Next review: April 22\n")
    write(r / "Projects" / "old_app" / "node_modules" / "left-pad" / "index.js", "module.exports = (s) => s;\n" * 40)
    write(r / "Projects" / "old_app" / "README.md", "# Old app\n")
    write(r / "Photos" / "2025-06_Lake_trip" / "IMG_0412.png", png(480, 300, (110, 160, 230), (250, 200, 160)))
    write(r / "Photos" / "2025-06_Lake_trip" / "IMG_0413.png", png(480, 300, (60, 90, 170), (240, 150, 120)))
    write(r / "Photos" / "2025-06_Lake_trip" / "Thumbs.db", b"\x00" * 64)
    write(r / "Photos" / "Wallpapers" / "dusk.png", png(480, 300, (30, 40, 90), (230, 120, 110)))
    write(r / "Downloads" / "VirtualBox-7.1.4-Win.exe", b"MZ" + b"\x00" * 2048)
    write(r / "Downloads" / "ubuntu-26.04-desktop-amd64.iso", b"\x00" * 4096)
    # 受け皿（Inbox・tmp）: 名前に意味の無いファイル
    write(r / "Inbox" / "downloadfile.pdf", invoice("Northwind Hosting Ltd.", "NW-2025-0910", "2025-09-10", "USD 240.00"))
    write(r / "Inbox" / "scan_0007.pdf", pdf(["Fabrikam Furniture", "", "RECEIPT", "Date: 2025-08-21", "", "Standing desk     USD 549.00", "Paid by card"]))
    write(r / "Inbox" / "document(3).pdf", pdf(["Woodgrove Bank", "", "Account statement - August 2025", "Opening balance   USD 5,402.10", "Closing balance   USD 6,118.77"]))
    write(r / "Inbox" / "notes.txt", "Kick-off follow-up for the website redesign.\nReview wireframes with Tailspin Studio on Sept 3, 2025.\n")
    write(r / "Inbox" / "IMG_20250621_183012.png", png(480, 300, (90, 140, 220), (250, 210, 170)))
    write(r / "Inbox" / "tmp" / "20250114_Northwind_hosting_invoice (1).pdf", invoice("Northwind Hosting Ltd.", "NW-2025-0114", "2025-01-14", "USD 240.00"))

    data = d / "data"
    (d / "config.toml").write_text(
        f'''[[roots]]
name = "demo"
path = "{r}"

[tags]
dir = "{data / "tags"}"

[plan]
quarantine_dir = "{data / "quarantine"}"
snapper_config = ""

[organize]
model = "{args.model}"
''',
        encoding="utf-8",
    )
    print(f"見本を作りました: {r}")


if __name__ == "__main__":
    main()
