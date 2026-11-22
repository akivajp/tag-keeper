"""AI エージェント向けの MCP サーバー（tag-keeper mcp）のテスト。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from conftest import write
from tag_keeper.cli import main
from tag_keeper.mcp_server import AgentTools
from tag_keeper.tags import TagError, TagStore
from tag_keeper.web.files import PathError


@pytest.fixture
def tools(tmp_path: Path, root: Path) -> AgentTools:
    """走査・ハッシュ・本文の抽出まで済ませたカタログと、そのツールを用意する。"""
    write(root / "docs" / "見積書_2026.txt", "株式会社サンプル 御見積 合計 120,000 円")
    write(root / "docs" / "memo.txt", "打ち合わせのメモ")
    write(root / "photos" / "a.jpg", b"\xff\xd8\xff")
    cfg = write(tmp_path / "config.toml", f'[[roots]]\nname = "data"\npath = "{root}"\n[hygiene]\ndevice_names = []\n')
    db = tmp_path / "data" / "catalog.db"
    for cmd in ("scan", "hash", "extract"):
        assert main(["--config", str(cfg), "--db", str(db), cmd]) == 0
    return AgentTools(cfg, db)


def test_list_roots(tools: AgentTools) -> None:
    roots = tools.list_roots()
    assert [r["name"] for r in roots] == ["data"]
    assert roots[0]["files"] == 3 and roots[0]["scanned"] and roots[0]["last_scan"]


def test_search_by_name_text_kind_and_tags(tools: AgentTools) -> None:
    # 名前の語
    res = tools.search(words=["見積"])
    assert [i["path"] for i in res["items"]] == ["docs/見積書_2026.txt"]
    # 本文の語（一致したところも返る）
    res = tools.search(text="御見積")
    assert res["total"] == 1 and "御見積" in res["items"][0]["snippet"]
    # 種類
    assert [i["path"] for i in tools.search(kind="image")["items"]] == ["photos/a.jpg"]
    # 条件が無ければ拒む・種類の誤りも拒む
    with pytest.raises(ValueError):
        tools.search()
    with pytest.raises(ValueError):
        tools.search(kind="nope")
    # フォルダに付けたタグは配下にも効く
    tools.add_tags("data", ["docs"], ["書類"])
    res = tools.search(tags=["書類"])
    assert {i["path"] for i in res["items"]} == {"docs/見積書_2026.txt", "docs/memo.txt"}
    assert res["items"][0]["tags"] == ["書類"]
    assert {i["path"] for i in tools.search(tags=["書類"], exclude_tags=["書類"])["items"]} == set()
    # 件数の上限と全件数
    res = tools.search(under="docs", limit=1)
    assert res["total"] == 2 and res["returned"] == 1


def test_get_item_and_read_text(tools: AgentTools) -> None:
    item = tools.get_item("data", "docs/見積書_2026.txt")
    assert item["in_catalog"] and item["extracted_text"]["chars"] > 0 and item["size"] > 0
    folder = tools.get_item("data", "docs")
    assert folder["is_dir"] and folder["children"] == ["memo.txt", "見積書_2026.txt"]
    # 本文は offset・max_chars で分けて読める
    whole = tools.read_text("data", "docs/見積書_2026.txt")
    assert "120,000" in whole["text"] and not whole["truncated"]
    head = tools.read_text("data", "docs/見積書_2026.txt", max_chars=4)
    assert head["text"] == whole["text"][:4] and head["truncated"]
    rest = tools.read_text("data", "docs/見積書_2026.txt", offset=4)
    assert head["text"] + rest["text"] == whole["text"]
    # 本文が無いもの
    with pytest.raises(ValueError):
        tools.read_text("data", "photos/a.jpg")


def test_paths_must_stay_inside_root(tools: AgentTools, root: Path) -> None:
    for bad in ("../etc/passwd", "docs/../../x", "/etc/passwd"):
        with pytest.raises(PathError):
            tools.get_item("data", bad)
    with pytest.raises(PathError):
        tools.add_tags("data", ["nothing.txt"], ["x"])
    # ルートの中の絶対パスは受け付ける
    assert tools.get_item("data", str(root / "docs" / "memo.txt"))["path"] == "docs/memo.txt"
    with pytest.raises(Exception, match="ルートがありません"):
        tools.get_item("other", "docs")


def test_tags_and_attrs_are_recorded_as_agent(tools: AgentTools, tmp_path: Path) -> None:
    res = tools.add_tags("data", ["docs/memo.txt"], ["会議", "会議"])
    assert res["added"] == {"会議": 1}
    item = tools.get_item("data", "docs/memo.txt")
    assert item["tags"]["direct"] == [{"tag": "会議", "source": "agent"}]
    assert tools.list_tags()["tags"] == [{"tag": "会議", "count": 1, "files": 1}]
    assert tools.remove_tags("data", ["docs/memo.txt"], ["会議"])["removed"] == {"会議": 1}
    # 属性は定義済みのものだけ。型に合う値を付けられる
    store = TagStore(tools._config().tags.dir)
    store.define_attr("金額", "number")
    tools.set_attr("data", ["docs/見積書_2026.txt"], "金額", "120000")
    assert tools.search(attrs=["金額>=100000"])["total"] == 1
    assert tools.search(attrs=["金額<100000"])["total"] == 0
    direct = tools.get_item("data", "docs/見積書_2026.txt")["attrs"]["direct"]
    assert direct[0]["name"] == "金額" and direct[0]["source"] == "agent"
    assert tools.unset_attr("data", ["docs/見積書_2026.txt"], "金額")["changed"] == 1


def test_untagged_direct_only_and_effective_counts(tools: AgentTools) -> None:
    # フォルダに付けたタグは配下に継承される。直接付いた件数（count）は 1、効いているファイル（files）は 2
    tools.add_tags("data", ["docs"], ["書類"])
    tags = {t["tag"]: t for t in tools.list_tags()["tags"]}
    assert tags["書類"]["count"] == 1 and tags["書類"]["files"] == 2
    # タグ無し: 継承を含めて何も効いていないものだけ
    assert [i["path"] for i in tools.search(untagged=True)["items"]] == ["photos/a.jpg"]
    # 直接付いたタグだけで見ると、docs の中のファイルもタグ無し。フォルダ自身は付いている
    assert {i["path"] for i in tools.search(untagged=True, direct_only=True)["items"]} == {
        "docs/memo.txt", "docs/見積書_2026.txt", "photos/a.jpg",
    }
    assert tools.search(tags=["書類"], direct_only=True)["total"] == 0
    assert [i["path"] for i in tools.search(tags=["書類"], direct_only=True, folders=True)["items"]] == ["docs"]
    # タグ無しと tags は両立しない
    with pytest.raises(ValueError):
        tools.search(untagged=True, tags=["書類"])


def test_add_tags_takes_several_groups_at_once(tools: AgentTools) -> None:
    res = tools.add_tags("data", items=[
        {"paths": ["docs/memo.txt"], "tags": ["会議"]},
        {"path": "photos/a.jpg", "tags": ["写真", "会議"]},
    ])
    assert res["added"] == {"会議": 2, "写真": 1}
    assert set(res["paths"]) == {"docs/memo.txt", "photos/a.jpg"}
    # どれかの組が不正なら、何も付けない
    with pytest.raises(PathError):
        tools.add_tags("data", items=[{"paths": ["docs/見積書_2026.txt"], "tags": ["見積"]}, {"paths": ["nope.txt"], "tags": ["x"]}])
    assert tools.get_item("data", "docs/見積書_2026.txt")["tags"]["direct"] == []
    with pytest.raises(ValueError):
        tools.add_tags("data")


def test_move_delete_and_undo(tools: AgentTools, root: Path) -> None:
    tools.add_tags("data", ["docs/memo.txt"], ["会議"])
    # 移動・改名: 移動先のフォルダは作られ、タグは追従する
    res = tools.move_items("data", [{"path": "docs/memo.txt", "dest": "notes/2026/打ち合わせ.txt"}])
    assert res["done"] == ["docs/memo.txt"] and res["skipped"] == []
    assert (root / "notes" / "2026" / "打ち合わせ.txt").exists() and not (root / "docs" / "memo.txt").exists()
    assert tools.get_item("data", "notes/2026/打ち合わせ.txt")["tags"]["direct"][0]["tag"] == "会議"
    # 既にある名前へは上書きしない・ルートの外へは出さない
    with pytest.raises(PathError, match="同じ名前"):
        tools.move_items("data", [{"path": "photos/a.jpg", "dest": "docs/見積書_2026.txt"}])
    with pytest.raises(PathError):
        tools.move_items("data", [{"path": "photos/a.jpg", "dest": "../a.jpg"}])
    # 取り消すと元の場所に戻る
    undone = tools.undo_plan(res["plan_id"])
    assert undone["restored"] == ["docs/memo.txt"] and (root / "docs" / "memo.txt").exists()
    # 削除は隔離フォルダへ移すだけで、取り消せる
    res = tools.delete_items("data", ["photos/a.jpg"], reason="テスト")
    assert res["done"] == ["photos/a.jpg"] and not (root / "photos" / "a.jpg").exists()
    quarantine = tools._config().plan.quarantine_dir / res["plan_id"] / "photos" / "a.jpg"
    assert quarantine.exists()
    assert tools.undo_plan(res["plan_id"])["restored"] == ["photos/a.jpg"]
    assert (root / "photos" / "a.jpg").exists()
    # ルートそのものは消せない
    with pytest.raises(PathError):
        tools.delete_items("data", [str(root)])
    with pytest.raises(ValueError, match="実行記録"):
        tools.undo_plan("nope")


def test_search_pages_with_offset(tools: AgentTools) -> None:
    # パス順に並ぶので、next_offset をたどると全件を重ならずに読める
    first = tools.search(untagged=True, limit=2)
    assert first["total"] == 3 and first["returned"] == 2 and first["next_offset"] == 2
    rest = tools.search(untagged=True, limit=2, offset=first["next_offset"])
    assert rest["returned"] == 1 and "next_offset" not in rest
    paths = [i["path"] for i in first["items"] + rest["items"]]
    assert paths == ["docs/memo.txt", "docs/見積書_2026.txt", "photos/a.jpg"]
    # 全件数を超えた offset は空
    assert tools.search(untagged=True, offset=10)["returned"] == 0


def test_define_tag_rename_tag_and_define_attr(tools: AgentTools) -> None:
    tools.add_tags("data", ["docs/memo.txt"], ["会議"])
    tools.add_tags("data", ["docs/見積書_2026.txt"], ["ミーティング"])
    # 既に使われている名前を別名にすると、付いたアイテムごと正式な名前へまとまる
    res = tools.define_tag("会議", aliases=["ミーティング"], parents=["仕事"])
    assert res["tag"] == "会議" and res["aliases"] == ["ミーティング"] and res["parents"] == ["仕事"]
    assert tools.search(tags=["会議"])["total"] == 2
    # 親のタグでも探せる
    assert tools.search(tags=["仕事"])["total"] == 2
    # 循環する親子・ほかのタグの別名を外すのは断る
    with pytest.raises(TagError):
        tools.define_tag("仕事", parents=["会議"])
    with pytest.raises(TagError):
        tools.define_tag("仕事", remove_aliases=["ミーティング"])
    assert tools.define_tag("会議", remove_parents=["仕事"])["parents"] == []
    # 名前の変更: 新しい名前なら統合ではない。既にあるタグへの変更は統合として知らせる
    res = tools.rename_tag("会議", "打ち合わせ")
    assert res == {"from": "会議", "to": "打ち合わせ", "changed": 2, "merged": False}
    tools.add_tags("data", ["photos/a.jpg"], ["写真"])
    assert tools.rename_tag("写真", "打ち合わせ")["merged"] is True
    with pytest.raises(ValueError, match="ありません"):
        tools.rename_tag("無いタグ", "x")
    # 属性を定義すれば、set_attr で値を付けられる
    d = tools.define_attr("状態", "choice", ["未処理", "済み"])
    assert d["type"] == "choice" and d["choices"] == ["未処理", "済み"]
    tools.set_attr("data", ["docs/memo.txt"], "状態", "済み")
    assert tools.search(attrs=["状態=済み"])["total"] == 1
    with pytest.raises(TagError):
        tools.define_attr("x", "nope")
    with pytest.raises(TagError):
        tools.define_attr("y", "choice", ["1つだけ"])


def test_mcp_server_lists_and_calls_tools(tools: AgentTools) -> None:
    pytest.importorskip("mcp")
    from mcp import Client

    from tag_keeper.mcp_server import build_server

    server = build_server(tools)

    async def run() -> tuple[list[Any], Any, Any]:
        async with Client(server) as client:
            listed = await client.list_tools()
            ok = await client.call_tool("search", {"words": ["memo"]})
            bad = await client.call_tool("get_item", {"root": "data", "path": "../x"})
            return listed.tools, ok, bad

    listed, ok, bad = asyncio.run(run())
    names = {t.name for t in listed}
    assert names == {
        "list_roots", "search", "get_item", "read_text", "list_tags", "add_tags", "remove_tags", "set_attr", "unset_attr",
        "move_items", "delete_items", "undo_plan", "define_tag", "rename_tag", "define_attr",
    }
    # 削除（隔離）には、破壊的な操作の印が付く
    assert next(t for t in listed if t.name == "delete_items").annotations.destructive_hint is True
    # 読み取りのツールには読み取り専用の印が付く
    assert next(t for t in listed if t.name == "search").annotations.read_only_hint is True
    assert not ok.is_error and ok.structured_content["total"] == 1
    # 想定内のエラーは、エージェントが読める文で返る
    assert bad.is_error and "不正なパス" in bad.content[0].text
