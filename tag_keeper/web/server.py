"""Web の画面のサーバー（bottle）。

画面はブラウザ側（static/ の HTML と JavaScript）で組み立て、ここは JSON の API と静的ファイルだけを返す。
時間のかかる処理（走査・ハッシュ・実行・取り消しなど）はジョブとして裏で動かし、
画面はジョブの進み具合を1秒ごとに読み出して表示する（tag_keeper.jobs）。

API の一覧:

    GET  /api/state                      ルート・プラン・動いているジョブの概要
    GET  /api/report/<root>              衛生レポート
    GET  /api/plans/<id>                 プランの中身と実行の状態
    GET  /api/jobs/current               動いている（または最後の）ジョブ
    POST /api/roots/<root>/<scan|hash|refresh>
    POST /api/plans                      プランを作る {root, categories, include_review}
    POST /api/plans/<id>/skips           除外するものを保存する {skip: [path, ...]}
    POST /api/plans/<id>/<check|apply|undo|delete>
    POST /api/plans/<id>/sync/<retry|resume>
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import bottle

from tag_keeper import __version__, history, organize, rules
from tag_keeper.catalog import connect, ensure_root, last_scan, utcnow
from tag_keeper.config import (
    Config,
    ConfigError,
    RootConfig,
    default_config_path,
    load_config,
)
from tag_keeper.hashing import hash_root
from tag_keeper.history import HistoryError
from tag_keeper.jobs import JobBusyError, JobManager, JobReporter
from tag_keeper.organize import OllamaError
from tag_keeper.plan import (
    ACTION_MOVE,
    CAT_MOVE,
    Plan,
    PlanError,
    PlanOp,
    Snapper,
    SnapshotError,
    apply_plan,
    build_plan,
    check_op,
    fingerprint,
    force_resume_sync,
    journal_path,
    list_plans,
    load_plan,
    pending_restores,
    plan_info,
    read_journal,
    retry_sync,
    set_skips,
    undo_plan,
    write_plan,
)
from tag_keeper.report import build_report
from tag_keeper.scan import RootUnavailableError, scan_root
from tag_keeper.syncguard import SyncError, SyncGuard, make_guard
from tag_keeper.tags import TagError, TagStore
from tag_keeper.web.auth import (
    Credentials,
    host_allowed,
    is_loopback,
    is_same_origin,
    parse_basic_header,
)
from tag_keeper.web.files import (
    PathError,
    clean_relpath,
    list_live,
    resolve_within,
    serve_kind,
)

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
_PLAN_ID = re.compile(r"[\w.-]+")
# レポートで画面に送る重複の組の数（多すぎると画面が重くなる）
DUPLICATE_LIMIT = 300


class Settings:
    """サーバーの設定（設定ファイルとカタログの場所、そこから決まる置き場所）。"""

    def __init__(self, config_path: Path | None, db_path: Path) -> None:
        self.config_path = config_path
        self.db_path = db_path
        data_dir = db_path.parent
        self.plans_dir = data_dir / "plans"
        self.journal_dir = data_dir / "journal"
        self.job_log_dir = data_dir / "logs" / "jobs"

        self._tag_stores: dict[Path, TagStore] = {}

    def config(self) -> Config:
        """設定を読み直す（編集をサーバーの再起動なしで反映するため、リクエストのたびに読む）。"""
        return load_config(self.config_path)

    def tags(self, config: Config | None = None) -> TagStore:
        """タグ（ログの置き場所ごとに1つを使い回す）。"""
        tags_dir = (config or self.config()).tags.dir
        if tags_dir not in self._tag_stores:
            self._tag_stores[tags_dir] = TagStore(tags_dir)
        return self._tag_stores[tags_dir]


# 同期クライアントとの連携の窓口は、設定の組ごとに使い回す（onedrive の設定の読み出しに 0.5 秒ほどかかるため）
_guards: dict[tuple[str, str, str], SyncGuard | None] = {}


def _guard(rc: RootConfig | None) -> SyncGuard | None:
    """ルートの設定から、同期クライアントとの連携の窓口を作る（作ったものは使い回す）。"""
    if rc is None:
        return None
    key = (rc.sync_client, rc.sync_service, rc.sync_guard)
    if key not in _guards:
        _guards[key] = make_guard(*key)
    return _guards[key]


def create_app(
    settings: Settings,
    credentials: Credentials | None = None,
    bind_host: str = "127.0.0.1",
    jobs: JobManager | None = None,
) -> bottle.Bottle:
    """bottle のアプリを作る。

    Args:
        settings: 設定ファイルとカタログの場所。
        credentials: BASIC 認証の資格情報。None なら認証しない。
        bind_host: 待ち受けるアドレス（Host ヘッダの検証に使う）。
        jobs: ジョブの管理（テストで差し替えられるように引数にしてある）。
    """
    app = bottle.Bottle()
    jobs = jobs or JobManager(settings.job_log_dir)

    # --- アクセス制御と共通の応答ヘッダ ---

    @app.hook("before_request")
    def guard_request() -> None:
        # 認証がある場合は、ブラウザが資格情報を別のオリジンへ送らないので、Host の検証は要らない
        # （Tailscale の MagicDNS 名など、アドレス以外の名前で開けるように）
        if credentials is None and not host_allowed(bottle.request.get_header("Host"), bind_host):
            raise bottle.HTTPResponse(status=403, body="Host ヘッダが不正です")
        if credentials is not None:
            user, password = parse_basic_header(bottle.request.get_header("Authorization"))
            if not credentials.verify(user, password):
                raise bottle.HTTPResponse(
                    status=401,
                    headers={"WWW-Authenticate": 'Basic realm="tag-keeper", charset="UTF-8"'},
                    body="認証が必要です",
                )
        if bottle.request.method == "POST" and not is_same_origin(
            bottle.request.get_header("Origin"), bottle.request.get_header("Host")
        ):
            raise bottle.HTTPResponse(status=403, body="別のサイトからの書き込みは受け付けません")

    @app.hook("after_request")
    def security_headers() -> None:
        # 同じオリジンの中でだけ枠に入れられる（PDF のプレビューのため。他のサイトからは入れられない）
        bottle.response.set_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'self'")
        bottle.response.set_header("X-Content-Type-Options", "nosniff")
        bottle.response.set_header("Referrer-Policy", "no-referrer")
        bottle.response.set_header("Cache-Control", "no-store")

    def fail(status: int, message: str) -> bottle.HTTPResponse:
        return bottle.HTTPResponse(
            status=status, body=bottle.json_dumps({"error": message}), headers={"Content-Type": "application/json"}
        )

    def body() -> dict[str, Any]:
        data = bottle.request.json
        return data if isinstance(data, dict) else {}

    def open_db() -> sqlite3.Connection:
        return connect(settings.db_path)

    def root_config(config: Config, name: str) -> RootConfig:
        rc = config.find_root(name)
        if rc is None:
            raise fail(404, f"設定ファイルにルートがありません: {name}")
        return rc

    def plan_file(plan_id: str) -> Path:
        if not _PLAN_ID.fullmatch(plan_id):
            raise fail(400, f"プラン ID が不正です: {plan_id}")
        path = settings.plans_dir / f"{plan_id}.toml"
        if not path.exists():
            raise fail(404, f"プランがありません: {plan_id}")
        return path

    def start(kind: str, title: str, fn: Any, target: str = "") -> dict[str, Any]:
        try:
            job = jobs.start(kind, title, fn, target=target)
        except JobBusyError as e:
            raise fail(409, str(e)) from e
        return job.to_dict()

    # --- 画面（静的ファイル） ---

    @app.route("/")
    def index() -> Any:
        return bottle.static_file("index.html", root=str(STATIC_DIR))

    @app.route("/favicon.ico")
    def favicon() -> Any:
        return bottle.HTTPResponse(status=204)

    @app.route("/static/<path:path>")
    def static(path: str) -> Any:
        return bottle.static_file(path, root=str(STATIC_DIR))

    # --- 読み出し ---

    @app.route("/api/state")
    def api_state() -> dict[str, Any]:
        config = settings.config()
        conn = open_db()
        try:
            roots = [_root_state(conn, rc) for rc in config.roots]
        finally:
            conn.close()
        plans = [_plan_summary(p) for p in list_plans(settings.plans_dir, settings.journal_dir)]
        job = jobs.latest()
        return {
            "version": __version__,
            "config_path": str(settings.config_path or default_config_path()),
            "quarantine_dir": str(config.plan.quarantine_dir),
            "snapper_config": config.plan.snapper_config,
            "roots": roots,
            "plans": plans,
            "job": job.to_dict() if job else None,
            "categories": [{"key": k, "label": v} for k, v in rules.CATEGORY_KEYS.items()],
        }

    @app.route("/api/report/<root>")
    def api_report(root: str) -> dict[str, Any]:
        config = settings.config()
        rc = root_config(config, root)
        conn = open_db()
        try:
            row = conn.execute("SELECT id FROM roots WHERE name = ?", (rc.name,)).fetchone()
            if row is None or last_scan(conn, row["id"]) is None:
                raise fail(409, f"まだ走査していません: {rc.name}")
            report = build_report(conn, row["id"], rc.name, config.hygiene)
        finally:
            conn.close()
        data = report.to_dict()
        data["duplicate_groups"] = len(report.duplicates)
        data["duplicate_waste"] = sum(g.waste for g in report.duplicates)
        data["duplicates"] = data["duplicates"][:DUPLICATE_LIMIT]
        data["category_order"] = rules.CATEGORY_ORDER
        data["category_keys"] = {v: k for k, v in rules.CATEGORY_KEYS.items()}
        return data

    @app.route("/api/plans/<plan_id>")
    def api_plan(plan_id: str) -> dict[str, Any]:
        path = plan_file(plan_id)
        plan = load_plan(path)
        info = plan_info(path, settings.journal_dir)
        journal = journal_path(settings.journal_dir, plan.id)
        outcomes: dict[str, dict[str, Any]] = {}
        events: list[dict[str, Any]] = []
        if journal.exists():
            for rec in read_journal(journal):
                ev = rec.get("event")
                if ev in ("quarantine", "skip", "restore", "restore-skip"):
                    outcomes[rec["path"]] = {"event": ev, "detail": rec.get("detail", "")}
                else:
                    events.append(rec)
        rc = settings.config().find_root(plan.root)
        guard = _guard(rc)
        sync: dict[str, Any] | None = None
        if guard is not None:
            try:
                sync = {"client": guard.name, "mode": rc.sync_guard if rc else "auto", **guard.status()}
            except SyncError as e:
                sync = {"client": guard.name, "error": str(e)}
        return {
            "info": _plan_summary(info),
            "root_path": str(plan.root_path),
            "ops": [
                {**asdict(op), **({"outcome": outcomes[op.path]} if op.path in outcomes else {})}
                for op in plan.ops
            ],
            "events": events,
            "restorable": len(pending_restores(journal)) if journal.exists() else 0,
            "sync": sync,
            "category_order": rules.CATEGORY_ORDER,
        }

    @app.route("/api/jobs/current")
    def api_job_current() -> dict[str, Any]:
        job = jobs.latest()
        return {"job": job.to_dict(lines=int(bottle.request.query.get("lines", 50))) if job else None}

    @app.route("/api/jobs/<job_id>")
    def api_job(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise fail(404, f"ジョブがありません: {job_id}")
        return {"job": job.to_dict(lines=int(bottle.request.query.get("lines", 200)))}

    # --- ルートに対する処理 ---

    @app.post("/api/roots/<root>/<action:re:scan|hash|refresh>")
    def api_root_action(root: str, action: str) -> dict[str, Any]:
        config = settings.config()
        rc = root_config(config, root)
        titles = {"scan": "走査", "hash": "ハッシュ計算", "refresh": "最新にする（走査とハッシュ計算）"}

        def run(rep: JobReporter) -> dict[str, Any]:
            conn = open_db()
            try:
                root_id = ensure_root(conn, rc.name, rc.path)
                out: dict[str, Any] = {}
                if action in ("scan", "refresh"):
                    out["scan"] = _run_scan(conn, root_id, rc, config, rep)
                    # ツールの外での移動・改名に、タグを追従させる（P4）
                    moved = settings.tags(config).follow_moves(rc.name, out["scan"].pop("moves"))
                    out["scan"]["tag_moves"] = moved
                if action in ("hash", "refresh"):
                    out["hash"] = _run_hash(conn, root_id, rc, rep)
                return out
            finally:
                conn.close()

        return start(action, f"{titles[action]}: {rc.name}", run, target=rc.name)

    # --- プラン ---

    @app.post("/api/plans")
    def api_plan_create() -> dict[str, Any]:
        data = body()
        config = settings.config()
        rc = root_config(config, str(data.get("root", "")))
        keys = data.get("categories") or []
        unknown = [k for k in keys if k not in rules.CATEGORY_KEYS]
        if unknown:
            raise fail(400, f"カテゴリが不明です: {', '.join(unknown)}")
        categories = [rules.CATEGORY_KEYS[k] for k in keys] or None
        include_review = bool(data.get("include_review", False))

        def run(rep: JobReporter) -> dict[str, Any]:
            conn = open_db()
            try:
                row = conn.execute("SELECT id FROM roots WHERE name = ?", (rc.name,)).fetchone()
                if row is None or last_scan(conn, row["id"]) is None:
                    raise PlanError(f"まだ走査していません: {rc.name}")
                rep.phase("レポートの作成")
                report = build_report(conn, row["id"], rc.name, config.hygiene)
            finally:
                conn.close()
            rep.phase("対象の状態の記録", total=len(report.findings))
            plan = build_plan(report, rc.name, rc.path, categories, include_review=include_review)
            rep.set_done(len(report.findings))
            write_plan(plan, settings.plans_dir / f"{plan.id}.toml")
            return {"plan_id": plan.id, "ops": len(plan.ops)}

        return start("plan", f"プランの作成: {rc.name}", run, target=rc.name)

    @app.post("/api/plans/<plan_id>/skips")
    def api_plan_skips(plan_id: str) -> dict[str, Any]:
        path = plan_file(plan_id)
        if plan_info(path, settings.journal_dir).state != "draft":
            raise fail(409, "実行したプランは編集できません")
        skip = body().get("skip") or []
        if not isinstance(skip, list):
            raise fail(400, "skip はパスの配列にしてください")
        plan = set_skips(path, {str(p) for p in skip})
        return {"skipped": sum(1 for op in plan.ops if op.skip)}

    @app.post("/api/plans/<plan_id>/delete")
    def api_plan_delete(plan_id: str) -> dict[str, Any]:
        path = plan_file(plan_id)
        if plan_info(path, settings.journal_dir).state != "draft":
            raise fail(409, "実行したプランは消せません（実行記録と対応しているため）")
        path.unlink()
        return {"deleted": plan_id}

    @app.post("/api/plans/<plan_id>/check")
    def api_plan_check(plan_id: str) -> dict[str, Any]:
        plan = load_plan(plan_file(plan_id))

        def run(rep: JobReporter) -> dict[str, Any]:
            ops = [op for op in plan.ops if not op.skip]
            rep.phase("変わっていないかの確認", total=len(ops))
            problems: dict[str, str] = {}
            for op in ops:
                problem = check_op(plan, op)
                if problem:
                    problems[op.path] = problem
                rep.advance()
            return {"plan_id": plan.id, "checked": len(ops), "problems": problems}

        return start("check", f"確認: {plan.id}", run, target=plan.id)

    @app.post("/api/plans/<plan_id>/apply")
    def api_plan_apply(plan_id: str) -> dict[str, Any]:
        path = plan_file(plan_id)
        plan = load_plan(path)
        config = settings.config()
        rc = config.find_root(plan.root)

        def run(rep: JobReporter) -> dict[str, Any]:
            conn = open_db()
            try:
                result = apply_plan(
                    conn,
                    plan,
                    quarantine_dir=config.plan.quarantine_dir,
                    journal_dir=settings.journal_dir,
                    snapshot=Snapper(config.plan.snapper_config) if config.plan.snapper_config else None,
                    sync=_guard(rc),
                    reporter=rep,
                    tags=settings.tags(config),
                )
            finally:
                conn.close()
            return _run_result(result)

        return start("apply", f"実行: {plan.id}", run, target=plan.id)

    @app.post("/api/plans/<plan_id>/undo")
    def api_plan_undo(plan_id: str) -> dict[str, Any]:
        plan = load_plan(plan_file(plan_id))
        config = settings.config()

        def run(rep: JobReporter) -> dict[str, Any]:
            conn = open_db()
            try:
                result = undo_plan(
                    conn,
                    plan.id,
                    journal_dir=settings.journal_dir,
                    snapshot=Snapper(config.plan.snapper_config) if config.plan.snapper_config else None,
                    reporter=rep,
                    tags=settings.tags(config),
                )
            finally:
                conn.close()
            return _run_result(result)

        return start("undo", f"取り消し: {plan.id}", run, target=plan.id)

    @app.post("/api/plans/<plan_id>/sync/<action:re:retry|resume>")
    def api_plan_sync(plan_id: str, action: str) -> dict[str, Any]:
        plan = load_plan(plan_file(plan_id))
        guard = _guard(settings.config().find_root(plan.root))
        if guard is None:
            raise fail(409, f"ルート {plan.root} に同期クライアント（sync_client）が設定されていません")

        def run(rep: JobReporter) -> dict[str, Any]:
            if action == "resume":
                rep.phase("常駐の同期を再開")
                force_resume_sync(plan.id, journal_dir=settings.journal_dir, sync=guard)
                return {"plan_id": plan.id, "sync_error": None}
            error = retry_sync(plan.id, journal_dir=settings.journal_dir, sync=guard, reporter=rep)
            if error:
                raise SyncError(error)
            return {"plan_id": plan.id, "sync_error": None}

        title = "削除の反映の再試行" if action == "retry" else "常駐の同期の再開"
        return start("sync", f"{title}: {plan.id}", run, target=plan.id)

    # --- ファイルブラウザ・版の履歴 ---

    def root_id_of(conn: sqlite3.Connection, rc: RootConfig) -> int:
        row = conn.execute("SELECT id FROM roots WHERE name = ?", (rc.name,)).fetchone()
        if row is None:
            raise fail(409, f"まだ走査していません: {rc.name}")
        return int(row["id"])

    @app.route("/api/browse")
    def api_browse() -> dict[str, Any]:
        q = bottle.request.query
        config = settings.config()
        rc = root_config(config, q.getunicode("root", ""))
        rel = clean_relpath(q.getunicode("path", ""))
        snapshot = q.getunicode("snapshot", "")
        if snapshot:
            entries = history.list_directory_at(rc.path, rel, snapshot)
            entries.sort(key=lambda e: (not e["is_dir"], e["name"].casefold()))
        else:
            path = resolve_within(rc.path, rel)
            if not path.is_dir():
                raise fail(404, f"フォルダがありません: {rel}")
            entries = list_live(path)
        store = settings.tags(config)
        tagged = store.tagged_under(rc.name, rel)
        for e in entries:
            child = f"{rel}/{e['name']}" if rel else e["name"]
            e["tags"] = tagged.get(child, [])
            e["inbox"] = e["is_dir"] and organize.is_inbox_name(e["name"], config.organize.inbox_patterns)
        crumbs = [{"name": rc.name, "path": ""}]
        acc = ""
        for part in rel.split("/") if rel else []:
            acc = f"{acc}/{part}" if acc else part
            crumbs.append({"name": part, "path": acc})
        return {
            "root": rc.name,
            "path": rel,
            "snapshot": snapshot or None,
            "crumbs": crumbs,
            "tags": store.tags_of(rc.name, rel),
            "entries": entries,
        }

    @app.route("/api/file")
    def api_file() -> Any:
        q = bottle.request.query
        rc = root_config(settings.config(), q.getunicode("root", ""))
        rel = clean_relpath(q.getunicode("path", ""))
        snapshot = q.getunicode("snapshot", "")
        if snapshot:
            base = history.snapshot_path(rc.path, "", snapshot)
            path = resolve_within(base, rel)
        else:
            path = resolve_within(rc.path, rel)
        if not path.is_file():
            raise fail(404, f"ファイルがありません: {rel}")
        mime, inline = serve_kind(path)
        download = bool(q.get("download")) or not inline
        res = bottle.static_file(path.name, root=str(path.parent), mimetype=mime, download=path.name if download else False)
        if mime == "text/plain":
            res.set_header("Content-Type", "text/plain; charset=utf-8")
        return res

    @app.route("/api/info")
    def api_info() -> dict[str, Any]:
        """ファイル・フォルダ1件の詳細（タグ・カタログの記録）。"""
        q = bottle.request.query
        config = settings.config()
        rc = root_config(config, q.getunicode("root", ""))
        rel = clean_relpath(q.getunicode("path", ""))
        path = resolve_within(rc.path, rel)
        mime, inline = serve_kind(path)
        conn = open_db()
        try:
            row = conn.execute(
                "SELECT e.sha256, e.first_seen FROM entries e JOIN roots r ON r.id = e.root_id"
                " WHERE r.name = ? AND e.relpath = ? AND e.gone_at IS NULL",
                (rc.name, rel),
            ).fetchone()
            same: list[str] = []
            if row is not None and row["sha256"]:
                same = [
                    r["relpath"]
                    for r in conn.execute(
                        "SELECT e.relpath FROM entries e JOIN roots r ON r.id = e.root_id"
                        " WHERE r.name = ? AND e.sha256 = ? AND e.gone_at IS NULL AND e.relpath != ? LIMIT 20",
                        (rc.name, row["sha256"], rel),
                    )
                ]
        finally:
            conn.close()
        return {
            "root": rc.name,
            "path": rel,
            "is_dir": path.is_dir(),
            "mime": mime,
            "inline": inline,
            "sha256": row["sha256"] if row is not None else None,
            "first_seen": row["first_seen"] if row is not None else None,
            "same_content": same,
            "tags": settings.tags(config).tags_of(rc.name, rel),
        }

    @app.route("/api/history")
    def api_history() -> dict[str, Any]:
        q = bottle.request.query
        rc = root_config(settings.config(), q.getunicode("root", ""))
        rel = clean_relpath(q.getunicode("path", ""))
        conn = open_db()
        try:
            versions = history.versions(conn, root_id_of(conn, rc), rc.path, rel)
        finally:
            conn.close()
        return {"root": rc.name, "path": rel, "versions": versions}

    @app.route("/api/snapshots")
    def api_snapshots() -> dict[str, Any]:
        rc = root_config(settings.config(), bottle.request.query.getunicode("root", ""))
        return {"snapshots": history.snapshot_summaries(rc.path)}

    @app.post("/api/restore")
    def api_restore() -> dict[str, Any]:
        data = body()
        rc = root_config(settings.config(), str(data.get("root", "")))
        rel = clean_relpath(str(data.get("path", "")))
        version_rel = clean_relpath(str(data.get("version_path") or rel))
        dest = history.restore_beside(rc.path, rel, version_rel, str(data.get("snapshot", "")))
        restored = dest.relative_to(rc.path).as_posix()
        log.info("過去の版を復元しました: %s → %s（スナップショット %s）", version_rel, restored, data.get("snapshot"))
        return {"restored": restored}

    # --- タグ ---

    @app.route("/api/tags")
    def api_tags() -> dict[str, Any]:
        store = settings.tags()
        return {"tags": [{"tag": t, "count": n} for t, n in sorted(store.all_tags().items())]}

    @app.route("/api/tags/items")
    def api_tag_items() -> dict[str, Any]:
        config = settings.config()
        tag = bottle.request.query.getunicode("tag", "")
        items = []
        for root, rel in settings.tags(config).items_with(tag):
            rc = config.find_root(root)
            path = (rc.path / rel) if rc is not None else None
            items.append(
                {
                    "root": root,
                    "path": rel,
                    "exists": bool(path and path.exists()),
                    "is_dir": bool(path and path.is_dir()),
                }
            )
        return {"tag": tag, "items": items}

    @app.post("/api/tags")
    def api_tags_edit() -> dict[str, Any]:
        data = body()
        config = settings.config()
        rc = root_config(config, str(data.get("root", "")))
        paths = [clean_relpath(str(p)) for p in data.get("paths") or []]
        for rel in paths:
            if not resolve_within(rc.path, rel).exists():
                raise fail(404, f"ありません: {rel}")
        store = settings.tags(config)
        if data.get("op") == "remove":
            n = store.remove(rc.name, paths, str(data.get("tag", "")))
        else:
            n = store.add(rc.name, paths, str(data.get("tag", "")))
        return {"changed": n}

    # --- 受け皿の整理の提案 ---

    def excluded_dirs(conn: sqlite3.Connection, root_id: int, rc: RootConfig, config: Config) -> list[str]:
        """提案の対象と移動先から外すフォルダ（アプリのデータ・作り直せる生成物）。"""
        report = build_report(conn, root_id, rc.name, config.hygiene)
        return [f.relpath for f in report.findings if f.is_dir and f.category in (rules.CAT_APP_DATA, rules.CAT_REGENERABLE)]

    @app.route("/api/organize/<root>")
    def api_organize(root: str) -> dict[str, Any]:
        config = settings.config()
        rc = root_config(config, root)
        conn = open_db()
        try:
            root_id = root_id_of(conn, rc)
            excluded = excluded_dirs(conn, root_id, rc, config)
            inbox, files = organize.find_inbox(conn, root_id, config.organize.inbox_patterns, excluded)
            cached = organize.cached_suggestions(conn, files, config.organize.model)
            # 同じ内容のファイルが受け皿の外にあれば示す（その場合は移動より隔離が向く）
            dups: dict[str, list[str]] = {}
            shas = [f.sha256 for f in files if f.sha256]
            for i in range(0, len(shas), 500):
                chunk = shas[i : i + 500]
                marks = ",".join("?" * len(chunk))
                for r in conn.execute(
                    f"SELECT relpath, sha256 FROM entries WHERE root_id = ? AND gone_at IS NULL AND is_dir = 0"
                    f" AND size > 0 AND sha256 IN ({marks})",
                    (root_id, *chunk),
                ):
                    if not any(r["relpath"] == d or r["relpath"].startswith(d + "/") for d in inbox):
                        dups.setdefault(r["sha256"], []).append(r["relpath"])
        finally:
            conn.close()
        return {
            "root": rc.name,
            "inbox": inbox,
            "model": config.organize.model,
            "patterns": config.organize.inbox_patterns,
            "files": [
                {
                    "path": f.relpath,
                    "size": f.size,
                    "mtime": f.mtime_ns // 1_000_000_000,
                    "suggestion": cached[f.relpath].to_dict() if f.relpath in cached else None,
                    "duplicates": dups.get(f.sha256 or "", []),
                }
                for f in files
            ],
        }

    @app.post("/api/organize/<root>/suggest")
    def api_organize_suggest(root: str) -> dict[str, Any]:
        data = body()
        config = settings.config()
        rc = root_config(config, root)
        only = [clean_relpath(str(p)) for p in data["paths"]] if data.get("paths") else None
        force = bool(data.get("force", False))
        oc = config.organize

        def run(rep: JobReporter) -> dict[str, Any]:
            conn = open_db()
            try:
                root_id = root_id_of(conn, rc)
                rep.phase("対象の確認")
                excluded = excluded_dirs(conn, root_id, rc, config)
                rep.note(f"モデル: {oc.model} ／ 受け皿のパターン: {', '.join(oc.inbox_patterns)} ／ 画像: {'使う' if oc.use_images else '使わない'}")
                done = {"ok": 0, "cached": 0, "error": 0}

                def on_start(n: int) -> None:
                    rep.phase("内容の読み取りと提案", total=n)

                def on_done(sug: organize.Suggestion) -> None:
                    key = "error" if sug.error else ("cached" if sug.cached else "ok")
                    done[key] += 1
                    name = sug.relpath.rsplit("/", 1)[-1]
                    if sug.error:
                        rep.note(f"失敗: {name}: {sug.error}")
                    elif not sug.cached:
                        rep.note(f"{name} → {sug.new_name}")
                    rep.advance()

                organize.run_suggestions(
                    conn, root_id, rc.path, oc, excluded, only=only, force=force, on_start=on_start, on_done=on_done
                )
                return {"root": rc.name, **done}
            finally:
                conn.close()

        return start("suggest", f"整理の提案: {rc.name}", run, target=rc.name)

    @app.route("/api/folders")
    def api_folders() -> dict[str, Any]:
        """移動先に選べるフォルダ（名前の一部で絞り込む）。"""
        q = bottle.request.query
        rc = root_config(settings.config(), q.getunicode("root", ""))
        word = q.getunicode("q", "").strip()
        conn = open_db()
        try:
            rows = conn.execute(
                "SELECT e.relpath FROM entries e JOIN roots r ON r.id = e.root_id"
                " WHERE r.name = ? AND e.gone_at IS NULL AND e.is_dir = 1 AND instr(lower(e.relpath), lower(?)) > 0"
                " ORDER BY length(e.relpath) LIMIT 50",
                (rc.name, word),
            ).fetchall()
        finally:
            conn.close()
        return {"folders": [r["relpath"] for r in rows if not any(p.startswith(".") for p in r["relpath"].split("/"))]}

    @app.post("/api/organize/<root>/plan")
    def api_organize_plan(root: str) -> dict[str, Any]:
        """採用した提案（移動・改名）から整理プランを作る。"""
        data = body()
        config = settings.config()
        rc = root_config(config, root)
        items = data.get("items") or []
        if not items:
            raise fail(400, "採用した提案がありません")
        plan = Plan(
            id=f"{datetime.now():%Y%m%d-%H%M%S}-{rc.name}-organize",
            root=rc.name,
            root_path=rc.path,
            created_at=utcnow(),
        )
        dests: set[str] = set()
        for it in items:
            src = clean_relpath(str(it.get("path", "")))
            dest = clean_relpath(str(it.get("dest", "")))
            if not src or not dest or src == dest:
                continue
            problem = rules.cloud_name_problem(dest)
            if problem:
                raise fail(400, f"{dest}: {problem}")
            if dest in dests or (rc.path / dest).exists():
                raise fail(409, f"移動先に同じ名前のものがあります: {dest}")
            dests.add(dest)
            fp = fingerprint(resolve_within(rc.path, src))
            if fp is None:
                raise fail(404, f"ありません: {src}")
            plan.ops.append(
                PlanOp(
                    action=ACTION_MOVE,
                    path=src,
                    dest=dest,
                    is_dir=False,
                    files=fp.files,
                    size=fp.size,
                    inode=fp.inode,
                    mtime_ns=fp.mtime_ns,
                    category=CAT_MOVE,
                    reason=str(it.get("reason", ""))[:200],
                )
            )
        if not plan.ops:
            raise fail(400, "動かすものがありません（名前も場所も変わらない）")
        write_plan(plan, settings.plans_dir / f"{plan.id}.toml")
        return {"plan_id": plan.id, "ops": len(plan.ops)}

    # 予期できる失敗は 400 番台の JSON にして、画面にそのまま表示できるようにする
    def json_errors(callback: Any) -> Any:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return callback(*args, **kwargs)
            except (PlanError, ConfigError, SnapshotError, SyncError, RootUnavailableError, TagError, PathError, OllamaError) as e:
                return fail(400, str(e))
            except HistoryError as e:
                return fail(409, str(e))
            except FileNotFoundError as e:
                return fail(404, str(e))
            except (NotADirectoryError, PermissionError) as e:
                return fail(400, str(e))

        return wrapper

    app.install(json_errors)
    return app


def _root_state(conn: sqlite3.Connection, rc: RootConfig) -> dict[str, Any]:
    """ルートの概要（ファイル数・容量・ハッシュの計算状況・最後の走査・同期クライアントの状態）。"""
    out: dict[str, Any] = {"name": rc.name, "path": str(rc.path), "scanned": False}
    row = conn.execute("SELECT id FROM roots WHERE name = ?", (rc.name,)).fetchone()
    if row is not None:
        n, size, hashed, hashed_size = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0),"
            " SUM(sha256 IS NOT NULL AND hashed_size IS size AND hashed_mtime_ns IS mtime_ns),"
            " COALESCE(SUM(CASE WHEN sha256 IS NOT NULL AND hashed_size IS size"
            " AND hashed_mtime_ns IS mtime_ns THEN size END), 0)"
            " FROM entries WHERE root_id = ? AND gone_at IS NULL AND is_dir = 0",
            (row["id"],),
        ).fetchone()
        scan = last_scan(conn, row["id"])
        out.update(
            files=n,
            size=size,
            hashed=hashed or 0,
            hashed_size=hashed_size,
            scanned=scan is not None,
            last_scan=dict(scan) if scan is not None else None,
        )
    guard = _guard(rc)
    if guard is not None:
        try:
            out["sync"] = {"mode": rc.sync_guard, **guard.status()}
        except SyncError as e:
            out["sync"] = {"client": guard.name, "error": str(e)}
    return out


def _plan_summary(info: Any) -> dict[str, Any]:
    """プランの概要を JSON にできる辞書にする。"""
    d = asdict(info)
    d["file"] = str(info.file)
    return d


def _run_scan(conn: sqlite3.Connection, root_id: int, rc: RootConfig, config: Config, rep: JobReporter) -> dict[str, Any]:
    """走査を進捗つきで行う。作業量の目安は前回の件数。"""
    (previous,) = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE root_id = ? AND gone_at IS NULL", (root_id,)
    ).fetchone()
    rep.phase("走査", total=previous or None)
    res = scan_root(conn, root_id, rc.path, config.safety, exclude=rc.exclude, on_progress=rep.set_done)
    rep.set_total(res.seen)
    rep.set_done(res.seen)
    return {**asdict(res), "status": res.status}


def _run_hash(conn: sqlite3.Connection, root_id: int, rc: RootConfig, rep: JobReporter) -> dict[str, Any]:
    """ハッシュの計算を、バイト数の進捗つきで行う。"""
    rep.phase("ハッシュ計算", unit="B")
    res = hash_root(
        conn,
        root_id,
        rc.path,
        on_start=lambda count, total: (rep.set_total(total), rep.note(f"対象 {count:,} 件")),
        on_bytes=rep.advance,
    )
    return asdict(res)


def _run_result(result: Any) -> dict[str, Any]:
    """実行・取り消しの結果を画面用の辞書にする。"""
    done = result.done
    return {
        "plan_id": result.plan_id,
        "done": len(done),
        "done_files": sum(o.files for o in done),
        "done_size": sum(o.size for o in done),
        "skipped": [{"path": o.path, "detail": o.detail} for o in result.skipped],
        "pre_snapshot": result.pre_snapshot,
        "post_snapshot": result.post_snapshot,
        "synced": result.synced,
        "sync_error": result.sync_error,
    }


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """リクエストをスレッドで並行に処理する WSGI サーバー（進捗の問い合わせを処理中に待たせない）。"""

    daemon_threads = True


class _QuietHandler(WSGIRequestHandler):
    """アクセスログを詳しいログ（DEBUG）にだけ出す。"""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 親クラスの引数名に合わせる
        log.debug("%s %s", self.address_string(), format % args)


def load_credentials(auth_file: Path | None) -> Credentials | None:
    """資格情報をファイルか環境変数 TAG_KEEPER_AUTH から読む。どちらも無ければ None。"""
    if auth_file is not None:
        return Credentials.parse(auth_file.read_text(encoding="utf-8"))
    env = os.environ.get("TAG_KEEPER_AUTH")
    if env:
        return Credentials.parse(env)
    return None


def serve(
    *,
    host: str,
    port: int,
    config_path: Path | None,
    db_path: Path,
    auth_file: Path | None,
    allow_no_auth: bool,
) -> int:
    """Web の画面のサーバーを動かす（Ctrl+C で止まるまで戻らない）。終了コードを返す。"""
    try:
        credentials = load_credentials(auth_file)
    except (OSError, ValueError) as e:
        log.error("資格情報を読めません: %s", e)
        return 2
    if credentials is None and not is_loopback(host) and not allow_no_auth:
        log.error(
            "ループバック以外（%s）で待ち受けるには、--auth-file か TAG_KEEPER_AUTH で認証を設定してください"
            "（信頼できるネットワークに限って --allow-no-auth で省略できます）",
            host,
        )
        return 2
    settings = Settings(config_path, db_path)
    app = create_app(settings, credentials, bind_host=host)
    server = make_server(host, port, app, server_class=_ThreadingWSGIServer, handler_class=_QuietHandler)
    shown = f"[{host}]" if ":" in host else host
    log.info("Web の画面: http://%s:%d/（認証: %s）", shown, port, "あり" if credentials else "なし")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("止めました")
    finally:
        server.server_close()
    return 0
