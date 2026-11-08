"""Web の画面のサーバー（API・アクセス制御・ジョブ）のテスト。"""

from __future__ import annotations

import base64
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from webtest import TestApp

from conftest import write
from tag_keeper.jobs import JobManager
from tag_keeper.web.auth import Credentials, host_allowed
from tag_keeper.web.server import Settings, create_app

HOST = "127.0.0.1:8090"


@pytest.fixture
def env(tmp_path: Path, root: Path) -> dict:
    """設定ファイル・カタログ・アプリをテスト用に用意する。"""
    write(root / "docs" / "report.pdf", "keep")
    write(root / "docs" / "Thumbs.db", "junk")
    write(root / "Documents" / "DMMGames" / "a.dat", "game")
    cfg = write(
        tmp_path / "config.toml",
        f'[[roots]]\nname = "data"\npath = "{root}"\n[hygiene]\ndevice_names = []\n'
        f'[plan]\nquarantine_dir = "{tmp_path / "quarantine"}"\nsnapper_config = ""\n',
    )
    settings = Settings(cfg, tmp_path / "data" / "catalog.db")
    jobs = JobManager(tmp_path / "logs")
    app = TestApp(create_app(settings, jobs=jobs), extra_environ={"HTTP_HOST": HOST})
    return {"app": app, "jobs": jobs, "root": root, "settings": settings}


def run_job(env: dict, url: str, body: dict | None = None) -> dict:
    """ジョブを始めて、終わるまで待ち、その結果を返す。"""
    res = env["app"].post_json(url, body or {}, headers={"Origin": f"http://{HOST}"})
    job_id = res.json["id"]
    env["jobs"].wait()
    job = env["app"].get(f"/api/jobs/{job_id}").json["job"]
    assert job["status"] == "done", job
    return job


def test_index_and_static_files(env: dict) -> None:
    res = env["app"].get("/")
    assert "tag-keeper" in res.text
    assert "default-src 'self'" in res.headers["Content-Security-Policy"]
    assert env["app"].get("/static/app.js").status_int == 200


def test_scan_report_plan_apply_undo(env: dict) -> None:
    app, root = env["app"], env["root"]
    job = run_job(env, "/api/roots/data/refresh")
    assert [p["name"] for p in job["phases"]] == ["走査", "ハッシュ計算"]
    assert job["result"]["scan"]["added"] == 3

    state = app.get("/api/state").json
    assert state["roots"][0]["files"] == 3 and state["roots"][0]["hashed"] == 3

    report = app.get("/api/report/data").json
    assert {f["relpath"] for f in report["findings"]} == {"docs/Thumbs.db", "Documents/DMMGames"}

    plan_id = run_job(env, "/api/plans", {"root": "data", "categories": ["os-junk", "app-data"]})["result"]["plan_id"]
    plan = app.get(f"/api/plans/{plan_id}").json
    assert plan["info"]["state"] == "draft" and len(plan["ops"]) == 2

    # 画面でチェックを外したものは、実行しない
    app.post_json(f"/api/plans/{plan_id}/skips", {"skip": ["docs/Thumbs.db"]})
    assert app.get(f"/api/plans/{plan_id}").json["info"]["skipped"] == 1

    check = run_job(env, f"/api/plans/{plan_id}/check")
    assert check["result"]["problems"] == {}

    applied = run_job(env, f"/api/plans/{plan_id}/apply")
    assert applied["result"]["done"] == 1
    assert "実行（隔離・移動）" in [p["name"] for p in applied["phases"]]
    assert not (root / "Documents" / "DMMGames").exists() and (root / "docs" / "Thumbs.db").exists()
    plan = app.get(f"/api/plans/{plan_id}").json
    assert plan["info"]["state"] == "applied" and plan["restorable"] == 1
    # 実行したプランは編集も削除もできない
    app.post_json(f"/api/plans/{plan_id}/skips", {"skip": []}, status=409)
    app.post_json(f"/api/plans/{plan_id}/delete", {}, status=409)

    run_job(env, f"/api/plans/{plan_id}/undo")
    assert (root / "Documents" / "DMMGames" / "a.dat").exists()
    assert app.get(f"/api/plans/{plan_id}").json["info"]["state"] == "undone"


def test_only_one_job_at_a_time(env: dict) -> None:
    started = env["jobs"].start("test", "長い処理", lambda rep: time.sleep(0.3))
    try:
        env["app"].post_json("/api/roots/data/scan", {}, status=409)
    finally:
        env["jobs"].wait()
    assert started.status == "done"


def test_failed_job_reports_the_error(env: dict) -> None:
    res = env["app"].post_json("/api/plans", {"root": "data", "categories": []})
    env["jobs"].wait()
    job = env["app"].get(f"/api/jobs/{res.json['id']}").json["job"]
    assert job["status"] == "failed" and "まだ走査していません" in job["error"]


def test_cross_site_writes_are_refused(env: dict) -> None:
    env["app"].post_json("/api/roots/data/scan", {}, headers={"Origin": "http://evil.example"}, status=403)


def test_unexpected_host_is_refused(env: dict) -> None:
    """DNS リバインディング対策: 待ち受けているアドレス以外の Host は断る。"""
    env["app"].get("/api/state", extra_environ={"HTTP_HOST": "evil.example:8090"}, status=403)
    assert host_allowed("localhost:8090", "127.0.0.1")
    assert host_allowed("100.90.1.2:8090", "100.90.1.2")
    assert not host_allowed("evil.example", "100.90.1.2")


def test_basic_auth(tmp_path: Path, env: dict) -> None:
    app = TestApp(
        create_app(env["settings"], Credentials.parse("me:secret"), jobs=env["jobs"]),
        extra_environ={"HTTP_HOST": HOST},
    )
    app.get("/api/state", status=401)
    token = base64.b64encode(b"me:secret").decode()
    assert app.get("/api/state", headers={"Authorization": f"Basic {token}"}).status_int == 200


def test_unknown_plan_and_bad_ids(env: dict) -> None:
    env["app"].get("/api/plans/nope", status=404)
    env["app"].get("/api/plans/..%2Fetc", status=(400, 404))


def test_browse_and_file_serving(env: dict) -> None:
    app, root = env["app"], env["root"]
    write(root / "docs" / "page.html", "<script>alert(1)</script>")
    write(root / "docs" / "photo.jpg", b"\xff\xd8\xff")
    data = app.get("/api/browse", {"root": "data", "path": "docs"}).json
    assert [e["name"] for e in data["entries"]] == ["page.html", "photo.jpg", "report.pdf", "Thumbs.db"]
    assert [c["path"] for c in data["crumbs"]] == ["", "docs"]
    # 画像は画面の中で開く。HTML はスクリプトを動かさないよう text/plain にする
    assert app.get("/api/file", {"root": "data", "path": "docs/photo.jpg"}).headers["Content-Type"].startswith("image/jpeg")
    html = app.get("/api/file", {"root": "data", "path": "docs/page.html"})
    assert html.headers["Content-Type"].startswith("text/plain")
    dl = app.get("/api/file", {"root": "data", "path": "docs/report.pdf", "download": "1"})
    assert "attachment" in dl.headers["Content-Disposition"]
    # ルートの外は見せない
    app.get("/api/file", {"root": "data", "path": "../config.toml"}, status=400)
    app.get("/api/browse", {"root": "data", "path": "docs/../.."}, status=400)


def test_symlink_out_of_root_is_refused(env: dict, tmp_path: Path) -> None:
    secret = write(tmp_path / "secret.txt", "secret")
    (env["root"] / "link.txt").symlink_to(secret)
    env["app"].get("/api/file", {"root": "data", "path": "link.txt"}, status=400)


def test_tags_api(env: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    app = env["app"]
    origin = {"Origin": f"http://{HOST}"}
    app.post_json("/api/tags", {"root": "data", "paths": ["docs"], "tag": "種別:書類"}, headers=origin)
    app.post_json("/api/tags", {"root": "data", "paths": ["docs/report.pdf"], "tag": "相手:A社"}, headers=origin)
    app.post_json("/api/tags", {"root": "data", "paths": ["nope.pdf"], "tag": "x"}, headers=origin, status=404)
    data = app.get("/api/browse", {"root": "data", "path": "docs"}).json
    assert data["tags"]["direct"][0]["tag"] == "種別:書類"
    assert next(e for e in data["entries"] if e["name"] == "report.pdf")["tags"] == ["相手:A社"]
    assert {t["tag"] for t in app.get("/api/tags").json["tags"]} == {"種別:書類", "相手:A社"}
    items = app.get("/api/tags/items", {"tag": "種別:書類"}).json["items"]
    assert items == [{"root": "data", "path": "docs", "exists": True, "is_dir": True}]
    app.post_json("/api/tags", {"root": "data", "paths": ["docs"], "tag": "種別:書類", "op": "remove"}, headers=origin)
    assert app.get("/api/browse", {"root": "data", "path": "docs"}).json["tags"]["direct"] == []


def test_organize_plan_from_accepted_suggestions(env: dict) -> None:
    app, root = env["app"], env["root"]
    write(root / "99_Inbox" / "tmp" / "downloadfile.pdf", "statement")
    run_job(env, "/api/roots/data/scan")
    data = app.get("/api/organize/data").json
    assert data["inbox"] == ["99_Inbox"] and data["files"][0]["path"] == "99_Inbox/tmp/downloadfile.pdf"
    origin = {"Origin": f"http://{HOST}"}
    items = [{"path": "99_Inbox/tmp/downloadfile.pdf", "dest": "docs/20250414_明細書.pdf", "reason": "明細書"}]
    app.post_json("/api/organize/data/plan", {"items": [{"path": "99_Inbox/tmp/downloadfile.pdf", "dest": "docs/a?.pdf"}]}, headers=origin, status=400)
    app.post_json("/api/organize/data/plan", {"items": [{"path": "99_Inbox/tmp/downloadfile.pdf", "dest": "docs/report.pdf"}]}, headers=origin, status=409)
    plan_id = app.post_json("/api/organize/data/plan", {"items": items}, headers=origin).json["plan_id"]
    plan = app.get(f"/api/plans/{plan_id}").json
    assert plan["ops"][0]["action"] == "move" and plan["ops"][0]["dest"] == "docs/20250414_明細書.pdf"
    run_job(env, f"/api/plans/{plan_id}/apply")
    assert (root / "docs" / "20250414_明細書.pdf").read_text() == "statement"


def test_suggest_queue_feedback_and_decision_log(env: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """1件ずつの提案 → タグの採否 → 採用して実行 → 取り消し、が判断のログに残る。"""
    import json as _json

    from tag_keeper import organize

    def generate(config: Any, prompt: str, image: bytes | None = None, **_: Any) -> dict:
        if "内容を読み取ってください" in prompt:
            return {"title": "明細書", "doc_type": "明細書", "summary": "利用明細"}
        return {"new_name": "20250414_明細書", "date": "20250414", "destinations": [{"index": 1, "reason": "書類の置き場"}], "tags": ["種別:明細書", "相手:A社"]}

    monkeypatch.setattr(organize, "ollama_generate", generate)
    monkeypatch.setattr(organize, "check_ollama", lambda config: None)
    app, root = env["app"], env["root"]
    origin = {"Origin": f"http://{HOST}"}
    write(root / "99_Inbox" / "downloadfile.pdf", "statement")
    run_job(env, "/api/roots/data/scan")

    path = "99_Inbox/downloadfile.pdf"
    app.post_json("/api/suggest", {"root": "data", "paths": [path], "model": "m"}, headers=origin)
    deadline = time.time() + 10
    while time.time() < deadline:
        reqs = app.get("/api/suggest/queue", {"root": "data"}).json["requests"]
        if reqs and reqs[0]["state"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert reqs[0]["state"] == "done", reqs
    sug = app.get("/api/suggestion", {"root": "data", "path": path, "model": "m"}).json["suggestion"]
    assert sug["new_name"] == "20250414_明細書.pdf" and sug["tags"] == ["種別:明細書", "相手:A社"]

    app.post_json("/api/tags/feedback", {"root": "data", "path": path, "tag": "種別:明細書", "decision": "accept", "model": "m"}, headers=origin)
    app.post_json("/api/tags/feedback", {"root": "data", "path": path, "tag": "相手:A社", "decision": "reject", "model": "m"}, headers=origin)
    tags = app.get("/api/suggestion", {"root": "data", "path": path, "model": "m"}).json["tags"]["direct"]
    assert tags == [{"tag": "種別:明細書", "source": "suggested"}]

    dest = sug["destinations"][0]["relpath"] + "/20250414_明細書.pdf"
    res = app.post_json("/api/organize/data/plan", {"items": [{"path": path, "dest": dest, "model": "m"}], "apply": True}, headers=origin).json
    env["jobs"].wait()
    assert (root / dest).exists()
    run_job(env, f"/api/plans/{res['plan_id']}/undo")

    log_dir = env["settings"].decision_log.log_dir
    events = [_json.loads(l) for f in log_dir.glob("*.jsonl") for l in f.read_text(encoding="utf-8").splitlines()]
    ops = [e["op"] for e in events]
    assert ops == ["tag", "tag", "accept", "revert"]
    acc = events[2]
    assert acc["suggested"]["name"] == "20250414_明細書.pdf" and acc["final"]["dest_dir"] == sug["destinations"][0]["relpath"]
    assert acc["final"]["tags"] == ["種別:明細書"] and acc["info"]["title"] == "明細書"


def test_models_endpoint(env: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from tag_keeper import organize
    from tag_keeper.web import server

    monkeypatch.setattr(organize, "cached_capabilities", lambda oc, name: ["completion", "vision"] if name != "bad" else ["embedding"])
    monkeypatch.setattr(server, "_local_models", lambda oc: ["x:cloud", "bad"])
    data = env["app"].get("/api/models").json
    assert data["default"] == "gemma3:12b"
    assert [(m["name"], m["cloud"]) for m in data["models"]] == [("gemma3:12b", False), ("x:cloud", True)]


def test_rename_and_delete_from_the_browser(env: dict) -> None:
    """ファイル・フォルダの名前の変更と削除は、1件ずつのプランとして実行し、取り消せる。"""
    import json as _json

    app, root = env["app"], env["root"]
    origin = {"Origin": f"http://{HOST}"}
    write(root / "projects" / "old" / "memo.txt", "memo")
    run_job(env, "/api/roots/data/scan")
    app.post_json("/api/tags", {"root": "data", "paths": ["projects/old/memo.txt"], "tag": "種別:メモ"}, headers=origin)

    # ファイルの名前を変える（好きな名前）
    r = app.post_json("/api/fileops", {"root": "data", "op": "rename", "items": [{"path": "docs/report.pdf", "new_name": "20250101_報告書.pdf"}]}, headers=origin).json
    env["jobs"].wait()
    assert (root / "docs" / "20250101_報告書.pdf").read_text() == "keep" and not (root / "docs" / "report.pdf").exists()
    log_dir = env["settings"].decision_log.log_dir
    events = [_json.loads(l) for f in log_dir.glob("*.jsonl") for l in f.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["source"] == "browser-rename" and events[-1]["final"]["name"] == "20250101_報告書.pdf"
    run_job(env, f"/api/plans/{r['plan_id']}/undo")
    assert (root / "docs" / "report.pdf").exists()

    # フォルダの名前を変えると、中のタグも追従する
    app.post_json("/api/fileops", {"root": "data", "op": "rename", "items": [{"path": "projects/old", "new_name": "2025_案件"}]}, headers=origin)
    env["jobs"].wait()
    assert (root / "projects" / "2025_案件" / "memo.txt").exists()
    data = app.get("/api/browse", {"root": "data", "path": "projects/2025_案件"}).json
    assert data["entries"][0]["tags"] == ["種別:メモ"]

    # 断るもの: 同じ名前が既にある・使えない名前・ルートそのもの
    app.post_json("/api/fileops", {"root": "data", "op": "rename", "items": [{"path": "docs/report.pdf", "new_name": "Thumbs.db"}]}, headers=origin, status=409)
    app.post_json("/api/fileops", {"root": "data", "op": "rename", "items": [{"path": "docs/report.pdf", "new_name": "a?.pdf"}]}, headers=origin, status=400)
    app.post_json("/api/fileops", {"root": "data", "op": "rename", "items": [{"path": "docs/report.pdf", "new_name": "x/y.pdf"}]}, headers=origin, status=400)
    app.post_json("/api/fileops", {"root": "data", "op": "delete", "items": [{"path": ""}]}, headers=origin, status=400)

    # 削除は隔離フォルダへ移すだけで、取り消せば戻る
    r = app.post_json("/api/fileops", {"root": "data", "op": "delete", "items": [{"path": "projects"}, {"path": "docs/Thumbs.db"}]}, headers=origin).json
    env["jobs"].wait()
    assert not (root / "projects").exists() and not (root / "docs" / "Thumbs.db").exists()
    assert app.get(f"/api/plans/{r['plan_id']}").json["restorable"] == 2
    run_job(env, f"/api/plans/{r['plan_id']}/undo")
    assert (root / "projects" / "2025_案件" / "memo.txt").exists() and (root / "docs" / "Thumbs.db").exists()


def test_office_preview_api(env: dict) -> None:
    import zipfile

    root = env["root"]
    with zipfile.ZipFile(root / "docs" / "memo.docx", "w") as z:
        z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>本文</w:t></w:r></w:p></w:body></w:document>')
    data = env["app"].get("/api/office", {"root": "data", "path": "docs/memo.docx"}).json
    assert data["kind"] == "docx" and data["blocks"][0]["runs"][0]["t"] == "本文"
    env["app"].get("/api/office", {"root": "data", "path": "docs/report.pdf"}, status=400)
    env["app"].get("/api/office", {"root": "data", "path": "../x.docx"}, status=400)
    assert env["app"].get("/api/state").json["office_pdf"] in (True, False)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg が無い")
def test_media_api(env: dict) -> None:
    import subprocess

    root = env["root"]
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x48:rate=10", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(root / "docs" / "clip.flv")],
        check=True,
    )
    q = {"root": "data", "path": "docs/clip.flv"}
    deadline = time.time() + 30
    while time.time() < deadline:
        st = env["app"].get("/api/media/status", q).json
        if st["state"] != "running":
            break
        time.sleep(0.05)
    assert st["state"] == "done" and st["mode"] == "copy"
    res = env["app"].get("/api/media", q, headers={"Range": "bytes=0-99"})
    assert res.status_int == 206 and res.headers["Content-Type"] == "video/mp4"
    env["app"].get("/api/media/status", {"root": "data", "path": "docs/report.pdf"}, status=400)


def test_tag_management_and_search(env: dict) -> None:
    app, root = env["app"], env["root"]
    origin = {"Origin": f"http://{HOST}"}
    write(root / "docs" / "sub" / "memo.txt", "m")
    run_job(env, "/api/roots/data/scan")
    app.post_json("/api/tags", {"root": "data", "paths": ["docs"], "tag": "area:docs"}, headers=origin)
    app.post_json("/api/tags", {"root": "data", "paths": ["docs/report.pdf"], "tag": "type:report"}, headers=origin)
    app.post_json("/api/tags", {"root": "data", "paths": ["docs/sub"], "tag": "draft"}, headers=origin)

    def search(**q: object) -> list[str]:
        return [x["path"] for x in app.get("/api/tags/search", {"root": "data", **q}).json["items"]]

    assert search(tag=["area:docs", "type:report"]) == ["docs/report.pdf"]
    assert search(tag="area:docs", **{"not": "draft"}) == ["docs/Thumbs.db", "docs/report.pdf"]
    assert search(tag="area:docs", q="memo") == ["docs/sub/memo.txt"]
    assert "docs" in search(tag="area:docs", folders="1")
    item = app.get("/api/tags/search", {"root": "data", "tag": "type:report"}).json["items"][0]
    assert item["tags"]["direct"][0]["tag"] == "type:report" and item["tags"]["inherited"][0]["tag"] == "area:docs"
    app.get("/api/tags/search", {"root": "data"}, status=400)

    # 日本語のタグでも探せる（複数の値をとる問い合わせも UTF-8 として読む）
    app.post_json("/api/tags", {"root": "data", "paths": ["docs/report.pdf"], "tag": "種類:報告書"}, headers=origin)
    assert search(tag="種類:報告書") == ["docs/report.pdf"]
    assert search(tag="area:docs", **{"not": "種類:報告書"}) == ["docs/Thumbs.db", "docs/sub/memo.txt"]
    app.post_json("/api/tags", {"root": "data", "paths": ["docs/report.pdf"], "tag": "種類:報告書", "op": "remove"}, headers=origin)

    assert app.post_json("/api/tags/rename", {"from": "type:report", "to": "type:document"}, headers=origin).json["changed"] == 1
    assert search(tag="type:document") == ["docs/report.pdf"]
    assert app.post_json("/api/tags/delete", {"tag": "draft"}, headers=origin).json["changed"] == 1
    assert {t["tag"] for t in app.get("/api/tags").json["tags"]} == {"area:docs", "type:document"}


def test_mkdir_and_move_from_the_browser(env: dict) -> None:
    app, root = env["app"], env["root"]
    origin = {"Origin": f"http://{HOST}"}
    write(root / "inbox" / "a.pdf", "a")
    write(root / "inbox" / "sub" / "b.pdf", "b")
    run_job(env, "/api/roots/data/scan")
    app.post_json("/api/tags", {"root": "data", "paths": ["inbox/sub"], "tag": "keep"}, headers=origin)

    # フォルダの作成
    assert app.post_json("/api/fileops", {"root": "data", "op": "mkdir", "parent": "docs", "name": "2025"}, headers=origin).json["created"] == "docs/2025"
    assert (root / "docs" / "2025").is_dir()
    app.post_json("/api/fileops", {"root": "data", "op": "mkdir", "parent": "docs", "name": "2025"}, headers=origin, status=409)
    app.post_json("/api/fileops", {"root": "data", "op": "mkdir", "parent": "docs", "name": "a?b"}, headers=origin, status=400)
    app.post_json("/api/fileops", {"root": "data", "op": "mkdir", "parent": "nope", "name": "x"}, headers=origin, status=404)

    # 移動（選んだフォルダの中のものは重ねて扱わない。まだ無い移動先は作る）
    items = [{"path": "inbox/a.pdf"}, {"path": "inbox/sub"}, {"path": "inbox/sub/b.pdf"}]
    r = app.post_json("/api/fileops", {"root": "data", "op": "move", "items": items, "dest_dir": "docs/2025/new"}, headers=origin).json
    env["jobs"].wait()
    assert r["ops"] == 2
    assert (root / "docs" / "2025" / "new" / "a.pdf").exists() and (root / "docs" / "2025" / "new" / "sub" / "b.pdf").exists()
    entries = {e["name"]: e for e in app.get("/api/browse", {"root": "data", "path": "docs/2025/new"}).json["entries"]}
    assert entries["sub"]["tags"] == ["keep"]  # タグも追従

    # 断るもの: それ自身の中へ・同じ名前が既にある
    app.post_json("/api/fileops", {"root": "data", "op": "move", "items": [{"path": "docs/2025"}], "dest_dir": "docs/2025/new"}, headers=origin, status=400)
    write(root / "a.pdf", "other")
    app.post_json("/api/fileops", {"root": "data", "op": "move", "items": [{"path": "docs/2025/new/a.pdf"}], "dest_dir": ""}, headers=origin, status=409)

    # 取り消すと元の場所へ戻る
    run_job(env, f"/api/plans/{r['plan_id']}/undo")
    assert (root / "inbox" / "a.pdf").exists() and (root / "inbox" / "sub" / "b.pdf").exists()


# ---------- 自動で最新にする（F-SC-3・P4） ----------


def test_auto_refresh_decides_when_to_refresh() -> None:
    """間隔が過ぎたら、同期が終わったら最新にする。同期中・未走査・別のジョブが動いているときは待つ。"""
    from tag_keeper.jobs import AutoRefresh, RootTiming

    now = [10_000.0]
    timings: list[RootTiming] = []
    started: list[str] = []
    free = [True]

    def start(name: str) -> bool:
        if free[0]:
            started.append(name)
        return free[0]

    auto = AutoRefresh(lambda: timings, start, retry=600, clock=lambda: now[0])

    # 間隔（1時間）が過ぎていなければ、何もしない
    timings[:] = [RootTiming("a", 3600, True, last_scan=now[0] - 100)]
    assert auto.tick() is None
    # 同期が終わった（前回の走査より後）なら、最新にする
    timings[:] = [RootTiming("a", 3600, True, last_scan=now[0] - 100, sync_finished=now[0] - 10)]
    assert auto.tick() == "a" and started == ["a"]
    # 始めた直後は、走査がまだ記録されていなくても、retry の間は試し直さない
    assert auto.tick() is None
    # 同期が動いている間は、間隔が過ぎていても待つ
    now[0] += 7200
    timings[:] = [RootTiming("a", 3600, True, last_scan=now[0] - 7200, sync_busy=True)]
    assert auto.tick() is None
    # 別のジョブが動いていれば始めず、空いたら始める
    timings[:] = [RootTiming("a", 3600, True, last_scan=now[0] - 7200)]
    free[0] = False
    assert auto.tick() is None
    free[0] = True
    assert auto.tick() == "a"
    # 一度も走査していないルートは、自動では始めない（初回は全件のハッシュ計算になるため）
    now[0] += 7200
    timings[:] = [RootTiming("new", 3600, True, last_scan=None, sync_finished=now[0])]
    assert auto.tick() is None
    # 間隔が 0 で同期の後も無効なら、何もしない
    timings[:] = [RootTiming("b", 0, False, last_scan=0.0, sync_finished=now[0])]
    assert auto.tick() is None


def test_auto_refresh_runs_a_quiet_refresh_job(env: dict, tmp_path: Path) -> None:
    """自動で始めたジョブは auto の印が付き、走査とハッシュ計算と内容ハッシュの結び付けまで行う。"""
    from tag_keeper.web.server import root_timings, start_auto_refresh

    run_job(env, "/api/roots/data/refresh")
    (t,) = root_timings(env["settings"])
    assert (t.name, t.interval, t.after_sync, t.sync_busy) == ("data", 3600.0, True, False)
    assert t.last_scan is not None

    assert start_auto_refresh(env["settings"], env["jobs"], "data") is True
    job = env["jobs"].wait()
    assert job is not None and job.status == "done" and job.auto
    data = env["app"].get("/api/jobs/current").json["job"]
    assert data["auto"] is True and data["title"] == "自動で最新にする: data"
    assert set(data["result"]) == {"scan", "hash", "relink"}
    # 別のジョブが動いていれば始めない
    env["jobs"].start("test", "長い処理", lambda rep: time.sleep(0.3))
    try:
        assert start_auto_refresh(env["settings"], env["jobs"], "data") is False
    finally:
        env["jobs"].wait()
    state = env["app"].get("/api/state").json
    assert state["auto_refresh"] == 60 and state["roots"][0]["held_files"] == 0


# ---------- バグ調査（v0.9.1）で見つかったものの回帰テスト ----------


def test_history_apis_do_not_leave_the_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """過去の版の復元・過去の時点の一覧・版の履歴も、ルートの外（リンクの先・ルートの親）を読み書きしない。"""
    import datetime as dt

    from btrfs_timeline.core.mounts import MountPoint
    from btrfs_timeline.core.snapshots import Snapshot

    from tag_keeper import history

    mnt = tmp_path / "mnt"
    root, outside = mnt / "root", mnt / "outside"
    write(root / "a.txt", "a-now")
    write(outside / "secret.txt", "SECRET-NOW")
    (root / "link").symlink_to(outside)
    snap_root = mnt / ".snapshots" / "1" / "snapshot"
    write(snap_root / "root" / "a.txt", "a-then")
    write(snap_root / "outside" / "secret.txt", "SECRET-THEN")
    mount = MountPoint(mount_point=str(mnt), fs_type="btrfs", device="fake", subvol="/", root="/")
    snap = Snapshot(id="1", root=str(snap_root), taken_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), description="", layout="snapper")
    monkeypatch.setattr(history, "snapshots_for", lambda path: (mount, [snap]))
    cfg = write(tmp_path / "config.toml", f'[[roots]]\nname = "r"\npath = "{root}"\n[tags]\ndir = "{tmp_path / "tags"}"\n')
    jobs = JobManager(tmp_path / "logs")
    app = TestApp(create_app(Settings(cfg, tmp_path / "data" / "catalog.db"), jobs=jobs), extra_environ={"HTTP_HOST": HOST})
    origin = {"Origin": f"http://{HOST}"}
    app.post_json("/api/roots/r/scan", {}, headers=origin)
    jobs.wait()

    # 外の版をルートの中へ複製する・外へ書き込む・ルートの親へ書き込む、はどれも断る
    for body in (
        {"path": "a.txt", "version_path": "link/secret.txt"},
        {"path": "link/secret.txt", "version_path": "a.txt"},
        {"path": "", "version_path": "a.txt"},
    ):
        app.post_json("/api/restore", {"root": "r", "snapshot": "1", **body}, headers=origin, status=400)
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]
    assert sorted(p.name for p in mnt.iterdir() if p.name.startswith("root")) == ["root"]
    app.get("/api/browse", {"root": "r", "path": "link", "snapshot": "1"}, status=400)
    app.get("/api/history", {"root": "r", "path": "link/secret.txt"}, status=400)
    # ルートの中なら復元できる（今のファイルの隣に日時付きの別名で）
    restored = app.post_json("/api/restore", {"root": "r", "path": "a.txt", "snapshot": "1"}, headers=origin).json["restored"]
    assert (root / restored).read_text() == "a-then"


def test_plans_of_a_root_with_spaces_in_its_name_can_be_undone_from_the_web(tmp_path: Path, root: Path) -> None:
    """ルート名に空白などがあっても、ファイルブラウザで作ったプランを画面から開けて取り消せる。"""
    write(root / "a.txt", "a")
    write(root / "keep.txt", "keep")
    cfg = write(tmp_path / "config.toml", f'[[roots]]\nname = "My Drive"\npath = "{root}"\n[plan]\nquarantine_dir = "{tmp_path / "q"}"\n[tags]\ndir = "{tmp_path / "tags"}"\n')
    jobs = JobManager(tmp_path / "logs")
    app = TestApp(create_app(Settings(cfg, tmp_path / "data" / "catalog.db"), jobs=jobs), extra_environ={"HTTP_HOST": HOST})
    origin = {"Origin": f"http://{HOST}"}
    app.post_json("/api/roots/My Drive/scan", {}, headers=origin)
    jobs.wait()
    res = app.post_json("/api/fileops", {"root": "My Drive", "op": "delete", "items": [{"path": "a.txt"}]}, headers=origin).json
    jobs.wait()
    assert "My_Drive" in res["plan_id"] and not (root / "a.txt").exists()
    assert app.get(f"/api/plans/{res['plan_id']}").json["info"]["state"] == "applied"
    app.post_json(f"/api/plans/{res['plan_id']}/undo", {}, headers=origin)
    jobs.wait()
    assert (root / "a.txt").exists()


def test_organize_plan_refuses_destinations_outside_the_root(env: dict, tmp_path: Path) -> None:
    root = env["root"]
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    run_job(env, "/api/roots/data/refresh")
    env["app"].post_json(
        "/api/organize/data/plan", {"items": [{"path": "docs/report.pdf", "dest": "link/report.pdf"}]},
        headers={"Origin": f"http://{HOST}"}, status=400,
    )
    assert (root / "docs" / "report.pdf").exists() and list(outside.iterdir()) == []


def test_names_that_differ_only_in_case_are_refused(env: dict) -> None:
    """OneDrive は大文字・小文字を区別しないので、それだけが違う名前は同じ名前として断る。"""
    app, root = env["app"], env["root"]
    origin = {"Origin": f"http://{HOST}"}
    write(root / "A" / "Report.pdf", "mine")
    write(root / "B" / "report.pdf", "theirs")
    write(root / "C" / "draft.pdf", "draft")
    write(root / "C" / "memo.pdf", "memo")
    write(root / "x.pdf", "x")
    write(root / "y.pdf", "y")
    run_job(env, "/api/roots/data/refresh")
    app.post_json("/api/fileops", {"root": "data", "op": "move", "items": [{"path": "A/Report.pdf"}], "dest_dir": "B"}, headers=origin, status=409)
    app.post_json("/api/fileops", {"root": "data", "op": "rename", "items": [{"path": "C/draft.pdf", "new_name": "MEMO.pdf"}]}, headers=origin, status=409)
    app.post_json(
        "/api/organize/data/plan", {"items": [{"path": "x.pdf", "dest": "D/Same.pdf"}, {"path": "y.pdf", "dest": "D/same.pdf"}]},
        headers=origin, status=409,
    )
    app.post_json("/api/fileops", {"root": "data", "op": "mkdir", "parent": "", "name": "b"}, headers=origin, status=409)
    # 自分自身の大文字・小文字だけの改名は許す
    res = app.post_json("/api/fileops", {"root": "data", "op": "rename", "items": [{"path": "C/draft.pdf", "new_name": "Draft.pdf"}]}, headers=origin).json
    env["jobs"].wait()
    assert res["ops"] == 1 and (root / "C" / "Draft.pdf").exists()


def test_purge_from_the_web(env: dict) -> None:
    """プランの隔離を、画面から空にできる。空にした後は取り消せず、もう一度は空にできない。"""
    app = env["app"]
    run_job(env, "/api/roots/data/refresh")
    plan_id = run_job(env, "/api/plans", {"root": "data", "categories": ["os-junk"]})["result"]["plan_id"]
    run_job(env, f"/api/plans/{plan_id}/apply")
    plan = app.get(f"/api/plans/{plan_id}").json
    assert plan["purgeable"] == 1 and plan["purgeable_size"] == len("junk")
    job = run_job(env, f"/api/plans/{plan_id}/purge")
    assert job["result"]["removed"] == 1 and job["title"] == f"隔離を空にする: {plan_id}"
    plan = app.get(f"/api/plans/{plan_id}").json
    assert plan["info"]["state"] == "purged" and plan["restorable"] == 0 and plan["purgeable"] == 0
    assert plan["ops"][0]["outcome"]["event"] == "purge"
    app.post_json(f"/api/plans/{plan_id}/purge", {}, headers={"Origin": f"http://{HOST}"}, status=409)


# ---------- タグの辞書・属性・検索の絞り込み・保存した検索（0.12） ----------


def test_search_filters_attributes_dictionary_and_saved_searches(env: dict) -> None:
    import os

    app, root = env["app"], env["root"]
    origin = {"Origin": f"http://{HOST}"}
    write(root / "case" / "a.pdf", "A" * 100)
    write(root / "case" / "copy-of-a.pdf", "A" * 100)  # 同じ内容
    write(root / "case" / "big.bin", "B" * 5000)
    write(root / "other" / "old.txt", "old")
    os.utime(root / "other" / "old.txt", (1_600_000_000, 1_600_000_000))  # 2020-09-13
    write(root / "gone.txt", "will be deleted")
    run_job(env, "/api/roots/data/refresh")

    def find(**params: object) -> list[str]:
        return [x["path"] for x in app.get("/api/search", params).json["items"]]

    # 場所・大きさ・日付
    assert find(under="case") == ["case/a.pdf", "case/big.bin", "case/copy-of-a.pdf"]
    assert find(under="case", min_size=1000) == ["case/big.bin"]
    assert find(under="case", max_size=200) == ["case/a.pdf", "case/copy-of-a.pdf"]
    assert find(before="2021-01-01") == ["other/old.txt"]
    assert "other/old.txt" not in find(after="2021-01-01")
    app.get("/api/search", {"after": "2021/01/01"}, status=400)
    # 同じ内容をまとめる
    res = app.get("/api/search", {"under": "case", "collapse": "1"}).json
    assert [(x["path"], x["copies"]) for x in res["items"]] == [("case/a.pdf", ["case/copy-of-a.pdf"]), ("case/big.bin", [])]

    # 属性: 定義して、フォルダに付けた値は配下が継承する
    app.post_json("/api/attrs/defs", {"name": "対応状態", "type": "choice", "choices": "未対応, 対応中、対応済み"}, headers=origin)
    app.post_json("/api/attrs/defs", {"name": "金額", "type": "number"}, headers=origin)
    app.post_json("/api/attrs", {"root": "data", "paths": ["case"], "name": "対応状態", "value": "対応中"}, headers=origin)
    app.post_json("/api/attrs", {"root": "data", "paths": ["case/big.bin"], "name": "金額", "value": "20000"}, headers=origin)
    app.post_json("/api/attrs", {"root": "data", "paths": ["case/a.pdf"], "name": "対応状態", "value": "保留"}, headers=origin, status=400)
    assert find(attr="対応状態=対応中") == ["case/a.pdf", "case/big.bin", "case/copy-of-a.pdf"]
    assert find(attr=["対応状態=対応中", "金額>=10000"]) == ["case/big.bin"]
    assert find(attr="金額") == ["case/big.bin"]
    info = app.get("/api/info", {"root": "data", "path": "case/a.pdf"}).json
    assert info["attrs"]["inherited"] == [{"name": "対応状態", "value": "対応中", "from": "case"}]
    assert {d["name"] for d in info["attr_defs"]} == {"対応状態", "金額"}

    # タグの辞書: 別名で探しても、親のタグで探しても見つかる
    app.post_json("/api/tags", {"root": "data", "paths": ["case/a.pdf"], "tag": "種別:請求書", "op": "add"}, headers=origin)
    app.post_json("/api/tags/alias", {"tag": "種別:請求書", "alias": "種別:invoice"}, headers=origin)
    app.post_json("/api/tags/parent", {"tag": "種別:請求書", "parent": "種別:経理"}, headers=origin)
    app.post_json("/api/tags/parent", {"tag": "種別:経理", "parent": "種別:請求書"}, headers=origin, status=400)
    assert find(tag="種別:invoice") == ["case/a.pdf"]
    assert find(tag="種別:経理") == ["case/a.pdf"]
    tags = {t["tag"]: t for t in app.get("/api/tags").json["tags"]}
    assert tags["種別:請求書"]["aliases"] == ["種別:invoice"] and tags["種別:経理"]["children"] == ["種別:請求書"]

    # 今は無いもの（消えたパス）も探せる
    (root / "gone.txt").unlink()
    run_job(env, "/api/roots/data/refresh")
    assert find(q="gone") == []
    res = app.get("/api/search", {"q": "gone", "gone": "1"}).json
    assert [x["path"] for x in res["items"]] == ["gone.txt"] and res["items"][0]["gone_at"]

    # 検索の条件を保存して、一覧に出す
    app.post_json("/api/searches", {"name": "対応中", "query": "attr=対応状態%3D対応中"}, headers=origin)
    assert app.get("/api/tags").json["searches"] == [{"name": "対応中", "query": "attr=対応状態%3D対応中"}]
    app.post_json("/api/searches", {"name": "対応中", "op": "delete"}, headers=origin)
    assert app.get("/api/searches").json["searches"] == []


# ---------- 移動の確認（F-ID-2）・関係・書き出し（0.13） ----------


def test_relink_confirmation_and_relations(env: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    import datetime as dt
    import shutil

    # 同じ秒に2回走査すると、消えた側の最後の日時と新しい側の初めての日時が同じになり、
    # 「どちらが先か」を判定できない（tag_keeper.scan の時計を1分ずつ進める）
    import tag_keeper.scan as scan_mod

    base = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
    ticks = [0]

    def fake_now() -> str:
        ticks[0] += 1
        return (base + dt.timedelta(minutes=ticks[0])).isoformat(timespec="seconds")

    monkeypatch.setattr(scan_mod, "utcnow", fake_now)

    app, root = env["app"], env["root"]
    origin = {"Origin": f"http://{HOST}"}
    # 同じ内容のファイルが2か所から消えて1つ現れた（どれから動いたか、自動では決められない）
    write(root / "x" / "f.pdf", "same content")
    write(root / "y" / "f.pdf", "same content")
    write(root / "keep.txt", "keep")
    run_job(env, "/api/roots/data/refresh")
    # 関係とタグを、消える側に付けておく（結び付けたときに追従するはずのもの）
    app.post_json("/api/relations", {"root": "data", "from": "x/f.pdf", "to": "keep.txt", "kind": "前の版"}, headers=origin)
    app.post_json("/api/tags", {"root": "data", "paths": ["x/f.pdf"], "tag": "種別:請求書", "op": "add"}, headers=origin)
    (root / "z").mkdir()
    shutil.copy2(root / "x" / "f.pdf", root / "z" / "f.pdf")
    (root / "x" / "f.pdf").unlink()
    (root / "y" / "f.pdf").unlink()
    run_job(env, "/api/roots/data/refresh")
    assert app.get("/api/state").json["roots"][0]["relink_pending"] == 1
    items = app.get("/api/relink/data").json["items"]
    assert len(items) == 1 and items[0]["path"] == "z/f.pdf"
    assert sorted(c["path"] for c in items[0]["candidates"]) == ["x/f.pdf", "y/f.pdf"]
    new_id = items[0]["id"]
    old_id = next(c["id"] for c in items[0]["candidates"] if c["path"] == "x/f.pdf")
    res = app.post_json("/api/relink/data", {"new_id": new_id, "old_id": old_id}, headers=origin).json
    assert res == {"from": "x/f.pdf", "to": "z/f.pdf", "tag_moves": 1}
    assert app.get("/api/tags/search", {"root": "data", "tag": "種別:請求書"}).json["items"][0]["path"] == "z/f.pdf"
    assert app.get("/api/info", {"root": "data", "path": "z/f.pdf"}).json["relations"]["outgoing"] == [{"kind": "前の版", "path": "keep.txt"}]
    assert app.get("/api/relink/data").json["items"] == []
    assert app.get("/api/state").json["roots"][0]["relink_pending"] == 0

    # 一対一に決まる移動は、確認待ちにならずに自動で結び付く
    write(root / "z2" / "a.txt", "gone one")
    run_job(env, "/api/roots/data/refresh")
    (root / "z2" / "a.txt").unlink()
    (root / "z2" / "b.txt").write_text("gone one")
    run_job(env, "/api/roots/data/refresh")
    assert app.get("/api/relink/data").json["items"] == []
    # 候補が複数あるものを「どれでもない（新しいファイル）」と記録すると、以後は出ない
    write(root / "p" / "c.txt", "twin")
    write(root / "q" / "c.txt", "twin")
    run_job(env, "/api/roots/data/refresh")
    (root / "p" / "c.txt").unlink()
    (root / "q" / "c.txt").unlink()
    write(root / "r" / "c.txt", "twin")
    run_job(env, "/api/roots/data/refresh")
    items = app.get("/api/relink/data").json["items"]
    assert len(items) == 1
    app.post_json("/api/relink/data", {"new_id": items[0]["id"], "skip": True}, headers=origin)
    assert app.get("/api/relink/data").json["items"] == []


def test_export_from_the_web(env: dict) -> None:
    app = env["app"]
    origin = {"Origin": f"http://{HOST}"}
    app.post_json("/api/tags", {"root": "data", "paths": ["docs/report.pdf"], "tag": "種別:請求書", "op": "add"}, headers=origin)
    res = app.get("/api/export")
    assert res.content_type == "application/x-ndjson"
    import json

    events = [json.loads(line) for line in res.text.splitlines()]
    assert events == [{"op": "add", "root": "data", "path": "docs/report.pdf", "tag": "種別:請求書", "source": "manual"}]


def test_cli_tag_attr_relate_export_import(tmp_path: Path, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tag_keeper.cli import main

    write(root / "case" / "v2.docx", "new")
    write(root / "case" / "v1.docx", "old")
    write(root / "absolute.txt", "abs")
    cfg = write(tmp_path / "config.toml", f'[[roots]]\nname = "data"\npath = "{root}"\n[tags]\ndir = "{tmp_path / "tags"}"\n')
    base = ["--config", str(cfg), "--db", str(tmp_path / "catalog.db")]
    # タグ・別名・親子
    assert main([*base, "tag", "data", "case/v2.docx", "--add", "種別:見積"]) == 0
    assert main([*base, "alias", "種別:見積", "種別:estimate"]) == 0
    assert main([*base, "tag", "data", str(root / "absolute.txt"), "--add", "種別:estimate"]) == 0  # 絶対パス・別名
    assert main([*base, "parent", "種別:見積", "種別:書類"]) == 0
    assert main([*base, "tags"]) == 0
    out = capsys.readouterr().out
    assert "種別:見積" in out and "種別:estimate" in out and "種別:書類" in out
    # 属性の定義と値
    assert main([*base, "attr", "define", "対応状態", "choice", "--choices", "未対応, 対応済み"]) == 0
    assert main([*base, "attr", "set", "data", "対応状態", "対応済み", "case"]) == 0
    assert main([*base, "attr", "set", "data", "対応状態", "保留", "case"]) == 2  # 選択肢に無い
    assert main([*base, "attr", "set", "data", "未定義", "1", "case"]) == 2
    # 関係
    assert main([*base, "relate", "data", "case/v2.docx", "case/v1.docx", "--kind", "前の版"]) == 0
    assert main([*base, "relate", "data", "case/v2.docx", "missing.txt"]) == 2  # 無いファイル
    # 書き出しと読み込み
    out_file = tmp_path / "out.jsonl"
    assert main([*base, "export", "-o", str(out_file)]) == 0
    import json

    events = [json.loads(line) for line in out_file.read_text(encoding="utf-8").splitlines()]
    assert {ev["op"] for ev in events} == {"add", "attr-def", "attr-set", "alias", "parent", "relate"}
    other = write(tmp_path / "other.toml", f'[tags]\ndir = "{tmp_path / "other-tags"}"\n')
    assert main(["--config", str(other), "--db", str(tmp_path / "catalog.db"), "import", str(out_file)]) == 0
    from tag_keeper.tags import TagStore

    store = TagStore(tmp_path / "other-tags")
    assert store.relations_of("data", "case/v1.docx")["incoming"][0]["path"] == "case/v2.docx"
    assert main([*base, "tag", "data", "case/v2.docx", "--remove", "種別:見積"]) == 0
    assert main([*base, "attr", "unset", "data", "対応状態", "case"]) == 0
    assert main([*base, "relate", "data", "case/v2.docx", "case/v1.docx", "--kind", "前の版", "--remove"]) == 0
