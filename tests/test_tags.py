"""タグ（追記専用ログ・フォルダからの継承・移動への追従）のテスト。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tag_keeper.tags import TagError, TagStore, normalize_tag


def test_normalize_tag() -> None:
    assert normalize_tag("  会社 :  アティード ") == "会社:アティード"
    assert normalize_tag("a\n b") == "a b"  # 改行を含む空白は1つにまとめる
    for bad in ["", "  ", ":x", "x:", "a\x07b", "x" * 101]:
        with pytest.raises(TagError):
            normalize_tag(bad)


def test_add_remove_and_inherit(tmp_path: Path) -> None:
    store = TagStore(tmp_path / "tags", host="a")
    assert store.add("r", ["docs", "docs/x.pdf"], "種別:請求書") == 2
    assert store.add("r", ["docs/x.pdf"], "種別:請求書") == 0  # 付いているものは飛ばす
    store.add("r", ["docs"], "会社:アティード")

    tags = store.tags_of("r", "docs/sub/y.pdf")
    assert tags["direct"] == []
    assert {t["tag"] for t in tags["inherited"]} == {"種別:請求書", "会社:アティード"}
    assert all(t["from"] == "docs" for t in tags["inherited"])

    assert store.remove("r", ["docs/x.pdf"], "種別:請求書") == 1
    assert store.tags_of("r", "docs/x.pdf")["direct"] == []
    assert store.all_tags() == {"種別:請求書": 1, "会社:アティード": 1}
    assert store.items_with("会社:アティード") == [("r", "docs")]


def test_log_is_the_source_of_truth(tmp_path: Path) -> None:
    """読み直すと同じ状態になる。ログは1行1イベントの JSON。"""
    store = TagStore(tmp_path / "tags", host="a")
    store.add("r", ["a.pdf"], "x")
    lines = (tmp_path / "tags" / "a.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["op"] == "add"
    again = TagStore(tmp_path / "tags", host="a")
    assert again.tags_of("r", "a.pdf")["direct"][0]["tag"] == "x"


def test_logs_from_several_hosts_are_merged(tmp_path: Path) -> None:
    a = TagStore(tmp_path / "tags", host="a")
    b = TagStore(tmp_path / "tags", host="b")
    a.add("r", ["f.pdf"], "x")
    b.add("r", ["f.pdf"], "y")
    # 別の端末のログが増えたら、読み出しの前に読み直す
    assert {t["tag"] for t in a.tags_of("r", "f.pdf")["direct"]} == {"x", "y"}
    assert (tmp_path / "tags" / "b.jsonl").exists() and (tmp_path / "tags" / "a.jsonl").exists()


def test_follow_moves(tmp_path: Path) -> None:
    store = TagStore(tmp_path / "tags", host="a")
    store.add("r", ["inbox/a.pdf", "inbox/dir"], "x")
    store.add("r", ["inbox/dir/b.pdf"], "y")
    # タグの付いたものを含まない移動は記録しない
    assert store.follow_moves("r", [("other/c.pdf", "c.pdf")]) == 0
    assert store.follow_moves("r", [("inbox/a.pdf", "done/a.pdf"), ("inbox/dir", "done/dir")]) == 2
    assert store.tags_of("r", "done/a.pdf")["direct"][0]["tag"] == "x"
    assert store.tags_of("r", "done/dir/b.pdf")["direct"][0]["tag"] == "y"
    assert store.tags_of("r", "inbox/a.pdf")["direct"] == []
    # 読み直しても同じ（move のイベントで再現できる）
    assert TagStore(tmp_path / "tags", host="a").tags_of("r", "done/dir")["direct"][0]["tag"] == "x"


def test_broken_line_is_skipped(tmp_path: Path) -> None:
    """同期の途中で最後の行が欠けていても、読める行は使う。"""
    store = TagStore(tmp_path / "tags", host="a")
    store.add("r", ["a.pdf"], "x")
    with (tmp_path / "tags" / "other.jsonl").open("w", encoding="utf-8") as f:
        f.write('{"at": "2099", "op": "add", "root": "r", "path": "b.pdf", "tag": "y"}\n{"at": "20')
    assert TagStore(tmp_path / "tags", host="a").tags_of("r", "b.pdf")["direct"][0]["tag"] == "y"


def test_rename_merges_and_delete(tmp_path: Path) -> None:
    store = TagStore(tmp_path / "tags", host="a")
    store.add("r", ["a.pdf", "b.pdf"], "会社:ATID")
    store.add("r", ["b.pdf"], "会社:アティード")
    store.add("s", ["c.pdf"], "会社:ATID")
    assert store.rename_tag("会社:ATID", "会社:アティード") == 3  # 全ルート
    assert store.all_tags() == {"会社:アティード": 3}  # b.pdf では1つにまとめる
    assert store.rename_tag("無い", "x") == 0
    assert store.delete_tag("会社:アティード") == 3
    assert store.all_tags() == {}
    # ログから読み直しても同じ
    assert TagStore(tmp_path / "tags", host="a").all_tags() == {}


def test_search_with_inheritance() -> None:
    from tag_keeper.tags import search

    tagged = {"area:finance": {"Finance"}, "type:invoice": {"Finance/Invoices", "Inbox/x.pdf"}, "draft": {"Finance/Invoices/old.pdf"}}
    entries = [(p, False) for p in ["Finance/Invoices/a.pdf", "Finance/Invoices/old.pdf", "Finance/Receipts/r.pdf", "Inbox/x.pdf", "Inbox/y.pdf"]]
    assert search(entries, tagged, ["area:finance", "type:invoice"], "and") == ["Finance/Invoices/a.pdf", "Finance/Invoices/old.pdf"]
    assert search(entries, tagged, ["area:finance", "type:invoice"], "or") == [p for p, _ in entries if p != "Inbox/y.pdf"]
    assert search(entries, tagged, ["type:invoice"], "and", ["draft"]) == ["Finance/Invoices/a.pdf", "Inbox/x.pdf"]
    assert search(entries, tagged, [], "and", ["area:finance"]) == ["Inbox/x.pdf", "Inbox/y.pdf"]
    assert search(entries, tagged, ["無い"], "and") == []
