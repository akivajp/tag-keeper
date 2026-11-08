"""ディレクトリの型（推定・継承）・カード・複製と入れ子のルートの除外のテスト。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from conftest import live_paths, write
from tag_keeper import dirtypes
from tag_keeper.config import Config, RootConfig, SafetyConfig
from tag_keeper.scan import scan_root
from tag_keeper.tags import TagStore

SAFETY = SafetyConfig(mass_missing_ratio=0.2, mass_missing_min=3)


def test_types_are_inferred_from_contents(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    write(root / "code" / "proj" / ".git" / "HEAD", "ref")
    write(root / "code" / "proj" / "src" / "main.py", "print()")
    write(root / "notes" / ".obsidian" / "app.json", "{}")
    for d in ("2024", "2025", "2026"):
        write(root / "photos" / d / "a.jpg", d)
    for d in ("01_仕事", "02_家", "03_趣味"):
        write(root / "areas" / d / "x.txt", d)
    write(root / "未整理" / "a.pdf", "a")
    write(root / "plain" / "a.txt", "a")
    scan_root(conn, root_id, root, SAFETY)

    def t(path: str) -> tuple[str, str]:
        d = dirtypes.effective_type(conn, root_id, path, {}, ["*未整理*"])
        return d.type, d.how

    assert t("code/proj") == ("bundle", "inferred")
    assert t("notes") == ("app", "inferred")
    assert t("photos") == ("dated", "inferred")
    assert t("areas") == ("numbered", "inferred")
    assert t("未整理") == ("inbox", "inferred")
    assert t("plain") == ("normal", "none")
    # まとまり・アプリ管理の中は移動先にしない（外側だけ）
    assert dirtypes.closed_dirs(conn, root_id, {}) == ["notes", "code/proj"]
    # 「通常」を付けると目印があっても推定しない
    assert dirtypes.closed_dirs(conn, root_id, {"code/proj": "normal"}) == ["notes"]


def test_set_types_are_inherited_and_can_be_stopped() -> None:
    types = {"archive": "archive", "archive/2019": "dated", "archive/keep": "normal", "x": "dated"}
    assert dirtypes.explicit_type("archive/2019/01", types) == dirtypes.DirType("archive", "inherited", "archive")
    assert dirtypes.explicit_type("archive/2019", types) == dirtypes.DirType("dated", "set")
    assert dirtypes.explicit_type("archive/keep/a", types) is None  # 「通常」で止まる
    assert dirtypes.explicit_type("x/y", types) is None  # 日付分割は継承しない


def test_scan_skips_excluded_types_replicas_and_nested_roots(tmp_path: Path, conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    write(root / "docs" / "a.pdf", "a")
    write(root / "cache" / "big.bin", "b")
    write(root / "OneDrive" / "mirror.pdf", "m")  # 別のルートの複製
    write(root / "Nested" / "n.txt", "n")  # 別のルートとして設定したフォルダ
    rc = RootConfig(name="test", path=root, replicas={"OneDrive": "onedrive"})
    config = Config(roots=[rc, RootConfig(name="nested", path=root / "Nested")])
    skip = dirtypes.scan_exclusions(config, rc, {"cache": "exclude"})
    assert skip == {"cache": "除外の型", "OneDrive": "onedrive の複製", "Nested": "ルート nested"}
    scan_root(conn, root_id, root, SAFETY, exclude_paths=skip)
    assert live_paths(conn, root_id) == {"docs/a.pdf"}


def test_dir_types_and_cards_follow_moves_and_round_trip(tmp_path: Path) -> None:
    store = TagStore(tmp_path / "tags", host="a")
    assert store.set_dir_type("r", "経理", "archive")
    assert not store.set_dir_type("r", "経理", "archive")
    assert store.set_dir_card("r", "経理/請求書", "取引先ごとの請求書", "YYYYMMDD_相手_件名")
    store.follow_moves("r", [("経理", "04_アーカイブ/経理")])
    assert store.dir_types_of("r") == {"04_アーカイブ/経理": "archive"}
    assert store.dir_cards_of("r")["04_アーカイブ/経理/請求書"]["naming"] == "YYYYMMDD_相手_件名"
    again = TagStore(tmp_path / "other", host="b")
    again.import_events(store.export_events())
    assert again.dir_types_of("r") == {"04_アーカイブ/経理": "archive"}
    # 隔離で外し、取り消しで戻す
    meta = store.detach_dirmeta_under("r", "04_アーカイブ")
    assert store.dir_types_of("r") == {} and store.dir_cards_of("r") == {}
    assert store.attach_dirmeta("r", meta) == 2
    assert store.set_dir_type("r", "04_アーカイブ/経理", "")  # 外す
    assert store.dir_types_of("r") == {}
    assert store.set_dir_card("r", "04_アーカイブ/経理/請求書", "", "")
    assert store.dir_cards_of("r") == {}


def test_inbox_type_and_cards_feed_suggestions(conn: sqlite3.Connection, root_id: int, root: Path) -> None:
    """受け皿の型のフォルダは名前に関係なく受け皿になり、カードは移動先の候補（中が空でも）に添えられる。"""
    from tag_keeper import organize

    write(root / "scans" / "doc.pdf", "x")
    write(root / "経理" / "請求書" / "20250101_a.pdf", "y")
    (root / "経理" / "領収書").mkdir()
    scan_root(conn, root_id, root, SAFETY)
    inbox, files = organize.find_inbox(conn, root_id, ["*未整理*"], [], ["scans"])
    assert inbox == ["scans"] and [f.relpath for f in files] == ["scans/doc.pdf"]
    cards = {"経理/領収書": {"description": "レシートと領収書", "naming": "YYYYMMDD_店"}}
    index = organize.build_folder_index(conn, root_id, inbox, [], cards)
    found = index.find("経理/領収書")
    assert found is not None and "レシートと領収書" in found.card
    assert index.search("領収書 レシート", 3)[0][0].relpath == "経理/領収書"
