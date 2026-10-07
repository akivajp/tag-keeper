"""Web の画面のサーバー（API・アクセス制御・ジョブ）のテスト。"""

from __future__ import annotations

import base64
import time
from pathlib import Path

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
