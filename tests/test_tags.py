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


def test_moves_found_together_are_applied_as_if_at_once(tmp_path: Path) -> None:
    """1回の走査で見つけた移動は、配下を先に・入れ替えでは移動元を先に空けて当てはめる。"""
    from tag_keeper.tags import order_moves

    store = TagStore(tmp_path / "tags", host="a")
    store.add("r", ["a/x.pdf"], "種別:請求書")
    store.add("r", ["c"], "会社:アティード")
    store.add("r", ["p"], "色:赤")
    # フォルダの改名と、その中のファイルの改名（a → b、a/x.pdf → b/y.pdf）と、入れ替え（p → q、c → p）
    store.follow_moves("r", [("a", "b"), ("a/x.pdf", "b/y.pdf"), ("c", "p"), ("p", "q")])
    assert store.tags_of("r", "b/y.pdf")["direct"] == [{"tag": "種別:請求書", "source": "manual"}]
    assert store.tags_of("r", "b/x.pdf")["direct"] == []
    assert [t["tag"] for t in store.tags_of("r", "p")["direct"]] == ["会社:アティード"]
    assert [t["tag"] for t in store.tags_of("r", "q")["direct"]] == ["色:赤"]
    # 並べ方: 配下が先、移動元を空けるものが先
    assert order_moves([("a", "b"), ("a/x.pdf", "b/y.pdf")]) == [("a/x.pdf", "b/y.pdf"), ("a", "b")]
    assert order_moves([("c", "p"), ("p", "q")]) == [("p", "q"), ("c", "p")]


def test_detach_and_attach_keep_the_source(tmp_path: Path) -> None:
    store = TagStore(tmp_path / "tags", host="a")
    store.add("r", ["d"], "x", source="manual")
    store.add("r", ["d/f.pdf"], "y", source="suggested")
    store.add("r", ["other"], "z")
    detached = store.detach_under("r", "d")
    assert detached == {"d": [["x", "manual"]], "d/f.pdf": [["y", "suggested"]]}
    assert store.tags_of("r", "d/f.pdf") == {"direct": [], "inherited": [], "implied": []}
    assert store.attach("r", detached) == 2
    assert store.tags_of("r", "d/f.pdf")["direct"] == [{"tag": "y", "source": "suggested"}]
    # ログから読み直しても同じ
    again = TagStore(tmp_path / "tags", host="a")
    assert again.tags_of("r", "d")["direct"] == [{"tag": "x", "source": "manual"}]


# ---------- タグの辞書（別名・親子）・型付きの属性・保存した検索 ----------


def test_aliases_are_folded_into_the_canonical_name(tmp_path: Path) -> None:
    """別名で付けても探しても、正式な名前になる。既に使われていた名前を別名にすると、正式な名前にまとまる。"""
    from tag_keeper.tags import search

    store = TagStore(tmp_path / "tags", host="a")
    store.add("r", ["a.pdf"], "会社:ATID")
    store.add("r", ["b.pdf"], "会社:アティード")
    store.add_alias("会社:アティード", "会社:ATID")  # 既に使われている名前を別名にする
    assert store.all_tags() == {"会社:アティード": 2}
    assert store.canonical(" 会社 : ATID ") == "会社:アティード"
    assert store.add("r", ["c.pdf"], "会社:ATID") == 1
    assert store.tags_of("r", "c.pdf")["direct"][0]["tag"] == "会社:アティード"
    tagged = store.tagged_paths("r", implied=True)
    hits = search([("a.pdf", False), ("b.pdf", False), ("c.pdf", False)], tagged, [store.canonical("会社:ATID")])
    assert hits == ["a.pdf", "b.pdf", "c.pdf"]
    with pytest.raises(TagError):
        store.add_alias("会社:アティード", "会社:アティード")
    # ログから読み直しても同じ。別名の登録は外せる
    again = TagStore(tmp_path / "tags", host="a")
    assert again.dictionary()["会社:アティード"]["aliases"] == ["会社:ATID"]
    assert again.remove_alias("会社:ATID") and again.canonical("会社:ATID") == "会社:ATID"


def test_parent_tags_are_implied_and_cycles_are_refused(tmp_path: Path) -> None:
    from tag_keeper.tags import search

    store = TagStore(tmp_path / "tags", host="a")
    store.add_parent("種別:請求書", "種別:経理")
    store.add_parent("種別:経理", "種別:書類")
    store.add("r", ["inv.pdf"], "種別:請求書")
    store.add("r", ["memo.txt"], "種別:メモ")
    implied = store.tags_of("r", "inv.pdf")["implied"]
    assert implied == [{"tag": "種別:書類", "via": "種別:請求書"}, {"tag": "種別:経理", "via": "種別:請求書"}]
    entries = [("inv.pdf", False), ("memo.txt", False)]
    assert search(entries, store.tagged_paths("r", implied=True), ["種別:書類"]) == ["inv.pdf"]
    assert search(entries, store.tagged_paths("r"), ["種別:書類"]) == []  # 広げないとき
    with pytest.raises(TagError):
        store.add_parent("種別:書類", "種別:請求書")  # 循環
    # タグの名前を変えると、親子の中の名前も変わる。消すと親子も消える
    store.rename_tag("種別:経理", "種別:会計")
    assert store.dictionary()["種別:請求書"]["parents"] == ["種別:会計"]
    store.delete_tag("種別:会計")
    assert store.tags_of("r", "inv.pdf")["implied"] == []


def test_typed_attributes(tmp_path: Path) -> None:
    """択一には値が1つだけ付き、型に合わない値は断る。フォルダの値は配下が継承し、移動に追従する。"""
    store = TagStore(tmp_path / "tags", host="a")
    store.define_attr("対応状態", "choice", ["未対応", "対応中", "対応済み"])
    store.define_attr("要対応", "flag")
    store.define_attr("期限", "date")
    store.define_attr("金額", "number")
    store.define_attr("取引先", "text")
    store.set_attr("r", ["case/a.pdf"], "対応状態", "未対応")
    store.set_attr("r", ["case/a.pdf"], "対応状態", "対応中")  # 置き換わる（2つの値は付かない）
    store.set_attr("r", ["case/a.pdf"], "金額", "12,345")
    store.set_attr("r", ["case"], "取引先", " 株式会社  アティード ")
    store.set_attr("r", ["case"], "要対応", True)
    attrs = store.attrs_of("r", "case/a.pdf")
    assert attrs["direct"] == [
        {"name": "対応状態", "value": "対応中", "source": "manual"},
        {"name": "金額", "value": 12345, "source": "manual"},
    ]
    assert attrs["inherited"] == [
        {"name": "取引先", "value": "株式会社 アティード", "from": "case"},
        {"name": "要対応", "value": True, "from": "case"},
    ]
    for name, bad in (("対応状態", "保留"), ("期限", "2026/13/01"), ("金額", "たくさん"), ("要対応", "no"), ("未定義", 1)):
        with pytest.raises(TagError):
            store.set_attr("r", ["case/a.pdf"], name, bad)
    with pytest.raises(TagError):
        store.define_attr("区分", "choice", ["一つだけ"])
    store.follow_moves("r", [("case", "done/case")])
    assert store.attrs_of("r", "done/case/a.pdf")["direct"][0]["value"] == "対応中"
    # ログから読み直しても同じ。定義を消すと値も外れる
    again = TagStore(tmp_path / "tags", host="a")
    assert {d["name"]: d["count"] for d in again.attr_defs_list()}["金額"] == 1
    assert again.undefine_attr("金額") == 1
    assert [v["name"] for v in again.attrs_of("r", "done/case/a.pdf")["direct"]] == ["対応状態"]


def test_attributes_of_quarantined_items_come_back_with_undo(tmp_path: Path) -> None:
    store = TagStore(tmp_path / "tags", host="a")
    store.define_attr("対応状態", "choice", ["未対応", "対応済み"])
    store.set_attr("r", ["d/x.pdf"], "対応状態", "対応済み")
    detached = store.detach_attrs_under("r", "d")
    assert detached == {"d/x.pdf": [["対応状態", "対応済み", "manual"]]}
    assert store.attrs_of("r", "d/x.pdf")["direct"] == []
    assert store.attach_attrs("r", detached) == 1
    assert store.attrs_of("r", "d/x.pdf")["direct"][0]["value"] == "対応済み"


def test_saved_searches(tmp_path: Path) -> None:
    store = TagStore(tmp_path / "tags", host="a")
    store.save_search("要対応の書類", "?attr=要対応&kind=document")
    store.save_search("請求書", "text=請求書")
    store.save_search("請求書", "text=請求書&kind=pdf")  # 同じ名前は置き換える
    assert store.saved_searches() == [{"name": "要対応の書類", "query": "attr=要対応&kind=document"}, {"name": "請求書", "query": "text=請求書&kind=pdf"}]
    assert store.delete_search("請求書") and not store.delete_search("請求書")
    assert [s["name"] for s in TagStore(tmp_path / "tags", host="b").saved_searches()] == ["要対応の書類"]
    with pytest.raises(TagError):
        store.save_search("空", "  ")
