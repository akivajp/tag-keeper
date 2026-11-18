"""AI エージェント向けの MCP サーバー（tag-keeper mcp）のテスト。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from conftest import write
from tag_keeper.cli import main
from tag_keeper.mcp_server import AgentTools
from tag_keeper.tags import TagStore
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
    assert tools.list_tags()["tags"] == [{"tag": "会議", "count": 1}]
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
    assert names == {"list_roots", "search", "get_item", "read_text", "list_tags", "add_tags", "remove_tags", "set_attr", "unset_attr"}
    # 読み取りのツールには読み取り専用の印が付く
    assert next(t for t in listed if t.name == "search").annotations.read_only_hint is True
    assert not ok.is_error and ok.structured_content["total"] == 1
    # 想定内のエラーは、エージェントが読める文で返る
    assert bad.is_error and "不正なパス" in bad.content[0].text
