"""Office 文書のプレビューと、クラウドの Web 画面で開くリンクのテスト。"""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

import pytest

from tag_keeper.cloudlinks import OneDriveLinks
from tag_keeper.officeview import OfficeError, preview

S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
REL = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'


def make_zip(path: Path, parts: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, xml in parts.items():
            z.writestr(name, xml)
    return path


def test_xlsx(tmp_path: Path) -> None:
    f = make_zip(
        tmp_path / "a.xlsx",
        {
            "xl/workbook.xml": f'<workbook {S}><sheets><sheet name="家計" sheetId="1" r:id="rId1"/><sheet name="メモ" sheetId="2" r:id="rId2"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": f'<Relationships {REL}><Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="/xl/worksheets/sheet2.xml"/></Relationships>',
            "xl/sharedStrings.xml": f"<sst {S}><si><t>日付</t></si><si><r><t>金</t></r><r><t>額</t></r></si></sst>",
            "xl/styles.xml": f'<styleSheet {S}><cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>',
            "xl/worksheets/sheet1.xml": f'<worksheet {S}><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" s="1"><v>45383</v></c><c r="C2"><v>1000</v></c></row></sheetData></worksheet>',
            "xl/worksheets/sheet2.xml": f'<worksheet {S}><sheetData><row r="1"><c r="B1" t="inlineStr"><is><t>メモ書き</t></is></c><c r="C1" t="b"><v>1</v></c></row></sheetData></worksheet>',
        },
    )
    d = preview(f)
    assert [s["name"] for s in d["sheets"]] == ["家計", "メモ"]
    assert d["sheets"][0]["rows"] == [["日付", "", "金額"], ["2024-04-01", "", "1000"]]  # 日付の書式なら日付にする
    assert d["sheets"][1]["rows"] == [["", "メモ書き", "TRUE"]]


def test_docx(tmp_path: Path) -> None:
    f = make_zip(
        tmp_path / "a.docx",
        {
            "word/styles.xml": f'<w:styles {W}><w:style w:styleId="1"><w:name w:val="heading 1"/></w:style></w:styles>',
            "word/document.xml": f'<w:document {W}><w:body>'
            '<w:p><w:pPr><w:pStyle w:val="1"/></w:pPr><w:r><w:t>見出し</w:t></w:r></w:p>'
            '<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>太字</w:t></w:r><w:r><w:t>と普通</w:t></w:r></w:p>'
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
            "</w:body></w:document>",
        },
    )
    blocks = preview(f)["blocks"]
    assert blocks[0] == {"type": "p", "level": 1, "runs": [{"t": "見出し", "b": False, "i": False}]}
    assert blocks[1]["runs"] == [{"t": "太字", "b": True, "i": False}, {"t": "と普通", "b": False, "i": False}]
    assert blocks[2] == {"type": "table", "rows": [["A", "B"]]}


def test_pptx_follows_slide_order(tmp_path: Path) -> None:
    slide = lambda title, body: (  # noqa: E731
        f'<p:sld {P}><p:cSld><p:spTree>'
        f'<p:sp><p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>{title}</a:t></a:r></a:p></p:txBody></p:sp>'
        f"<p:sp><p:txBody><a:p><a:r><a:t>{body}</a:t></a:r></a:p></p:txBody></p:sp>"
        "</p:spTree></p:cSld></p:sld>"
    )
    f = make_zip(
        tmp_path / "a.pptx",
        {
            "ppt/presentation.xml": f'<p:presentation {P}><p:sldIdLst><p:sldId r:id="rId2"/><p:sldId r:id="rId1"/></p:sldIdLst></p:presentation>',
            "ppt/_rels/presentation.xml.rels": f'<Relationships {REL}><Relationship Id="rId1" Target="slides/slide1.xml"/><Relationship Id="rId2" Target="slides/slide2.xml"/></Relationships>',
            "ppt/slides/slide1.xml": slide("二枚目", "本文2"),
            "ppt/slides/slide2.xml": slide("一枚目", "本文1"),
        },
    )
    assert preview(f)["slides"] == [
        {"title": "一枚目", "paragraphs": ["本文1"], "images": []},
        {"title": "二枚目", "paragraphs": ["本文2"], "images": []},
    ]


def test_untrusted_xml_is_refused(tmp_path: Path) -> None:
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><w:document xmlns:w="x">&a;</w:document>'
    with pytest.raises(OfficeError, match="DTD"):
        preview(make_zip(tmp_path / "b.docx", {"word/document.xml": bomb}))
    (tmp_path / "c.docx").write_bytes(b"not a zip")
    with pytest.raises(OfficeError):
        preview(tmp_path / "c.docx")
    with pytest.raises(OfficeError):
        preview(tmp_path / "d.pdf")


def test_onedrive_links(tmp_path: Path) -> None:
    """onedrive クライアントのデータベースをたどって、パスに当たるアイテムのリンクを作る。"""
    conf = tmp_path / "onedrive"
    conf.mkdir()
    conn = sqlite3.connect(conf / "items.sqlite3")
    conn.execute("CREATE TABLE item (driveId TEXT, id TEXT, name TEXT, type TEXT, parentId TEXT)")
    conn.executemany(
        "INSERT INTO item VALUES (?, ?, ?, ?, ?)",
        [
            ("abc", "ABC!root", "root", "root", None),
            ("abc", "ABC!1", "書類", "dir", "ABC!root"),
            ("abc", "ABC!2", "見積書.xlsx", "file", "ABC!1"),
        ],
    )
    conn.commit()
    conn.close()
    sync = tmp_path / "OneDrive"
    (sync / "書類").mkdir(parents=True)
    (sync / "書類" / "見積書.xlsx").write_text("x")
    links = OneDriveLinks(conf, sync)

    file_links = links.links(sync / "書類" / "見積書.xlsx", False)
    assert [l["kind"] for l in file_links] == ["onedrive", "office"]
    assert file_links[0]["url"] == "https://onedrive.live.com/?cid=abc&id=ABC!2"
    assert file_links[1]["url"] == "https://onedrive.live.com/edit.aspx?cid=abc&resid=ABC!2"
    # 大文字・小文字の違いは同じものとみなす（OneDrive は区別しない）
    assert links.links(sync / "書類" / "見積書.XLSX", False)[0]["url"].endswith("ABC!2")
    assert [l["kind"] for l in links.links(sync / "書類", True)] == ["onedrive"]
    assert links.links(sync / "無い.pdf", False) == []
    assert links.links(tmp_path / "外.pdf", False) == []


def test_embedded_images(tmp_path: Path) -> None:
    from tag_keeper.officeview import image_part

    f = make_zip(
        tmp_path / "shot.docx",
        {
            "word/document.xml": f'<w:document {W} xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<w:body><w:p><w:r><w:drawing><a:blip r:embed="rId5"/></w:drawing></w:r></w:p><w:p><w:r><w:drawing><a:blip r:embed="rId6"/></w:drawing></w:r></w:p></w:body></w:document>',
            "word/_rels/document.xml.rels": f'<Relationships {REL}><Relationship Id="rId5" Target="media/image1.png"/><Relationship Id="rId6" Target="media/image2.emf"/></Relationships>',
            "word/media/image1.png": "PNGDATA",
            "word/media/image2.emf": "EMF",
        },
    )
    blocks = preview(f)["blocks"]
    assert [b for b in blocks if b["type"] == "image"] == [{"type": "image", "part": "word/media/image1.png"}]  # EMF は出さない
    assert image_part(f, "word/media/image1.png") == (b"PNGDATA", "image/png")
    with pytest.raises(OfficeError):
        image_part(f, "word/media/image2.emf")
    with pytest.raises(OfficeError):
        image_part(f, "word/document.xml")
