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
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import bottle

from tag_keeper import __version__, rules
from tag_keeper.catalog import connect, ensure_root, last_scan
from tag_keeper.config import Config, ConfigError, RootConfig, default_config_path, load_config
from tag_keeper.hashing import hash_root
from tag_keeper.jobs import JobBusyError, JobManager, JobReporter
from tag_keeper.plan import (
    PlanError,
    Snapper,
    SnapshotError,
    apply_plan,
    build_plan,
    check_op,
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
from tag_keeper.web.auth import Credentials, host_allowed, is_loopback, is_same_origin, parse_basic_header

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

    def config(self) -> Config:
        """設定を読み直す（編集をサーバーの再起動なしで反映するため、リクエストのたびに読む）。"""
        return load_config(self.config_path)


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
        bottle.response.set_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
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

    # 予期できる失敗は 400 番台の JSON にして、画面にそのまま表示できるようにする
    def json_errors(callback: Any) -> Any:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return callback(*args, **kwargs)
            except (PlanError, ConfigError, SnapshotError, SyncError, RootUnavailableError) as e:
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
