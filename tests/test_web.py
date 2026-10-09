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
    assert events[-1]["source"] == "browser" and events[-1]["final"]["name"] == "20250101_報告書.pdf"
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

    assert app.post_json("/api/tags/rename", {"from": "type:report", "to": "type:document"}, headers=origin).json["changed"] == 1
    assert search(tag="type:document") == ["docs/report.pdf"]
    assert app.post_json("/api/tags/delete", {"tag": "draft"}, headers=origin).json["changed"] == 1
    assert {t["tag"] for t in app.get("/api/tags").json["tags"]} == {"area:docs", "type:document"}
