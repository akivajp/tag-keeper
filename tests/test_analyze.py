"""書類の構造化抽出・画像の内容推定・確認（承認と却下）・外部 AI の可否のテスト。モデルは差し替える。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from conftest import write
from tag_keeper import analyze, fulltext, organize
from tag_keeper.config import Config, OrganizeConfig, RootConfig, SafetyConfig
from tag_keeper.hashing import hash_root
from tag_keeper.scan import scan_root
from tag_keeper.tags import TagStore
from test_web import HOST, env, run_job  # noqa: F401 - env は fixture

SAFETY = SafetyConfig(mass_missing_ratio=0.2, mass_missing_min=3)


def test_answers_are_normalized() -> None:
    d = analyze.normalize_document({
        "doc_type": "請求書", "date": "2026-10-31", "party": " 株式会社  アティード ", "amount": "12,345円",
        "due": "2026/11/30", "action": "null", "labels": ["経費", "", "税金", "保険", "医療"], "summary": "10 月分の請求",
    })
    assert d["amount"] == 12345 and d["party"] == "株式会社 アティード" and d["date"] == "2026-10-31"
    assert d["due"] is None and d["action"] is None and d["labels"] == ["経費", "税金", "保険"]
    assert analyze.normalize_image({"kind": "PHOTO", "description": "湖と山", "labels": "x"}) == {"kind": "photo", "description": "湖と山", "labels": []}


def test_cloud_models_are_not_used_for_roots_that_keep_content_local() -> None:
    oc = OrganizeConfig(model="gemma4:31b-cloud", vision_model="qwen-vl:cloud", local_model="gemma4:12b")
    local, note = organize.config_for_root(oc, False)
    assert (local.model, local.vision_model) == ("gemma4:12b", "") and "外部へ送らない" in note
    same, note = organize.config_for_root(oc, True)
    assert same.model == "gemma4:31b-cloud" and note == ""


def test_documents_and_images_are_read_and_reviewed(
    conn: sqlite3.Connection, root_id: int, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(root / "inv.txt", "請求書 株式会社アティード 合計 12,345円 支払期限 2026-11-30")
    write(root / "lake.png", b"\x89PNG fake")
    write(root / "notes.bin", b"\0")
    scan_root(conn, root_id, root, SAFETY)
    hash_root(conn, root_id, root)
    rc = RootConfig(name="test", path=root, allow_cloud=False)
    config = Config(roots=[rc])
    config.organize.model = "gemma4:31b-cloud"
    config.tags.dir = tmp_path / "tags"
    fulltext.run_pending(conn, root_id, root, config)
    used: list[str] = []

    def fake_generate(oc: OrganizeConfig, prompt: str, image: bytes | None = None, **kw: object) -> dict:
        used.append(str(kw.get("model") or oc.model))
        if image is not None:
            return {"kind": "photo", "description": "湖と山の写真", "labels": ["風景"]}
        return {"doc_type": "請求書", "date": "2026-10-31", "party": "株式会社アティード", "amount": 12345, "due": "2026-11-30",
                "action": "支払う", "labels": ["経費"], "summary": "請求"}

    monkeypatch.setattr(organize, "ollama_generate", fake_generate)
    monkeypatch.setattr(organize, "image_model", lambda oc: "local-vision")
    assert sorted(t.relpath for t in analyze.pending(conn, root_id)) == ["inv.txt", "lake.png"]
    results = analyze.run_pending(conn, root_id, rc, config)
    assert len(results) == 2 and not any(a.error for a in results)
    assert "gemma4:31b-cloud" not in used and "gemma4:12b" in used  # 外部へ送らないルート
    assert analyze.pending(conn, root_id) == []
    # 画像の説明は全文検索で探せる
    assert [r["relpath"] for r in fulltext.find_entries(conn, root_id, text="湖と山")] == ["lake.png"]

    store = TagStore(tmp_path / "tags", host="t")
    items = {i["path"]: i for i in analyze.review_items(conn, root_id, "test", store.reviewed("test"))}
    assert items["inv.txt"]["proposal"] == {
        "tags": ["種別:請求書", "分類:経費"],
        "attrs": {"日付": "2026-10-31", "相手": "株式会社アティード", "金額": 12345, "期限": "2026-11-30", "必要な対応": "支払う", "要対応": True},
    }
    assert items["lake.png"]["proposal"]["tags"] == ["画像:写真", "分類:風景"]
    # 承認: 選んだものだけを付け、足りない属性は定義する
    inv = items["inv.txt"]
    n = analyze.apply_accept(store, "test", "inv.txt", ["種別:請求書"], {"金額": 12345, "期限": "2026-11-30"})
    assert n == {"tags": 1, "attrs": 2}
    store.record_review("test", "inv.txt", inv["analysis"]["sha256"], "accept")
    store.record_review("test", "lake.png", items["lake.png"]["analysis"]["sha256"], "reject")
    assert store.tags_of("test", "inv.txt")["direct"] == [{"tag": "種別:請求書", "source": "ai"}]
    assert {a["name"]: a["value"] for a in store.attrs_of("test", "inv.txt")["direct"]} == {"期限": "2026-11-30", "金額": 12345}
    assert analyze.review_items(conn, root_id, "test", store.reviewed("test")) == []


def test_review_api(env: dict, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811 - env は test_web の fixture
    app, root = env["app"], env["root"]
    origin = {"Origin": f"http://{HOST}"}
    write(root / "bill.txt", "電気料金のお知らせ 3,210円")
    run_job(env, "/api/roots/data/refresh")
    from tag_keeper.catalog import connect

    c = connect(env["settings"].db_path)
    fulltext.run_pending(c, 1, root, env["settings"].config())
    c.close()
    monkeypatch.setattr(organize, "ollama_generate", lambda oc, prompt, image=None, **kw: {"doc_type": "明細", "amount": 3210, "labels": ["住まい"]})
    review = app.get("/api/review/data").json
    assert review["unread"] >= 1 and review["items"] == []
    job = run_job(env, "/api/analyze/data", {"paths": ["bill.txt"]})
    assert job["result"]["ok"] == 1
    items = app.get("/api/review/data").json["items"]
    assert [i["path"] for i in items] == ["bill.txt"]
    it = items[0]
    res = app.post_json("/api/review/data", {"items": [{"path": "bill.txt", "sha256": it["analysis"]["sha256"], "decision": "accept",
                                                       "tags": it["proposal"]["tags"], "attrs": it["proposal"]["attrs"]}]}, headers=origin).json
    assert res["accept"] == 1 and res["tags"] == 2 and res["attrs"] == 1
    assert app.get("/api/review/data").json["items"] == []
    tags = {t["tag"] for t in app.get("/api/info", {"root": "data", "path": "bill.txt"}).json["tags"]["direct"]}
    assert tags == {"種別:明細", "分類:住まい"}


def test_openai_compatible_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """「モデル名@送り先」は OpenAI 互換の API に送る（思考の部分は除く）。手元でない送り先はクラウドと同じ扱い。"""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # /v1/models
            body = json.dumps({"data": [{"id": "qwen-local", "architecture": {"input_modalities": ["text"]}}]}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # /v1/chat/completions
            seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            answer = '<think>{"draft": 1}</think>\n```json\n{"doc_type": "請求書", "amount": 120}\n```'
            body = json.dumps({"choices": [{"message": {"role": "assistant", "content": answer}}]}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1"
        oc = OrganizeConfig(model="qwen-local@strata", endpoints={"strata": url, "far": "https://llm.example.com/v1"})
        assert organize.model_capabilities(oc) == ["completion"]
        with pytest.raises(organize.OllamaError):
            organize.model_capabilities(oc, "missing@strata")
        assert organize.ollama_generate(oc, "読んで") == {"doc_type": "請求書", "amount": 120}
        assert seen[0]["model"] == "qwen-local" and seen[0]["messages"][0]["content"] == "読んで"
        assert not organize.sends_outside(oc, "qwen-local@strata")
        assert organize.sends_outside(oc, "big@far") and organize.sends_outside(oc, "gemma4:31b-cloud")
        local, note = organize.config_for_root(dataclasses_replace(oc, model="big@far"), False)
        assert local.model == oc.local_model and note
    finally:
        server.shutdown()


def dataclasses_replace(obj, **kw):
    import dataclasses

    return dataclasses.replace(obj, **kw)


def test_local_models_do_not_think_by_default() -> None:
    oc = OrganizeConfig(model="gemma4:12b", endpoints={"strata": "http://127.0.0.1:18080/v1"})
    assert organize.effective_think(oc, "gemma4:12b") == "false"
    assert organize.effective_think(oc, "qwen@") == "false"
    assert organize.effective_think(oc, "gemma4:31b-cloud") == ""  # クラウドのモデルはモデルに任せる
    assert organize.effective_think(dataclasses_replace(oc, think="true"), "gemma4:12b") == "true"
