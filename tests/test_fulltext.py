"""全文検索（本文の抽出・索引・検索）のテスト。"""

from __future__ import annotations

import importlib.util
import shutil
import sqlite3
import struct
import subprocess
import zipfile
from pathlib import Path

import pytest
from conftest import write

from tag_keeper import extract, fulltext
from tag_keeper.catalog import SCHEMA_VERSION, connect, ensure_root, fts_enabled
from tag_keeper.config import Config, RootConfig, SafetyConfig
from tag_keeper.hashing import hash_root
from tag_keeper.scan import scan_root

SAFETY = SafetyConfig(mass_missing_ratio=0.2, mass_missing_min=3)
HAS_PDFTOTEXT = shutil.which("pdftotext") is not None


def text_pdf(lines: list[str]) -> bytes:
    """文字だけの1ページの PDF（README の見本のデータを作る scripts/make_demo.py の部品を使う）。"""
    spec = importlib.util.spec_from_file_location("make_demo", Path(__file__).resolve().parent.parent / "scripts" / "make_demo.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.pdf(lines)


def docx(paragraphs: list[str]) -> bytes:
    """本文だけの最小限の docx。"""
    import io

    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document xmlns:w="w"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def image_pdf(jpeg: bytes) -> bytes:
    """JPEG を1枚だけ貼った、文字の無い PDF（スキャンした書類の代わり）。"""
    i = 2
    while i < len(jpeg):
        marker, length = jpeg[i + 1], struct.unpack(">H", jpeg[i + 2 : i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", jpeg[i + 5 : i + 9])
            comps = jpeg[i + 9]
            break
        i += 2 + length
    else:
        raise ValueError("JPEG の大きさを読めません")
    space = "/DeviceGray" if comps == 1 else "/DeviceRGB"
    draw = f"q {w} 0 0 {h} 0 0 cm /Im0 Do Q".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>".encode(),
        f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace {space} /BitsPerComponent 8"
        f" /Filter /DCTDecode /Length {len(jpeg)} >>\nstream\n".encode() + jpeg + b"\nendstream",
        f"<< /Length {len(draw)} >>\nstream\n".encode() + draw + b"\nendstream",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for n, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def make_config(root: Path, tmp_path: Path) -> Config:
    cfg = Config(roots=[RootConfig(name="test", path=root)])
    cfg.tags.dir = tmp_path / "tags"
    return cfg


def refresh(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    scan_root(conn, root_id, root, SAFETY)
    hash_root(conn, root_id, root)


# ---------- カタログの移行 ----------


def test_catalog_v2_is_migrated(tmp_path: Path) -> None:
    """版 2 のカタログは、行を保ったまま今の版になる（gone_by を足し、extracts を作り直して索引に入れ、analyses を足す）。"""
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.executescript(
        """
        CREATE TABLE roots (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, path TEXT NOT NULL);
        CREATE TABLE entries (
            id INTEGER PRIMARY KEY, root_id INTEGER NOT NULL REFERENCES roots(id), relpath TEXT NOT NULL,
            is_dir INTEGER NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, inode INTEGER NOT NULL,
            sha256 TEXT, hashed_size INTEGER, hashed_mtime_ns INTEGER, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            missing_count INTEGER NOT NULL DEFAULT 0, gone_at TEXT);
        CREATE TABLE extracts (sha256 TEXT PRIMARY KEY, method TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL);
        INSERT INTO roots VALUES (1, 'r', '/x');
        INSERT INTO entries VALUES (1, 1, 'a.pdf', 0, 3, 1, 1, 'abc', 3, 1, 't', 't', 0, NULL);
        INSERT INTO extracts VALUES ('abc', 'pdftotext', '請求書の本文', 't');
        PRAGMA user_version = 2;
        """
    )
    old.close()

    conn = connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'analyses'").fetchone() is not None
    assert "gone_by" in {r[1] for r in conn.execute("PRAGMA table_info(entries)")}
    assert conn.execute("SELECT relpath FROM entries").fetchone()[0] == "a.pdf"
    row = conn.execute("SELECT id, sha256, method, text, note FROM extracts").fetchone()
    assert tuple(row)[1:] == ("abc", "pdftotext", "請求書の本文", "")
    if fts_enabled(conn):
        hits = conn.execute("SELECT rowid FROM extracts_fts WHERE extracts_fts MATCH '\"請求書\"'").fetchall()
        assert [h[0] for h in hits] == [row["id"]]
    conn.close()
    # 2回目からは何も変えない
    assert connect(db).execute("SELECT COUNT(*) FROM extracts").fetchone()[0] == 1


# ---------- 抽出 ----------


def test_extract_text_of_office_and_text_files(tmp_path: Path) -> None:
    d = write(tmp_path / "a.docx", docx(["見積書 &amp; 契約書", "ＡＢＣ商事 御中"]))
    ex = extract.extract_text(d)
    # 文字参照は戻し、全角の英字は半角にそろえる（NFKC）
    assert ex.method == "office" and "見積書 & 契約書" in ex.text and "ABC商事 御中" in ex.text

    t = write(tmp_path / "memo.txt", "税金の書類\n".encode("cp932"))
    assert extract.extract_text(t).text == "税金の書類"
    assert extract.extract_text(write(tmp_path / "x.bin", b"\0\1")).method == "none"
    assert extract.extract_text(write(tmp_path / "big.txt", "x" * 100), max_chars=10).text == "x" * 10


def test_ocr_text_has_no_gaps_between_japanese_letters() -> None:
    """OCR が和文の文字の間に入れる空白は詰める（「請 求 書」でなく「請求書」で探せるように）。英単語の間は残す。"""
    assert extract.normalize_text("請 求 書  ABC Corp  ２０２６年", 100, join_cjk=True) == "請求書 ABC Corp 2026年"
    assert extract.normalize_text("株式会社 御中", 100) == "株式会社 御中"  # 書き手が入れた空白は残す


@pytest.mark.skipif(not HAS_PDFTOTEXT, reason="pdftotext が無い")
def test_extract_text_of_pdf_and_scans(tmp_path: Path) -> None:
    p = write(tmp_path / "invoice.pdf", text_pdf(["Invoice for ATID", "Total 12345 JPY"]))
    ex = extract.extract_text(p)
    assert ex.method == "pdftotext" and "Invoice for ATID" in ex.text

    blank = write(tmp_path / "scan.pdf", text_pdf([""]))
    # OCR を使えないときは、後で読み直せるよう「scan」として返す
    assert extract.extract_text(blank, ocr_lang=None).method == "scan"


@pytest.mark.skipif(
    not (HAS_PDFTOTEXT and extract.ocr_languages("eng")), reason="tesseract（英語）か poppler が無い"
)
def test_scanned_pdf_is_read_by_ocr(tmp_path: Path) -> None:
    src = write(tmp_path / "src.pdf", text_pdf(["INVOICE NUMBER 4821", "TOTAL AMOUNT DUE"]))
    subprocess.run(["pdftoppm", "-r", "200", "-gray", "-jpeg", "-singlefile", str(src), str(tmp_path / "page")], check=True)
    scan = write(tmp_path / "scan.pdf", image_pdf((tmp_path / "page.jpg").read_bytes()))
    assert extract.extract_text(scan, ocr_lang=None).method == "scan"  # 文字の無い PDF
    ex = extract.extract_text(scan, ocr_lang="eng", ocr_pages=1)
    assert ex.method == "ocr" and "INVOICE" in ex.text.upper()


# ---------- 抽出待ち・保存・検索 ----------


@pytest.fixture
def indexed(conn: sqlite3.Connection, root_id: int, root: Path, tmp_path: Path) -> Config:
    """本文を抽出して索引に入れた、小さなルート。"""
    write(root / "書類" / "見積書.docx", docx(["株式会社アティード 御見積書", "合計 12,345円"]))
    write(root / "書類" / "memo.txt", "税金の書類を出す")
    write(root / "notes" / "readme.md", "100% done_ok")
    write(root / "proj" / "node_modules" / "pkg" / "README.md", "株式会社アティード")  # 作り直せる生成物の中は入れない
    write(root / "video.mp4", b"\0\0\0 ftyp")  # 本文を読まない種類
    refresh(conn, root_id, root)
    cfg = make_config(root, tmp_path)
    res = fulltext.run_pending(conn, root_id, root, cfg)
    assert (res.done, res.errors) == (3, 0)
    return cfg


def test_pending_prefers_documents_and_skips_generated_folders(
    conn: sqlite3.Connection, root_id: int, root: Path, tmp_path: Path
) -> None:
    write(root / "a.txt", "text")
    write(root / "b.docx", docx(["office"]))
    write(root / ".venv" / "lib" / "x.py", "print()")
    write(root / "dup" / "a-copy.txt", "text")  # 同じ内容は1件にまとめる
    refresh(conn, root_id, root)
    cfg = make_config(root, tmp_path).extract
    items = fulltext.pending(conn, root_id, cfg, ocr_ready=False)
    assert items[0].relpath.rsplit("/", 1)[-1] == "b.docx"  # 書類が先
    assert len(items) == 2 and not any(".venv" in i.relpath for i in items)


def test_search_by_text_name_and_kind(conn: sqlite3.Connection, root_id: int, indexed: Config) -> None:
    def find(**kw) -> list[str]:
        return [r["relpath"] for r in fulltext.find_entries(conn, root_id, **kw)]

    assert find(text="アティード") == ["書類/見積書.docx"]  # 生成物のフォルダの中の README は出ない
    assert find(text="ｱﾃｨｰﾄﾞ 御見積") == ["書類/見積書.docx"]  # 半角カナも NFKC でそろえて探す・複数の語はすべて含む
    assert find(text="税金") == ["書類/memo.txt"]  # 2文字の語は索引を使わずに探す
    assert find(text="100%") == ["notes/readme.md"]  # LIKE の特殊文字は文字どおり
    assert find(text="done_ok") == ["notes/readme.md"]
    assert find(text="存在しない語") == []
    assert find(words=["MEMO"]) == ["書類/memo.txt"]  # 名前は大文字・小文字を区別しない
    assert find(words=["書類"], files_only=False) == ["書類", "書類/memo.txt", "書類/見積書.docx"]
    assert find(kind="office") == ["書類/見積書.docx"]
    assert find(kind="video") == ["video.mp4"]

    rows = fulltext.find_entries(conn, root_id, text="12,345円")
    snip = fulltext.snippets(conn, [rows[0]["sha256"]], fulltext.query_terms("12,345円"))
    assert "合計 12,345円" in snip[rows[0]["sha256"]]


def test_search_without_the_fts_index_gives_the_same_results(
    conn: sqlite3.Connection, root_id: int, indexed: Config
) -> None:
    """FTS5 の無い SQLite でも（索引を外しても）、LIKE で同じ結果になる。"""
    for name in ("extracts_fts_ai", "extracts_fts_ad", "extracts_fts_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.commit()
    assert not fts_enabled(conn)
    assert [r["relpath"] for r in fulltext.find_entries(conn, root_id, text="アティード")] == ["書類/見積書.docx"]


def test_changed_content_is_extracted_again(conn: sqlite3.Connection, root_id: int, root: Path, indexed: Config) -> None:
    write(root / "書類" / "memo.txt", "住民税の通知")
    refresh(conn, root_id, root)
    assert [i.relpath for i in fulltext.pending(conn, root_id, indexed.extract, ocr_ready=False)] == ["書類/memo.txt"]
    fulltext.run_pending(conn, root_id, root, indexed)
    assert [r["relpath"] for r in fulltext.find_entries(conn, root_id, text="住民税")] == ["書類/memo.txt"]


def test_scans_wait_until_ocr_is_ready(conn: sqlite3.Connection, root_id: int, root: Path, tmp_path: Path) -> None:
    write(root / "scan.pdf", b"%PDF-1.4 broken")
    refresh(conn, root_id, root)
    cfg = make_config(root, tmp_path)
    cfg.extract.ocr = False
    fulltext.run_pending(conn, root_id, root, cfg)
    if not HAS_PDFTOTEXT:
        return
    assert conn.execute("SELECT method FROM extracts").fetchone()[0] == "scan"
    assert fulltext.pending(conn, root_id, cfg.extract, ocr_ready=False) == []
    assert len(fulltext.pending(conn, root_id, cfg.extract, ocr_ready=True)) == 1


def test_worker_extracts_in_the_background(root: Path, tmp_path: Path) -> None:
    db = tmp_path / "catalog.db"
    conn = connect(db)
    root_id = ensure_root(conn, "test", root)
    write(root / "a.txt", "裏で抽出する本文")
    refresh(conn, root_id, root)
    cfg = make_config(root, tmp_path)
    worker = fulltext.ExtractWorker(db, lambda: cfg)
    assert worker.run_once() is True
    assert worker.run_once() is False  # もう抽出するものは無い
    assert [r["relpath"] for r in fulltext.find_entries(conn, root_id, text="抽出する")] == ["a.txt"]
    cfg.extract.enabled = False
    assert worker.run_once() is False


# ---------- 検索の API ----------


def test_search_api(tmp_path: Path, root: Path) -> None:
    from webtest import TestApp

    from tag_keeper.jobs import JobManager
    from tag_keeper.web.server import Settings, create_app

    write(root / "契約" / "見積書.docx", docx(["株式会社アティード 御見積書"]))
    write(root / "契約" / "請求書.txt", "請求書 アティード")
    write(root / "写真.jpg", b"\xff\xd8\xff")
    cfg = write(tmp_path / "config.toml", f'[[roots]]\nname = "data"\npath = "{root}"\n[tags]\ndir = "{tmp_path / "tags"}"\n')
    settings = Settings(cfg, tmp_path / "data" / "catalog.db")
    jobs = JobManager(tmp_path / "logs")
    app = TestApp(create_app(settings, jobs=jobs), extra_environ={"HTTP_HOST": "127.0.0.1:8090"})
    origin = {"Origin": "http://127.0.0.1:8090"}
    app.post_json("/api/roots/data/refresh", {}, headers=origin)
    jobs.wait()
    conn = connect(settings.db_path)
    fulltext.run_pending(conn, 1, root, settings.config())
    conn.close()

    res = app.get("/api/search", {"text": "アティード"}).json
    assert {x["path"] for x in res["items"]} == {"契約/見積書.docx", "契約/請求書.txt"}
    assert all("アティード" in x["snippet"] for x in res["items"])
    assert [x["path"] for x in app.get("/api/search", {"text": "アティード", "kind": "office"}).json["items"]] == ["契約/見積書.docx"]
    assert [x["path"] for x in app.get("/api/search", {"q": "写真"}).json["items"]] == ["写真.jpg"]
    # タグとも組み合わせられる（フォルダのタグは配下に効く）
    app.post_json("/api/tags", {"root": "data", "paths": ["契約"], "tag": "種類:契約", "op": "add"}, headers=origin)
    res = app.get("/api/search", {"text": "請求書", "tag": "種類:契約"}).json
    assert [x["path"] for x in res["items"]] == ["契約/請求書.txt"]
    assert res["items"][0]["tags"]["inherited"][0]["tag"] == "種類:契約"
    # 条件が無い・種類が不正なら 400
    app.get("/api/search", status=400)
    app.get("/api/search", {"kind": "nothing"}, status=400)

    status = app.get("/api/extract/status").json
    assert status["indexed"] == 2 and status["pending"] == 0 and status["enabled"] is True


def test_cli_extract_and_search(tmp_path: Path, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tag_keeper.cli import main

    write(root / "契約" / "見積書.docx", docx(["株式会社アティード 御見積書"]))
    write(root / "memo.txt", "税金の書類")
    cfg = write(tmp_path / "config.toml", f'[[roots]]\nname = "data"\npath = "{root}"\n[tags]\ndir = "{tmp_path / "tags"}"\n')
    db = tmp_path / "catalog.db"
    base = ["--config", str(cfg), "--db", str(db)]
    assert main([*base, "scan"]) == 0 and main([*base, "hash"]) == 0
    assert main([*base, "extract", "--no-ocr"]) == 0
    capsys.readouterr()
    assert main([*base, "search", "--text", "アティード"]) == 0
    out = capsys.readouterr().out
    assert "契約/見積書.docx" in out and "memo.txt" not in out
    assert main([*base, "search", "memo", "--kind", "text"]) == 0
    assert "memo.txt" in capsys.readouterr().out
    assert main([*base, "search"]) == 2  # 条件が無い


def test_catalog_works_without_the_fts_index(tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """FTS5 の trigram を作れない SQLite でも、カタログは開けて、本文の検索も（索引なしで）できる。警告は1回だけ。"""
    import tag_keeper.catalog as catalog

    broken = ("CREATE VIRTUAL TABLE extracts_fts USING fts5(text, tokenize='no_such_tokenizer')", *catalog.FTS_SCHEMA[1:])
    monkeypatch.setattr(catalog, "FTS_SCHEMA", broken)
    db = tmp_path / "nofts.db"
    conn = connect(db)
    assert not fts_enabled(conn)
    root_id = ensure_root(conn, "test", root)
    write(root / "a.txt", "索引なしでも探せる本文")
    refresh(conn, root_id, root)
    fulltext.run_pending(conn, root_id, root, make_config(root, tmp_path))
    assert [r["relpath"] for r in fulltext.find_entries(conn, root_id, text="探せる本文")] == ["a.txt"]
    caplog.clear()
    connect(db).close()
    assert "作れません" not in caplog.text  # 接続のたびに警告を出し続けない
