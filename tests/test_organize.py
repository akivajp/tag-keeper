"""受け皿の整理の提案（受け皿の検出・移動先の絞り込み・命名・保存）と、内容の抽出のテスト。

ollama は呼ばず、問い合わせの関数を差し替える。
"""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path
from typing import Any

import pytest

from conftest import write
from tag_keeper import organize
from tag_keeper.config import OrganizeConfig, SafetyConfig
from tag_keeper.extract import extract
from tag_keeper.scan import scan_root

SAFETY = SafetyConfig()


@pytest.fixture
def tree(root: Path) -> Path:
    write(root / "99_Inbox" / "tmp" / "downloadfile.pdf", "x")
    write(root / "99_Inbox" / "未整理のドキュメント" / "解約精算書.txt", "解約精算書 2025年9月24日 ライオンズガーデン尾山台")
    write(root / "99_Inbox" / "tmp" / "Thumbs.db", "junk")
    write(root / "02_財務・契約" / "銀行" / "Revolut" / "20250301_Revolut_明細書.pdf", "a")
    write(root / "02_財務・契約" / "銀行" / "Revolut" / "20250401_Revolut_明細書.pdf", "b")
    write(root / "03_リソース" / "物件関係" / "ライオンズガーデン尾山台" / "20240101_賃貸契約書.pdf", "c")
    write(root / "03_リソース" / "領収書" / "2025年" / "2025年04月" / "20250401_領収書.pdf", "d")
    write(root / "ドキュメント" / "My Games" / "tmp" / "save.dat", "game")
    return root


def test_is_inbox_name() -> None:
    patterns = OrganizeConfig().inbox_patterns
    for name in ["tmp", "TMP", "99_Inbox", "未整理のドキュメント", "__家計_未整理"]:
        assert organize.is_inbox_name(name, patterns)
    for name in ["temporary_report", "整理済み", "Documents"]:
        assert not organize.is_inbox_name(name, patterns)


def test_find_inbox(conn: sqlite3.Connection, root_id: int, tree: Path) -> None:
    scan_root(conn, root_id, tree, SAFETY)
    inbox, files = organize.find_inbox(conn, root_id, OrganizeConfig().inbox_patterns, excluded=["ドキュメント/My Games"])
    # 受け皿の中の受け皿は上のものにまとめ、アプリのデータの中の tmp は外す
    assert inbox == ["99_Inbox"]
    assert {f.relpath for f in files} == {"99_Inbox/tmp/downloadfile.pdf", "99_Inbox/未整理のドキュメント/解約精算書.txt"}


def test_folder_index_ranks_related_folders(conn: sqlite3.Connection, root_id: int, tree: Path) -> None:
    scan_root(conn, root_id, tree, SAFETY)
    index = organize.build_folder_index(conn, root_id, ["99_Inbox"])
    assert "99_Inbox/tmp" not in {f.relpath for f in index.folders}
    top = index.search("Revolut JPY 明細書", 2)
    assert top[0][0].relpath == "02_財務・契約/銀行/Revolut"
    top = index.search("ライオンズガーデン尾山台 解約精算書", 1)
    assert top[0][0].relpath == "03_リソース/物件関係/ライオンズガーデン尾山台"


def test_naming_style_and_clean_name() -> None:
    assert organize.naming_style(["20250301_a.pdf", "20250401_b.pdf", "memo.txt"]).startswith("YYYYMMDD_（2/3")
    assert organize.naming_style(["2025-03-01 a.pdf"]).startswith("YYYY-MM-DD_")
    assert organize.clean_name("20250414_明細書.PDF", "downloadfile.PDF") == "20250414_明細書.pdf"
    assert organize.clean_name('a/b:c?"d', "x.jpg") == "a_b_c__d.jpg"
    assert organize.clean_name("  ", "orig.txt") == "orig.txt"


def test_adjust_dated_folder() -> None:
    known = {"r/2025年", "r/2025年/2025年04月", "p/2025"}
    assert organize.adjust_dated_folder("r/2022年/2022年08月", "20250414", known)[0] == "r/2025年/2025年04月"
    assert organize.adjust_dated_folder("r/2022年/2022年08月", "20250514", known) == ("r/2025年/2025年05月", "書類の日付の年・月のフォルダを新しく作る")
    assert organize.adjust_dated_folder("p/2021", "20250101", known)[0] == "p/2025"
    # 日付の後に題名が続くフォルダ（案件名など）は変えない
    assert organize.adjust_dated_folder("q/202108_報告", "20250414", known) == ("q/202108_報告", "")
    assert organize.adjust_dated_folder("r/2022年", None, known) == ("r/2022年", "")


def fake_ollama(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    """ollama の代わりに、決まった答えを返す。"""

    def generate(config: OrganizeConfig, prompt: str, image: bytes | None = None, **_: Any) -> dict[str, Any]:
        calls.append(prompt)
        if "内容を読み取ってください" in prompt:
            return {"title": "解約精算書", "doc_type": "精算書", "date": "20250924", "parties": ["ライオンズガーデン尾山台"], "keywords": ["解約"], "summary": "解約の精算"}
        # 候補の一覧から、ライオンズガーデン尾山台の番号を選ぶ
        line = next(l for l in prompt.splitlines() if "ライオンズガーデン尾山台 ／" in l)
        return {
            "date": "20250924",
            "date_source": "content",
            "title": "解約精算書",
            "new_name": "20250924_ライオンズガーデン尾山台_解約精算書",
            "summary": "解約の精算",
            "doc_type": "精算書",
            "destinations": [{"index": int(line.split(":")[0]), "reason": "物件のフォルダ"}, {"index": 999, "reason": "無い番号"}],
            "tags": ["種別:精算書"],
        }

    monkeypatch.setattr(organize, "ollama_generate", generate)
    monkeypatch.setattr(organize, "check_ollama", lambda config: None)


def test_run_suggestions_and_cache(conn: sqlite3.Connection, root_id: int, tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scan_root(conn, root_id, tree, SAFETY)
    calls: list[str] = []
    fake_ollama(monkeypatch, calls)
    only = ["99_Inbox/未整理のドキュメント/解約精算書.txt"]
    [sug] = organize.run_suggestions(conn, root_id, tree, OrganizeConfig(), [], only=only)
    assert sug.new_name == "20250924_ライオンズガーデン尾山台_解約精算書.txt"
    assert [d["relpath"] for d in sug.destinations] == ["03_リソース/物件関係/ライオンズガーデン尾山台"]  # 無い番号は捨てる
    assert len(calls) == 2  # 内容の読み取りと、名前・移動先の2回
    # 2回目は保存した提案を使い、モデルに問い合わせない
    [again] = organize.run_suggestions(conn, root_id, tree, OrganizeConfig(), [], only=only)
    assert again.cached and len(calls) == 2
    # ハッシュをその場で計算したものはカタログにも残るので、画面の一覧から保存済みの提案を引ける
    _inbox, files = organize.find_inbox(conn, root_id, OrganizeConfig().inbox_patterns)
    assert only[0] in organize.cached_suggestions(conn, files, OrganizeConfig().model)


def test_ollama_failure_is_reported_per_file(conn: sqlite3.Connection, root_id: int, tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scan_root(conn, root_id, tree, SAFETY)

    def broken(*_: Any, **__: Any) -> dict[str, Any]:
        raise organize.OllamaError("つながらない")

    monkeypatch.setattr(organize, "ollama_generate", broken)
    monkeypatch.setattr(organize, "check_ollama", lambda config: None)
    out = organize.run_suggestions(conn, root_id, tree, OrganizeConfig(), [])
    assert out and all(s.error == "つながらない" for s in out)
    # 失敗したものは保存しない（次回にやり直す）
    assert conn.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0] == 0


def test_extract_office_and_text(tmp_path: Path) -> None:
    docx = tmp_path / "a.docx"
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:p><w:t>請求書</w:t></w:p><w:p><w:t>株式会社テスト</w:t></w:p></w:document>")
    ex = extract(docx)
    assert ex.method == "office" and ex.text.split() == ["請求書", "株式会社テスト"]

    sjis = tmp_path / "b.csv"
    sjis.write_bytes("日付,金額\n2025/04/01,1000".encode("cp932"))
    assert extract(sjis).text.startswith("日付,金額")

    img = write(tmp_path / "c.jpg", b"\xff\xd8fake")
    assert extract(img).method == "image" and extract(img, use_images=False).method == "none"
    assert extract(write(tmp_path / "d.bin", b"\x00")).method == "none"
