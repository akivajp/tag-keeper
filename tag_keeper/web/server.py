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

import json
import logging
import os
import re
import sqlite3
import threading
import unicodedata
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import bottle

from tag_keeper import __version__, dirtypes, history, media, officeview, organize, rules
from tag_keeper.dirtypes import scan_exclusions
from tag_keeper.catalog import connect, ensure_root, last_scan, utcnow
from tag_keeper.cloudlinks import OneDriveLinks
from tag_keeper.config import (
    Config,
    ConfigError,
    RootConfig,
    default_config_path,
    load_config,
)
from tag_keeper.analyze import AnalyzeWorker
from tag_keeper.snapwatch import Watch
from tag_keeper.decisions import DecisionLog
from tag_keeper.fulltext import KINDS, ExtractWorker, find_entries, index_status, query_terms, snippets
from tag_keeper.hashing import hash_root, sha256_file
from tag_keeper.history import HistoryError
from tag_keeper.jobs import AutoRefresh, JobBusyError, JobManager, JobReporter, RootTiming
from tag_keeper.media import MediaConverter
from tag_keeper.organize import OllamaError
from tag_keeper.plan import (
    ACTION_MOVE,
    ACTION_QUARANTINE,
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
    new_plan_id,
    pending_restores,
    plan_info,
    purge_quarantine,
    purgeable,
    read_journal,
    retry_sync,
    set_skips,
    undo_plan,
    write_plan,
)
from tag_keeper.report import build_report, structure_issues, user_report
from tag_keeper.scan import RootUnavailableError, link_move, relink_by_hash, relink_candidates, scan_root
from tag_keeper.suggest_queue import SuggestQueue
from tag_keeper.syncguard import CloudSyncGuard, OneDriveGuard, SyncError, SyncGuard, make_guard
from tag_keeper.tags import TagError, TagStore, normalize_tag
from tag_keeper.tags import search as tags_search
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
# ファイルブラウザから削除したもののプランのカテゴリ
CAT_DELETE = "削除（隔離フォルダへ）"
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
        # 判断のログ（整理の提案に対して利用者が決めたこと。提案の文脈と学習データ）
        self.decision_log = DecisionLog(data_dir / "decisions")
        # LibreOffice で変換した Office 文書の PDF（内容ハッシュをキーに保存）
        self.office_pdf_dir = data_dir / "office-pdf"
        # ブラウザが直接は再生できない動画を、再生用の MP4 にしたもの（内容ハッシュをキーに保存）
        self.media = MediaConverter(data_dir / "media-cache")
        self._sha_cache: dict[tuple[str, int, int], str] = {}
        self._links: dict[tuple[str, str], OneDriveLinks | None] = {}
        # 裏で本文を抽出するスレッド・書類を読み取るスレッド（serve が動かす。テストでは無し）。ルートを最新にしたら起こす
        self.extractor: ExtractWorker | None = None
        self.analyzer: AnalyzeWorker | None = None
        self.transcriber: Any = None
        # スナップショットの見張りの結果（ルートごとの最新の比較と警告）
        self.snapwatch = Watch(data_dir / "snapwatch.json")

    def sha256_of(self, path: Path) -> str:
        """ファイルの内容ハッシュ（大きさと更新日時が同じあいだは、計算し直さない）。"""
        st = path.stat()
        key = (str(path), st.st_size, st.st_mtime_ns)
        if key not in self._sha_cache:
            self._sha_cache[key] = sha256_file(path)
        return self._sha_cache[key]

    def cloud_links(self, rc: RootConfig) -> OneDriveLinks | None:
        """ルートの同期クライアントから、クラウドの Web 画面で開くリンクを作る部品（今は OneDrive だけ）。"""
        key = (rc.name, rc.sync_client)
        if key not in self._links:
            links = None
            guard = _guard(rc)
            if isinstance(guard, OneDriveGuard):
                try:
                    links = OneDriveLinks(Path(os.path.expanduser("~/.config/onedrive")), guard.sync_dir())
                except SyncError as e:
                    log.warning("OneDrive の同期フォルダを調べられません: %s", e)
            self._links[key] = links
        return self._links[key]

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
    # 1件ずつの提案の待ち行列（画面でファイルを選ぶたびに提案を出す）
    queue = SuggestQueue(settings.db_path, settings.config, lambda _c: settings.decision_log, settings.tags)

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
            store = settings.tags(config)
            roots = [
                _root_state(conn, rc, config.scan.relink_days, store.relink_skipped(rc.name), config, store.dir_types_of(rc.name))
                for rc in config.roots
            ]
        finally:
            conn.close()
        plans = [_plan_summary(p) for p in list_plans(settings.plans_dir, settings.journal_dir)]
        job = jobs.latest()
        return {
            "version": __version__,
            "config_path": str(settings.config_path or default_config_path()),
            "quarantine_dir": str(config.plan.quarantine_dir),
            "snapper_config": config.plan.snapper_config,
            # 自動で最新にする設定（画面で、走査が古いかを判断するのに使う）
            "auto_refresh": config.scan.auto_refresh,
            "after_sync": config.scan.after_sync,
            "roots": roots,
            "plans": plans,
            "job": job.to_dict() if job else None,
            "categories": [{"key": k, "label": v} for k, v in rules.CATEGORY_KEYS.items()],
            "office_pdf": officeview.soffice() is not None,
            "media": media.available(),
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
            store = settings.tags(config)
            report = user_report(conn, row["id"], rc.name, config, store)
            issues = structure_issues(conn, row["id"], rc.name, config, store, report)
        finally:
            conn.close()
        data = report.to_dict()
        # 構造の見直しの提案と、「残す」と判断したもの（F-OR-2・F-OR-5）
        data["structure"] = [i.to_dict() for i in issues]
        data["kept"] = [{"path": p, "reason": why} for p, why in sorted(store.kept_of(rc.name).items())]
        data["category_help"] = {
            rules.CAT_EXTRACTED: "圧縮ファイルの隣に、展開したフォルダがあるもの。中身がそろっていれば、圧縮ファイルは要りません。",
            rules.CAT_OLD_VERSION: "「前の版」の関係を張った旧版のうち、後継版が今もあるもの。",
            rules.CAT_STALE_LARGE: "大きくて長く更新していないファイル。消すのではなく、保管先（ローカルや別のクラウド）の見直しの候補です。",
        }
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
                if ev in ("quarantine", "skip", "restore", "restore-skip", "purge", "purge-skip"):
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
            # 隔離フォルダに残っていて、空にできるもの（件数・容量）
            "purgeable": len(purge_items := purgeable(journal)),
            "purgeable_size": sum(it.size for it in purge_items),
            "sync": sync,
            "category_order": rules.CATEGORY_ORDER,
        }

    # --- 同期（cloud-sync）の状態と手動の開始 ---

    def cloud_guard(root: str) -> tuple[RootConfig, CloudSyncGuard]:
        """ルートの同期窓口が cloud-sync であることを確かめて返す。"""
        rc = settings.config().find_root(root)
        if rc is None:
            raise fail(404, f"ルートがありません: {root}")
        guard = _guard(rc)
        if not isinstance(guard, CloudSyncGuard):
            raise fail(409, f"ルート {root} は cloud-sync と連携していません（sync_client = \"cloud-sync\" を設定してください）")
        return rc, guard

    @app.route("/api/sync")
    def api_sync() -> dict[str, Any]:
        """cloud-sync と連携している全ルートの同期の状態（画面が数秒ごとに読む。カタログには触れない）。"""
        items: list[dict[str, Any]] = []
        for rc in settings.config().roots:
            guard = _guard(rc)
            if isinstance(guard, CloudSyncGuard):
                items.append({"root": rc.name, **guard.status()})
        return {"items": items}

    @app.post("/api/sync/<root>/start")
    def api_sync_start(root: str) -> dict[str, Any]:
        """今すぐ1回同期する（定期の同期とは別に手動で始める）。"""
        _rc, guard = cloud_guard(root)
        try:
            guard.trigger()
        except SyncError as e:
            raise fail(409, str(e)) from e
        return {"root": root, **guard.status()}

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
        return start(action, f"{ROOT_ACTION_TITLES[action]}: {rc.name}", root_job(settings, config, rc, action), target=rc.name)

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
                report = user_report(conn, row["id"], rc.name, config, settings.tags(config))
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
        return start_apply(load_plan(plan_file(plan_id)))

    def start_apply(plan: Plan) -> dict[str, Any]:
        """プランを実行するジョブを始める。"""
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
            if any(op.action == ACTION_MOVE for op in plan.ops):
                settings.decision_log.revert(plan.id)
            return _run_result(result)

        return start("undo", f"取り消し: {plan.id}", run, target=plan.id)

    @app.post("/api/plans/<plan_id>/purge")
    def api_plan_purge(plan_id: str) -> dict[str, Any]:
        """隔離したものを完全に消す（隔離を空にする。F-PL-3）。消したものは取り消せなくなる。

        画面は、元に戻せないことを示して利用者が承認したときだけ呼ぶ。
        """
        plan = load_plan(plan_file(plan_id))
        config = settings.config()
        if not purgeable(journal_path(settings.journal_dir, plan.id)):
            raise fail(409, f"隔離フォルダに残っているものがありません: {plan.id}")

        def run(rep: JobReporter) -> dict[str, Any]:
            result = purge_quarantine(
                plan.id,
                journal_dir=settings.journal_dir,
                snapshot=Snapper(config.plan.snapper_config) if config.plan.snapper_config else None,
                reporter=rep,
            )
            return {
                "plan_id": plan.id,
                "removed": result.removed,
                "files": result.files,
                "size": result.size,
                "failed": [{"path": p, "detail": d} for p, d in result.failed],
                "pre_snapshot": result.pre_snapshot,
                "post_snapshot": result.post_snapshot,
            }

        return start("purge", f"隔離を空にする: {plan.id}", run, target=plan.id)

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
            # スナップショットの中でも、シンボリックリンクでルートの外へ出ない。btrfs-timeline は今のツリーで
            # リンクを解決してからスナップショットの中のパスに直すので、今のツリーの側でも確かめる
            resolve_within(rc.path, rel)
            resolve_within(history.snapshot_path(rc.path, "", snapshot), rel)
            entries = history.list_directory_at(rc.path, rel, snapshot)
            entries.sort(key=lambda e: (not e["is_dir"], e["name"].casefold()))
        else:
            path = resolve_within(rc.path, rel)
            if not path.is_dir():
                raise fail(404, f"フォルダがありません: {rel}")
            entries = list_live(path)
        store = settings.tags(config)
        tagged = store.tagged_under(rc.name, rel)
        types = store.dir_types_of(rc.name)
        dir_info: dict[str, Any] = {"type": None, "card": None}
        if not snapshot:
            conn = open_db()
            try:
                row = conn.execute("SELECT id FROM roots WHERE name = ?", (rc.name,)).fetchone()
                if row is not None:
                    pats = config.organize.inbox_patterns
                    dir_info["type"] = dirtypes.effective_type(conn, row["id"], rel, types, pats).to_dict()
                    # 子のフォルダの型（付けた・継承・推定）。多すぎるフォルダでは、付けたものだけ
                    child_dirs = [e for e in entries if e["is_dir"]]
                    for e in child_dirs:
                        child = f"{rel}/{e['name']}" if rel else e["name"]
                        if len(child_dirs) <= 200:
                            t = dirtypes.effective_type(conn, row["id"], unicodedata.normalize("NFC", child), types, pats)
                        else:
                            t = dirtypes.explicit_type(unicodedata.normalize("NFC", child), types) or dirtypes.DirType("normal", "none")
                        # 継承した型は札にしない（親と同じなので、見ればわかる）
                        e["dir_type"] = t.to_dict() if t.how in ("set", "inferred") and t.type != "normal" else None
            finally:
                conn.close()
            dir_info["card"] = store.dir_cards_of(rc.name).get(unicodedata.normalize("NFC", rel))
            # 本文を索引に入れるか（自身か祖先で「入れない」にしていれば false。from は設定したフォルダ）
            nfc = unicodedata.normalize("NFC", rel)
            off = [p for p in store.no_extract_of(rc.name) if nfc == p or nfc.startswith(p + "/")]
            dir_info["extract"] = {"enabled": not off, "from": off[0] if off else None}
        for e in entries:
            child = f"{rel}/{e['name']}" if rel else e["name"]
            # タグはパスを NFC にそろえて持つ（F-ID-4）。ディスクの名前が NFD でも引けるようにそろえて引く
            e["tags"] = tagged.get(unicodedata.normalize("NFC", child), [])
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
            "attrs": store.attrs_of(rc.name, rel),
            "attr_defs": store.attr_defs_list(),
            "dir": dir_info,
            "dir_types": [{"type": k, "label": v} for k, v in dirtypes.DIR_TYPES.items()],
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
            # 全文検索のために取り出した本文（書き起こし・OCR・画像の説明を含む）の種類と、書き起こしは本文そのもの
            extract_info = None
            if row is not None and row["sha256"]:
                x = conn.execute("SELECT method, note, length(text) AS n, text FROM extracts WHERE sha256 = ?", (row["sha256"],)).fetchone()
                if x is not None:
                    extract_info = {"method": x["method"], "note": x["note"], "chars": x["n"],
                                    "text": x["text"] if x["method"] in ("transcript", "describe") else None}
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
            "attrs": settings.tags(config).attrs_of(rc.name, rel),
            "attr_defs": settings.tags(config).attr_defs_list(),
            "relations": settings.tags(config).relations_of(rc.name, rel),
            "extract": extract_info,
            "relation_kinds": settings.tags(config).relation_kinds(),
            "cloud_links": links.links(path, path.is_dir()) if (links := settings.cloud_links(rc)) is not None else [],
        }

    @app.route("/api/history")
    def api_history() -> dict[str, Any]:
        q = bottle.request.query
        rc = root_config(settings.config(), q.getunicode("root", ""))
        rel = clean_relpath(q.getunicode("path", ""))
        resolve_within(rc.path, rel)
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
        if not rel:
            raise fail(400, "復元するファイルを指定してください")
        version_rel = clean_relpath(str(data.get("version_path") or rel))
        snapshot = str(data.get("snapshot", ""))
        # 復元先（今のパスの隣）と、読み出す版（スナップショットの中）が、どちらもルートの外を指さないこと
        # btrfs-timeline は今のツリーでリンクを解決してから版を探すので、版のパスは今のツリーの側でも確かめる
        resolve_within(rc.path, rel)
        resolve_within(rc.path, version_rel)
        resolve_within(history.snapshot_path(rc.path, "", snapshot), version_rel)
        dest = history.restore_beside(rc.path, rel, version_rel, snapshot)
        restored = dest.relative_to(rc.path).as_posix()
        log.info("過去の版を復元しました: %s → %s（スナップショット %s）", version_rel, restored, data.get("snapshot"))
        return {"restored": restored}

    # --- タグ ---

    @app.route("/api/tags")
    def api_tags() -> dict[str, Any]:
        """使われているタグ（件数・別名・親・子）と、属性の定義・保存した検索。"""
        store = settings.tags()
        counts = store.all_tags()
        dictionary = store.dictionary()
        return {
            "tags": [{"tag": t, "count": counts.get(t, 0), **dictionary.get(t, {})} for t in sorted(set(counts) | set(dictionary))],
            "aliases": {a: c for c, d in dictionary.items() for a in d.get("aliases", [])},
            "attrs": store.attr_defs_list(),
            "searches": store.saved_searches(),
        }

    @app.post("/api/tags/alias")
    def api_tags_alias() -> dict[str, Any]:
        """別名を登録する（op=add）・外す（op=remove）。既に使われている名前を別名にすると、正式な名前に統合する。"""
        data = body()
        store = settings.tags()
        if data.get("op") == "remove":
            return {"removed": store.remove_alias(str(data.get("alias", "")))}
        store.add_alias(str(data.get("tag", "")), str(data.get("alias", "")))
        return {"tag": store.canonical(str(data.get("tag", "")))}

    @app.post("/api/tags/parent")
    def api_tags_parent() -> dict[str, Any]:
        """親子を登録する（op=add。子が付けば親も成り立つ）・外す（op=remove）。"""
        data = body()
        store = settings.tags()
        tag, parent = str(data.get("tag", "")), str(data.get("parent", ""))
        if data.get("op") == "remove":
            return {"removed": store.remove_parent(tag, parent)}
        store.add_parent(tag, parent)
        return {"tag": store.canonical(tag), "parent": store.canonical(parent)}

    @app.post("/api/attrs/defs")
    def api_attr_defs() -> dict[str, Any]:
        """属性を定義する（op=add: name・type・choices）・定義を消す（op=remove。付いた値も外れる）。"""
        data = body()
        store = settings.tags()
        if data.get("op") == "remove":
            return {"changed": store.undefine_attr(str(data.get("name", "")))}
        choices = data.get("choices") or []
        if isinstance(choices, str):
            choices = [c for c in re.split(r"[,、\n]", choices)]
        d = store.define_attr(str(data.get("name", "")), str(data.get("type", "")), [str(c) for c in choices])
        return {"attr": d.to_dict()}

    @app.post("/api/attrs")
    def api_attrs_edit() -> dict[str, Any]:
        """属性の値を付ける（value を渡す）・外す（value が null か op=remove）。複数のアイテムにまとめて付けられる。"""
        data = body()
        config = settings.config()
        rc = root_config(config, str(data.get("root", "")))
        paths = [clean_relpath(str(p)) for p in data.get("paths") or []]
        for rel in paths:
            if not resolve_within(rc.path, rel).exists():
                raise fail(404, f"ありません: {rel}")
        store = settings.tags(config)
        name = str(data.get("name", ""))
        if data.get("op") == "remove" or data.get("value") is None:
            return {"changed": store.unset_attr(rc.name, paths, name)}
        return {"changed": store.set_attr(rc.name, paths, name, data.get("value"))}

    @app.route("/api/relink/<root>")
    def api_relink(root: str) -> dict[str, Any]:
        """内容ハッシュでは自動で結び付けられなかった（候補が複数ある）新しいファイルと、その候補（F-ID-2）。"""
        config = settings.config()
        rc = root_config(config, root)
        conn = open_db()
        try:
            items = relink_candidates(
                conn, root_id_of(conn, rc), days=config.scan.relink_days, skipped=settings.tags(config).relink_skipped(rc.name)
            )
        finally:
            conn.close()
        return {"root": rc.name, "items": items}

    @app.post("/api/relink/<root>")
    def api_relink_decide(root: str) -> dict[str, Any]:
        """確認待ちの新しいファイルについて、利用者の判断を記録する。

        old_id を渡せば、その消えたファイルからの移動として結び付ける（タグ・属性・関係も追従する）。
        skip が真なら、どれとも結び付けないと記録し、以後は一覧に出さない。
        """
        data = body()
        config = settings.config()
        rc = root_config(config, root)
        store = settings.tags(config)
        conn = open_db()
        try:
            root_id = root_id_of(conn, rc)
            row = conn.execute("SELECT relpath, sha256 FROM entries WHERE id = ? AND root_id = ?", (int(data.get("new_id", 0)), root_id)).fetchone()
            if row is None:
                raise fail(404, "カタログに見つかりません")
            if data.get("skip"):
                store.skip_relink(rc.name, str(row["relpath"]), str(row["sha256"] or ""))
                return {"skipped": row["relpath"]}
            try:
                old_rel, new_rel = link_move(conn, root_id, int(data.get("new_id", 0)), int(data.get("old_id", 0)))
            except ValueError as e:
                raise fail(409, str(e)) from e
        finally:
            conn.close()
        moved = store.follow_moves(rc.name, [(old_rel, new_rel)])
        return {"from": old_rel, "to": new_rel, "tag_moves": moved}

    @app.post("/api/analyze/<root>")
    def api_analyze(root: str) -> dict[str, Any]:
        """書類・画像をモデルに読み取らせるジョブを始める（F-AI-1・F-AI-3・F-EX-3）。

        paths を渡せばそれだけを（読み取り済みでも読み直す）、無ければまだ読み取っていないものを新しい順に limit 件まで。
        結果は確認の画面に、未確認の提案として並ぶ。
        """
        from tag_keeper import analyze

        data = body()
        config = settings.config()
        rc = root_config(config, root)
        paths = [clean_relpath(str(p)) for p in data.get("paths") or []] or None
        limit = max(1, min(int(data.get("limit") or 20), 500))

        def run(rep: JobReporter) -> dict[str, Any]:
            conn = open_db()
            try:
                root_id = root_id_of(conn, rc)
                rep.phase("対象の確認")
                counts = {"ok": 0, "error": 0}

                def on_start(n: int) -> None:
                    rep.phase("書類・画像の読み取り", total=n)

                def on_item(t: Any, a: Any) -> None:
                    counts["error" if a.error else "ok"] += 1
                    rep.note(f"{'失敗' if a.error else '読み取り'}: {t.relpath.rsplit('/', 1)[-1]}" + (f"（{a.error}）" if a.error else ""))
                    rep.advance()

                analyze.run_pending(conn, root_id, rc, config, limit=None if paths else limit, paths=paths, force=bool(paths),
                                    on_start=on_start, on_item=on_item)
            finally:
                conn.close()
            return counts

        return start("analyze", f"書類の読み取り: {rc.name}", run, target=rc.name)

    @app.post("/api/transcribe/<root>")
    def api_transcribe(root: str) -> dict[str, Any]:
        """音声・動画を書き起こすジョブを始める（F-EX-2）。paths を渡せばそれだけを（書き起こし済みでもやり直す）、
        無ければまだ書き起こしていないものを新しい順に limit 件まで。"""
        from tag_keeper import transcribe
        from tag_keeper.fulltext import app_data_names

        if not transcribe.available():
            raise fail(409, "書き起こしには faster-whisper が要ります（pip install 'tag-keeper[transcribe]'。GPU なら [transcribe-gpu]）")
        data = body()
        config = settings.config()
        rc = root_config(config, root)
        paths = [clean_relpath(str(p)) for p in data.get("paths") or []] or None
        limit = max(1, min(int(data.get("limit") or 10), 200))

        def run(rep: JobReporter) -> dict[str, Any]:
            conn = open_db()
            counts = {"ok": 0, "skipped": 0, "error": 0}
            try:
                root_id = root_id_of(conn, rc)
                rep.phase("対象の確認")

                def on_start(n: int) -> None:
                    rep.phase("書き起こし", total=n)
                    rep.note(f"モデル: {config.transcribe.model} ／ {'GPU' if transcribe.resolve_device(config.transcribe.options()) == 'cuda' else 'CPU'}")

                def on_item(t: Any, r: Any) -> None:
                    key = "error" if r.error else ("skipped" if r.skipped else "ok")
                    counts[key] += 1
                    name = t.relpath.rsplit("/", 1)[-1]
                    rep.note(f"{name}: " + (r.error or r.skipped or f"{r.language}・{len(r.text.splitlines())} 行"))
                    rep.advance()

                transcribe.run_pending(conn, root_id, rc.path, config.transcribe.options(), limit=None if paths else limit, paths=paths,
                                       force=bool(paths), app_data=app_data_names(config), no_extract=settings.tags(config).no_extract_of(rc.name),
                                       on_start=on_start, on_item=on_item)
            finally:
                conn.close()
            return counts

        return start("transcribe", f"書き起こし: {rc.name}", run, target=rc.name)

    @app.route("/api/review/<root>")
    def api_review(root: str) -> dict[str, Any]:
        """確認待ちの読み取りの結果と、付ける候補（タグ・属性）。"""
        from tag_keeper import analyze

        config = settings.config()
        rc = root_config(config, root)
        store = settings.tags(config)
        conn = open_db()
        try:
            root_id = root_id_of(conn, rc)
            items = analyze.review_items(conn, root_id, rc.name, store.reviewed(rc.name))
            waiting = len(analyze.pending(conn, root_id, images=config.ai.images))
        finally:
            conn.close()
        return {"root": rc.name, "items": items, "unread": waiting, "auto": config.ai.auto, "allow_cloud": rc.allow_cloud}

    @app.post("/api/review/<root>")
    def api_review_decide(root: str) -> dict[str, Any]:
        """確認の判断をまとめて記録する。items: [{path, sha256, decision: accept|reject, tags, attrs}]。

        承認したものは、渡されたタグと属性（画面で選んだもの）だけを付ける。
        """
        from tag_keeper import analyze

        data = body()
        config = settings.config()
        rc = root_config(config, root)
        store = settings.tags(config)
        done = {"accept": 0, "reject": 0, "tags": 0, "attrs": 0}
        for it in data.get("items") or []:
            rel = clean_relpath(str(it.get("path", "")))
            decision = str(it.get("decision", ""))
            if decision == "accept":
                if not resolve_within(rc.path, rel).exists():
                    raise fail(404, f"ありません: {rel}")
                n = analyze.apply_accept(store, rc.name, rel, [str(t) for t in it.get("tags") or []], dict(it.get("attrs") or {}))
                done["tags"] += n["tags"]
                done["attrs"] += n["attrs"]
            store.record_review(rc.name, rel, str(it.get("sha256", "")), decision)
            done[decision] = done.get(decision, 0) + 1
        return done

    @app.route("/api/snapwatch/<root>")
    def api_snapwatch(root: str) -> dict[str, Any]:
        """スナップショットの見張り: 最新の比較・警告・スナップショットにだけ残る大きなファイル（調べてあれば）。"""
        config = settings.config()
        rc = root_config(config, root)
        only_file = settings.plans_dir.parent / f"snapshot-only-{rc.name}.json"
        only = json.loads(only_file.read_text(encoding="utf-8")) if only_file.exists() else None
        return {"latest": settings.snapwatch.latest(rc.name), "alerts": settings.snapwatch.alerts(rc.name), "snapshot_only": only,
                "snapper": bool(config.plan.snapper_config)}

    @app.post("/api/snapwatch/<root>/<action:re:check|protect|dismiss|snapshot-only>")
    def api_snapwatch_action(root: str, action: str) -> dict[str, Any]:
        """見張りの操作。check: 今すぐ比べる / protect: スナップショットを自動削除の対象から外す /
        dismiss: 警告を消す / snapshot-only: スナップショットにだけ残る大きなファイルを調べる（ジョブ）。"""
        from tag_keeper import snapwatch

        data = body()
        config = settings.config()
        rc = root_config(config, root)
        skip = scan_exclusions(config, rc, settings.tags(config).dir_types_of(rc.name))
        if action == "check":
            return {"latest": settings.snapwatch.check(rc.name, rc.path, exclude=rc.exclude, exclude_paths=skip, force=True)}
        if action == "protect":
            sid = str(data.get("snapshot", ""))
            if not re.fullmatch(r"\d+", sid):
                raise fail(400, f"スナップショットの番号が不正です: {sid}")
            try:
                snapwatch.protect(config.plan.snapper_config, sid)
            except RuntimeError as e:
                raise fail(409, str(e)) from e
            settings.snapwatch.mark_protected(rc.name, sid)
            return {"protected": sid}
        if action == "dismiss":
            settings.snapwatch.dismiss(rc.name, str(data.get("newer", "")))
            return {"dismissed": True}
        min_mb = max(1, int(data.get("min_mb") or 100))

        def run(rep: JobReporter) -> dict[str, Any]:
            conn = open_db()
            try:
                live = {r["relpath"]: r["size"] for r in conn.execute(
                    "SELECT relpath, size FROM entries WHERE root_id = ? AND gone_at IS NULL AND is_dir = 0", (root_id_of(conn, rc),))}
            finally:
                conn.close()
            rep.phase("スナップショットを読む")
            res = snapwatch.snapshot_only_files(rc.path, live, min_bytes=min_mb * 1024 * 1024, exclude=rc.exclude, exclude_paths=skip,
                                                on_progress=lambda i, n: (rep.set_total(n), rep.set_done(i)))
            out = {"checked_at": utcnow(), "min_mb": min_mb, "items": [asdict(x) for x in res[:500]], "total": len(res),
                   "total_bytes": sum(x.size for x in res)}
            only_file = settings.plans_dir.parent / f"snapshot-only-{rc.name}.json"
            only_file.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
            return {"total": len(res), "total_bytes": out["total_bytes"]}

        return start("snapshot-only", f"スナップショットにだけ残るファイル: {rc.name}", run, target=rc.name)

    @app.route("/api/digest")
    def api_digest() -> Any:
        """定期レポートの Markdown（今の状況）。"""
        from tag_keeper import digest

        config = settings.config()
        conn = open_db()
        try:
            text = digest.build(conn, config, settings.tags(config), plans_dir=settings.plans_dir, journal_dir=settings.journal_dir,
                                watch=settings.snapwatch, days=config.digest.days)
        finally:
            conn.close()
        return bottle.HTTPResponse(body=text, headers={"Content-Type": "text/markdown; charset=utf-8"})

    @app.post("/api/keep")
    def api_keep() -> dict[str, Any]:
        """整理の候補・構造の見直しを「残す」と記録する（理由つき。op=remove で外す）。F-OR-2。"""
        data = body()
        config = settings.config()
        rc = root_config(config, str(data.get("root", "")))
        rel = clean_relpath(str(data.get("path", "")))
        store = settings.tags(config)
        if data.get("op") == "remove":
            return {"changed": store.unkeep(rc.name, rel)}
        return {"changed": store.keep(rc.name, rel, str(data.get("reason", "")))}

    @app.post("/api/dirtype")
    def api_dirtype() -> dict[str, Any]:
        """フォルダの型を付ける（type が "" なら外して推定に任せる）。F-DR-1・3。"""
        data = body()
        config = settings.config()
        rc = root_config(config, str(data.get("root", "")))
        rel = clean_relpath(str(data.get("path", "")))
        t = str(data.get("type", ""))
        if t and t not in dirtypes.DIR_TYPES:
            raise fail(400, f"型が不正です: {t}")
        if not resolve_within(rc.path, rel).is_dir():
            raise fail(404, f"フォルダがありません: {rel}")
        return {"changed": settings.tags(config).set_dir_type(rc.name, rel, t)}

    @app.post("/api/dirextract")
    def api_dirextract() -> dict[str, Any]:
        """フォルダの本文を全文検索の索引に入れるか（extract: false で入れない。配下も含む）。F-EX-7。"""
        data = body()
        config = settings.config()
        rc = root_config(config, str(data.get("root", "")))
        rel = clean_relpath(str(data.get("path", "")))
        if not resolve_within(rc.path, rel).is_dir():
            raise fail(404, f"フォルダがありません: {rel}")
        return {"changed": settings.tags(config).set_extract(rc.name, rel, bool(data.get("extract")))}

    @app.post("/api/dircard")
    def api_dircard() -> dict[str, Any]:
        """ディレクトリカード（何を置く場所か・命名規則）を書く。両方空なら外す。F-DR-4。"""
        data = body()
        config = settings.config()
        rc = root_config(config, str(data.get("root", "")))
        rel = clean_relpath(str(data.get("path", "")))
        if not resolve_within(rc.path, rel).is_dir():
            raise fail(404, f"フォルダがありません: {rel}")
        changed = settings.tags(config).set_dir_card(rc.name, rel, str(data.get("description", "")), str(data.get("naming", "")))
        return {"changed": changed}

    @app.post("/api/dircard/draft")
    def api_dircard_draft() -> dict[str, Any]:
        """ディレクトリカードの下書きを、中のフォルダ名・ファイル名からモデルに書かせる（保存はしない）。

        モデルに使えなければ、命名規則だけを中のファイル名から推定して返す。
        """
        data = body()
        config = settings.config()
        rc = root_config(config, str(data.get("root", "")))
        rel = clean_relpath(str(data.get("path", "")))
        path = resolve_within(rc.path, rel)
        if not path.is_dir():
            raise fail(404, f"フォルダがありません: {rel}")
        entries = list_live(path)
        dirs = [e["name"] for e in entries if e["is_dir"] and not e["name"].startswith(".")][:40]
        files = [e["name"] for e in entries if not e["is_dir"] and not e["name"].startswith(".")][:60]
        style = organize.naming_style(files)
        prompt = (
            "次のフォルダの「ディレクトリカード」を書いてください。何を置く場所か（description）と、"
            "中のファイルの名前の付け方（naming。例: YYYYMMDD_相手_件名）を、フォルダの中身の言語で短く書きます。"
            "推測で断定せず、中身から読み取れることだけを書いてください。\n"
            f"フォルダ: {rel or '（ルート）'}\n子のフォルダ: {' / '.join(dirs) or '（なし）'}\n"
            f"ファイル名の例: {' / '.join(files) or '（なし）'}\n日付の付け方の多数派: {style}\n"
            'JSON で {"description": "...", "naming": "..."} の形で答えてください。'
        )
        oc, _note = organize.config_for_root(config.organize, rc.allow_cloud, str(data.get("model", "") or config.organize.model))
        try:
            # 主のモデルが応答しなければ、手元のモデルで下書きする（[organize] fallback）
            answer, used, _fb_note = organize.with_fallback(oc, lambda c: organize.ollama_generate(c, prompt))
            model = used.model
        except OllamaError as e:
            return {"description": "", "naming": style if files else "", "model": "", "note": str(e)}
        return {
            "description": " ".join(str(answer.get("description", "")).split())[:1000],
            "naming": " ".join(str(answer.get("naming", "")).split())[:200],
            "model": model,
            "note": "",
        }

    @app.post("/api/relations")
    def api_relations() -> dict[str, Any]:
        """2つのアイテムの間に関係を張る（op=add）・外す（op=remove）。from から to へ（例: 新しい版 → 前の版）。"""
        data = body()
        config = settings.config()
        rc = root_config(config, str(data.get("root", "")))
        src, dst = clean_relpath(str(data.get("from", ""))), clean_relpath(str(data.get("to", "")))
        store = settings.tags(config)
        kind = str(data.get("kind", "") or "関連")
        if data.get("op") == "remove":
            return {"changed": store.unrelate(rc.name, src, dst, kind)}
        for rel in (src, dst):
            if not rel or not resolve_within(rc.path, rel).exists():
                raise fail(404, f"ありません: {rel}")
        return {"changed": store.relate(rc.name, src, dst, kind)}

    @app.route("/api/export")
    def api_export() -> Any:
        """管理情報の全量（タグ・属性・関係・辞書・保存した検索）を JSONL で書き出す（F-MD-9）。"""
        events = settings.tags().export_events()
        body_text = "".join(json.dumps(ev, ensure_ascii=False) + "\n" for ev in events)
        return bottle.HTTPResponse(
            body=body_text,
            headers={
                "Content-Type": "application/x-ndjson; charset=utf-8",
                "Content-Disposition": f'attachment; filename="tag-keeper-{datetime.now():%Y%m%d}.jsonl"',
            },
        )

    @app.route("/api/searches")
    def api_searches() -> dict[str, Any]:
        return {"searches": settings.tags().saved_searches()}

    @app.post("/api/searches")
    def api_searches_edit() -> dict[str, Any]:
        """検索の条件を保存する（op=save: name・query）・消す（op=delete）。"""
        data = body()
        store = settings.tags()
        if data.get("op") == "delete":
            return {"removed": store.delete_search(str(data.get("name", "")))}
        store.save_search(str(data.get("name", "")), str(data.get("query", "")))
        return {"searches": store.saved_searches()}

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
        """提案の対象と移動先から外すフォルダ（アプリのデータ・作り直せる生成物・まとまり・アプリ管理・除外の型）。"""
        report = build_report(conn, root_id, rc.name, config.hygiene)
        out = [f.relpath for f in report.findings if f.is_dir and f.category in (rules.CAT_APP_DATA, rules.CAT_REGENERABLE)]
        return out + dirtypes.closed_dirs(conn, root_id, settings.tags(config).dir_types_of(rc.name))

    def inbox_hints(rc: RootConfig, config: Config) -> dict[str, Any]:
        """整理の提案に渡す、型で受け皿にしたフォルダとディレクトリカード。"""
        store = settings.tags(config)
        return {"inbox_dirs": dirtypes.inbox_dirs(store.dir_types_of(rc.name)), "cards": store.dir_cards_of(rc.name)}

    @app.route("/api/organize/<root>")
    def api_organize(root: str) -> dict[str, Any]:
        config = settings.config()
        rc = root_config(config, root)
        model = bottle.request.query.getunicode("model", "") or config.organize.model
        conn = open_db()
        try:
            root_id = root_id_of(conn, rc)
            excluded = excluded_dirs(conn, root_id, rc, config)
            inbox, files = organize.find_inbox(conn, root_id, config.organize.inbox_patterns, excluded, inbox_hints(rc, config)["inbox_dirs"])
            # 主のモデルの提案が無ければ、代わりに手元のモデルで作った提案を出す
            cached = organize.cached_suggestions(conn, files, model, organize.fallback_model(config.organize))
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
            "model": model,
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
        oc, cloud_note = organize.config_for_root(config.organize, rc.allow_cloud, str(data.get("model") or config.organize.model))

        def run(rep: JobReporter) -> dict[str, Any]:
            conn = open_db()
            try:
                root_id = root_id_of(conn, rc)
                rep.phase("対象の確認")
                excluded = excluded_dirs(conn, root_id, rc, config)
                rep.note(f"モデル: {oc.model} ／ 受け皿のパターン: {', '.join(oc.inbox_patterns)} ／ 画像: {'使う' if oc.use_images else '使わない'}")
                if cloud_note:
                    rep.note(cloud_note)
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
                        # 手元のモデルで代わりに作ったときは、そのモデルを添える
                        rep.note(f"{name} → {sug.new_name}" + (f"（{sug.model}）" if sug.model != oc.model else ""))
                    rep.advance()

                organize.run_suggestions(
                    conn,
                    root_id,
                    rc.path,
                    oc,
                    excluded,
                    root_name=rc.name,
                    decisions=settings.decision_log,
                    tag_counts=settings.tags(config).all_tags(),
                    **inbox_hints(rc, config),
                    only=only,
                    force=force,
                    on_start=on_start,
                    on_done=on_done,
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
            id=new_plan_id(rc.name, "organize"),
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
            # 移動先の途中にルートの外を指すシンボリックリンクがあれば、ルートの外へ移ってしまうので断る
            resolve_within(rc.path, dest)
            # OneDrive は大文字・小文字を区別しないので、それだけが違う名前も同じ名前として扱う
            clash = rules.case_conflict(rc.path, dest, src)
            if rules.name_key(dest) in dests or os.path.lexists(rc.path / dest) or clash:
                raise fail(409, f"移動先に同じ名前のものがあります: {clash or dest}")
            dests.add(rules.name_key(dest))
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
        _log_accepts(settings, config, rc, plan, items)
        out: dict[str, Any] = {"plan_id": plan.id, "ops": len(plan.ops)}
        if data.get("apply"):
            out["job"] = start_apply(plan)
        return out

    # --- 1件ずつの提案（待ち行列） ---

    @app.route("/api/models")
    def api_models() -> dict[str, Any]:
        """画面で選べるモデル（設定の既定・候補と、ollama の手元のモデル）。"""
        oc = settings.config().organize
        names = list(dict.fromkeys([oc.model, *oc.model_choices, *_local_models(oc)]))
        out = []
        for name in names:
            try:
                caps = organize.cached_capabilities(oc, name)
            except OllamaError:
                continue
            if "completion" in caps:
                out.append({"name": name, "cloud": organize.sends_outside(oc, name), "vision": "vision" in caps})
        return {"default": oc.model, "models": out}

    @app.post("/api/suggest")
    def api_suggest() -> dict[str, Any]:
        data = body()
        config = settings.config()
        rc = root_config(config, str(data.get("root", "")))
        model = str(data.get("model") or config.organize.model)
        reqs = [
            queue.enqueue(rc.name, clean_relpath(str(p)), model, force=bool(data.get("force")), prefetch=bool(data.get("prefetch")))
            for p in data.get("paths") or []
        ]
        return {"requests": [r.to_dict() for r in reqs]}

    @app.route("/api/suggest/queue")
    def api_suggest_queue() -> dict[str, Any]:
        return {"requests": queue.status(bottle.request.query.getunicode("root") or None)}

    @app.route("/api/suggestion")
    def api_suggestion() -> dict[str, Any]:
        """1ファイルの保存済みの提案と、画面に要る周りの情報（同じ内容のファイル・今のタグ・既存のタグ）。"""
        q = bottle.request.query
        config = settings.config()
        rc = root_config(config, q.getunicode("root", ""))
        rel = clean_relpath(q.getunicode("path", ""))
        model = q.getunicode("model", "") or config.organize.model
        conn = open_db()
        try:
            root_id = root_id_of(conn, rc)
            item = organize.catalog_item(conn, root_id, rel)
            sug = organize.load_cached(conn, item.sha256, model, organize.fallback_model(config.organize)) if item is not None and item.sha256 else None
            same: list[str] = []
            if item is not None and item.sha256 and item.size > 0:
                same = [
                    r["relpath"]
                    for r in conn.execute(
                        "SELECT relpath FROM entries WHERE root_id = ? AND sha256 = ? AND gone_at IS NULL AND relpath != ? LIMIT 10",
                        (root_id, item.sha256, rel),
                    )
                ]
        finally:
            conn.close()
        store = settings.tags(config)
        if sug is not None:
            sug.relpath = rel
        return {
            "root": rc.name,
            "path": rel,
            "model": model,
            "known": item is not None,
            "suggestion": sug.to_dict() if sug is not None else None,
            "same_content": same,
            "tags": store.tags_of(rc.name, rel),
            "existing_tags": sorted(store.all_tags()),
        }

    @app.post("/api/tags/rename")
    def api_tags_rename() -> dict[str, Any]:
        """タグの名前を変える（全ルート）。変更先が既にあれば、1つにまとめる。"""
        data = body()
        n = settings.tags().rename_tag(str(data.get("from", "")), str(data.get("to", "")))
        return {"changed": n}

    @app.post("/api/tags/delete")
    def api_tags_delete() -> dict[str, Any]:
        """タグを、付いているすべてのアイテムから外す（全ルート）。"""
        n = settings.tags().delete_tag(str(body().get("tag", "")))
        return {"changed": n}

    @app.route("/api/tags/search")
    def api_tags_search() -> dict[str, Any]:
        """タグで絞り込んだアイテム（フォルダのタグは配下にも効く）。名前の一部でも絞れる。"""
        q = bottle.request.query
        config = settings.config()
        rc = root_config(config, q.getunicode("root", "") or (config.roots[0].name if config.roots else ""))
        # getall() は値を latin-1 のまま返すので、UTF-8 に直した写しから読む（日本語のタグのため）
        uq = q.decode()
        # 別名は正式な名前にそろえる（親のタグは、子のタグが付いたものも含めて探す）
        include = [settings.tags(config).canonical(x) for x in uq.getall("tag") if x]
        exclude = [settings.tags(config).canonical(x) for x in uq.getall("not") if x]
        mode = "or" if q.get("mode") == "or" else "and"
        word = q.getunicode("q", "").strip().lower()
        files_only = q.get("folders") != "1"
        limit = max(1, min(int(q.get("limit", 500)), 5000))
        if not include and not exclude:
            raise fail(400, "タグを選んでください")
        conn = open_db()
        try:
            rows = conn.execute(
                "SELECT relpath, is_dir, size, mtime_ns FROM entries WHERE root_id = ? AND gone_at IS NULL ORDER BY relpath",
                (root_id_of(conn, rc),),
            ).fetchall()
        finally:
            conn.close()
        info = {r["relpath"]: r for r in rows if not (files_only and r["is_dir"]) and (not word or word in r["relpath"].lower())}
        store = settings.tags(config)
        hits = tags_search(((p, bool(info[p]["is_dir"])) for p in info), store.tagged_paths(rc.name, implied=True), include, mode, exclude)
        return {
            "root": rc.name,
            "total": len(hits),
            "items": [
                {
                    "path": path,
                    "is_dir": bool(info[path]["is_dir"]),
                    "size": info[path]["size"],
                    "mtime": info[path]["mtime_ns"] // 1_000_000_000,
                    "tags": store.tags_of(rc.name, path),
                }
                for path in hits[:limit]
            ],
        }

    @app.route("/api/search")
    def api_search() -> dict[str, Any]:
        """名前・本文・種類・場所・日付・大きさ・タグ・属性で探す（F-SE-1・2・4、F-VS-4）。どれか1つの条件が要る。

        問い合わせ: root、q（名前・フォルダに含む語。空白区切りで、すべて含むもの）、text（本文に含む語）、
        kind（種類: KINDS のキー）、under（このフォルダの配下）、after・before（更新日 YYYY-MM-DD）、min_size・max_size（バイト）、
        tag・not・mode（タグ。別名は正式な名前に、親のタグは子のタグが付いたものも含む。フォルダのタグは配下にも効く）、
        attr（属性。「名前」「名前=値」「名前>=値」など。フォルダの値は配下が継承する）、
        gone=1（今は無いものも含める）、collapse=1（同じ内容のファイルを1件にまとめる）、folders=1（フォルダも出す）、limit。
        """
        # getall() などは値を latin-1 のまま返すので、UTF-8 に直した写しから読む（日本語の語とタグのため）
        uq = bottle.request.query.decode()
        config = settings.config()
        rc = root_config(config, uq.get("root", "") or (config.roots[0].name if config.roots else ""))
        store = settings.tags(config)
        include = [store.canonical(x) for x in uq.getall("tag") if x]
        exclude = [store.canonical(x) for x in uq.getall("not") if x]
        mode = "or" if uq.get("mode") == "or" else "and"
        words = uq.get("q", "").split()
        text = uq.get("text", "").strip()
        kind = uq.get("kind", "")
        if kind and kind not in KINDS:
            raise fail(400, f"種類が不正です: {kind}")
        under = clean_relpath(uq.get("under", ""))
        conds = [_parse_attr_cond(x) for x in uq.getall("attr") if x.strip()]
        files_only = uq.get("folders") != "1"

        def number(key: str) -> int | None:
            raw = uq.get(key, "").strip()
            if not raw:
                return None
            try:
                return int(float(raw))
            except ValueError:
                raise fail(400, f"{key} は数で指定してください") from None

        limit = number("limit") or 500
        limit = max(1, min(limit, 5000))
        after, before = uq.get("after", "").strip(), uq.get("before", "").strip()
        min_size, max_size = number("min_size"), number("max_size")
        if not (include or exclude or words or text or kind or under or after or before or min_size is not None
                or max_size is not None or conds):
            raise fail(400, "探す条件を入れてください")
        terms = query_terms(text)
        conn = open_db()
        try:
            try:
                rows = find_entries(
                    conn, root_id_of(conn, rc), words=words, text=text, kind=kind, files_only=files_only, under=under,
                    after=after, before=before, min_size=min_size, max_size=max_size, gone=uq.get("gone") == "1",
                )
            except ValueError as e:
                raise fail(400, f"日付は YYYY-MM-DD で指定してください（{e}）") from None
            paths = [r["relpath"] for r in rows]
            info = {r["relpath"]: r for r in rows}
            if include or exclude:
                paths = tags_search(((p, bool(info[p]["is_dir"])) for p in paths), store.tagged_paths(rc.name, implied=True), include, mode, exclude)
            if conds:
                defs = {d["name"]: d for d in store.attr_defs_list()}
                values = store.attr_paths(rc.name)
                paths = [p for p in paths if _attrs_match(p, conds, values, defs)]
            # 同じ内容のファイルを1件にまとめる（最初のものを代表にし、ほかの場所を copies に入れる）
            copies: dict[str, list[str]] = {}
            if uq.get("collapse") == "1":
                first: dict[str, str] = {}
                kept: list[str] = []
                for p in paths:
                    sha = info[p]["sha256"]
                    if sha and not info[p]["is_dir"] and info[p]["gone_at"] is None:
                        if sha in first:
                            copies[first[sha]].append(p)
                            continue
                        first[sha] = p
                        copies[p] = []
                    kept.append(p)
                paths = kept
            shown = paths[:limit]
            snips = snippets(conn, [info[p]["sha256"] for p in shown if info[p]["sha256"]], terms) if terms else {}
        finally:
            conn.close()
        return {
            "root": rc.name,
            "total": len(paths),
            "items": [
                {
                    "path": path,
                    "is_dir": bool(info[path]["is_dir"]),
                    "size": info[path]["size"],
                    "mtime": info[path]["mtime_ns"] // 1_000_000_000,
                    "tags": store.tags_of(rc.name, path),
                    "attrs": store.attrs_of(rc.name, path),
                    "snippet": snips.get(info[path]["sha256"] or "", ""),
                    "gone_at": info[path]["gone_at"],
                    "copies": copies.get(path, []),
                }
                for path in shown
            ],
        }

    @app.route("/api/extract/status")
    def api_extract_status() -> dict[str, Any]:
        """全文検索の索引の状況（抽出済み・抽出待ち・OCR を使えるか）。"""
        uq = bottle.request.query.decode()
        config = settings.config()
        rc = root_config(config, uq.get("root", "") or (config.roots[0].name if config.roots else ""))
        conn = open_db()
        try:
            out = index_status(conn, root_id_of(conn, rc), config)
        finally:
            conn.close()
        worker = settings.extractor
        out["running"] = worker is not None
        out["current"] = worker.current if worker is not None else None
        return out

    @app.post("/api/tags/feedback")
    def api_tag_feedback() -> dict[str, Any]:
        """提案されたタグを付ける（accept）か、却下する（reject）。どちらも判断のログに残す。"""
        data = body()
        config = settings.config()
        rc = root_config(config, str(data.get("root", "")))
        rel = clean_relpath(str(data.get("path", "")))
        tag = normalize_tag(str(data.get("tag", "")))
        decision = "reject" if data.get("decision") == "reject" else "accept"
        if not resolve_within(rc.path, rel).exists():
            raise fail(404, f"ありません: {rel}")
        conn = open_db()
        try:
            item = organize.catalog_item(conn, root_id_of(conn, rc), rel)
        finally:
            conn.close()
        if decision == "accept":
            settings.tags(config).add(rc.name, [rel], tag, source="suggested")
        settings.decision_log.tag(rc.name, rel, item.sha256 if item else None, tag, decision, str(data.get("model", "")))
        return {"tag": tag, "decision": decision}

    # --- Office 文書のプレビュー ---

    def file_at(rc: RootConfig, rel: str, snapshot: str) -> Path:
        """今の（または過去の時点の）ファイルの実パス。"""
        base = history.snapshot_path(rc.path, "", snapshot) if snapshot else rc.path
        path = resolve_within(base, rel)
        if not path.is_file():
            raise fail(404, f"ファイルがありません: {rel}")
        return path

    @app.route("/api/office")
    def api_office() -> dict[str, Any]:
        """Office 文書の中身を、画面に出す構造にして返す（レイアウトは再現しない）。"""
        q = bottle.request.query
        rc = root_config(settings.config(), q.getunicode("root", ""))
        path = file_at(rc, clean_relpath(q.getunicode("path", "")), q.getunicode("snapshot", ""))
        try:
            return officeview.preview(path)
        except officeview.OfficeError as e:
            raise fail(400, str(e)) from e

    @app.route("/api/office/image")
    def api_office_image() -> Any:
        """Office 文書に埋め込まれた画像を返す（ブラウザで表示できる種類だけ）。"""
        q = bottle.request.query
        rc = root_config(settings.config(), q.getunicode("root", ""))
        path = file_at(rc, clean_relpath(q.getunicode("path", "")), q.getunicode("snapshot", ""))
        try:
            data, mime = officeview.image_part(path, q.getunicode("part", ""))
        except officeview.OfficeError as e:
            raise fail(400, str(e)) from e
        return bottle.HTTPResponse(body=data, headers={"Content-Type": mime})

    @app.route("/api/office/pdf")
    def api_office_pdf() -> Any:
        """LibreOffice で PDF に変換して返す（レイアウトどおりのプレビュー）。"""
        q = bottle.request.query
        rc = root_config(settings.config(), q.getunicode("root", ""))
        path = file_at(rc, clean_relpath(q.getunicode("path", "")), q.getunicode("snapshot", ""))
        if path.suffix.lower() not in officeview.CONVERTIBLE_EXTS:
            raise fail(400, f"この種類は PDF にできません: {path.suffix}")
        try:
            pdf = officeview.to_pdf(path, sha256_file(path), settings.office_pdf_dir)
        except officeview.OfficeError as e:
            raise fail(400, str(e)) from e
        return bottle.static_file(pdf.name, root=str(pdf.parent), mimetype="application/pdf")

    # --- ブラウザが直接は再生できない動画（flv・mkv・avi など） ---

    def media_file(q: Any) -> tuple[Path, str]:
        rc = root_config(settings.config(), q.getunicode("root", ""))
        path = file_at(rc, clean_relpath(q.getunicode("path", "")), q.getunicode("snapshot", ""))
        # mp4 などでも、中が HEVC などブラウザで再生できない形式なら変換する（画面が再生に失敗したとき）
        if path.suffix.lower() not in media.CONVERT_VIDEO | media.NATIVE_VIDEO:
            raise fail(400, f"この種類は変換しません: {path.suffix}")
        if not media.available():
            raise fail(400, "ffmpeg が入っていません")
        return path, settings.sha256_of(path)

    @app.route("/api/media/status")
    def api_media_status() -> dict[str, Any]:
        """再生用の MP4 の状態（まだなら裏で作り始める）。"""
        q = bottle.request.query
        path, sha = media_file(q)
        return settings.media.status(path, sha, retry=bool(q.get("retry"))).to_dict()

    @app.route("/api/media")
    def api_media() -> Any:
        """再生用の MP4（できていれば）。範囲の指定に応じるので、シークもできる。"""
        path, sha = media_file(bottle.request.query)
        out = settings.media.output_path(sha)
        if not out.exists():
            raise fail(409, "まだ変換していません")
        return bottle.static_file(out.name, root=str(out.parent), mimetype="video/mp4")

    # --- ファイルブラウザからの名前の変更・削除 ---

    def make_folder(rc: RootConfig, data: dict[str, Any]) -> dict[str, Any]:
        """フォルダを作る（空のフォルダを作るだけなので、プランにはしない）。"""
        parent = clean_relpath(str(data.get("parent", "")))
        name = str(data.get("name", "")).strip()
        if not name or "/" in name or name in (".", ".."):
            raise fail(400, f"フォルダの名前が不正です: {name!r}")
        rel = f"{parent}/{name}" if parent else name
        problem = rules.cloud_name_problem(rel)
        if problem:
            raise fail(400, f"{name}: {problem}")
        base = resolve_within(rc.path, parent)
        if not base.is_dir():
            raise fail(404, f"フォルダがありません: {parent}")
        target = base / name
        clash = rules.case_conflict(rc.path, rel)
        if os.path.lexists(target) or clash:
            raise fail(409, f"同じ名前のものが既にあります: {clash.rpartition('/')[2] if clash else name}")
        target.mkdir()
        log.info("フォルダを作りました: %s", rel)
        return {"created": rel}

    @app.post("/api/fileops")
    def api_fileops() -> dict[str, Any]:
        """ファイル・フォルダの名前の変更（rename）・別のフォルダへの移動（move）・削除（delete）を、
        1件ずつのプランにしてすぐ実行する。フォルダの作成（mkdir）は、その場で作る。

        プランにするものは、整理プランの仕組みに乗せる（実行直前の確認・前後のスナップショット・実行記録・取り消し）。
        削除は完全には消さず、隔離フォルダへ移す（F-PL-3）。
        """
        data = body()
        config = settings.config()
        rc = root_config(config, str(data.get("root", "")))
        op_kind = str(data.get("op", ""))
        if op_kind == "mkdir":
            return make_folder(rc, data)
        if op_kind not in ("rename", "move", "delete"):
            raise fail(400, f"操作が不明です: {op_kind}")
        items = data.get("items") or []
        dest_dir = clean_relpath(str(data.get("dest_dir", ""))) if op_kind == "move" else ""
        if op_kind == "move":
            problem = rules.cloud_name_problem(dest_dir) if dest_dir else None
            if problem:
                raise fail(400, f"{dest_dir}: {problem}")
            if dest_dir and resolve_within(rc.path, dest_dir).exists() and not (rc.path / dest_dir).is_dir():
                raise fail(400, f"移動先がフォルダではありません: {dest_dir}")
            # 選んだフォルダの中にあるものは、フォルダごと移るので重ねて扱わない
            paths = {clean_relpath(str(it.get("path", ""))) for it in items}
            items = [it for it in items if not any(clean_relpath(str(it.get("path", ""))).startswith(o + "/") for o in paths)]
        if not items:
            raise fail(400, "対象がありません")
        plan = Plan(
            id=new_plan_id(rc.name, op_kind),
            root=rc.name,
            root_path=rc.path,
            created_at=utcnow(),
        )
        dests: set[str] = set()
        for it in items:
            src = clean_relpath(str(it.get("path", "")))
            if not src:
                raise fail(400, "ルートそのものは扱えません")
            path = resolve_within(rc.path, src)
            fp = fingerprint(path)
            if fp is None or path.is_symlink():
                raise fail(404, f"ありません: {src}")
            is_dir = path.is_dir()
            if op_kind == "move":
                if dest_dir == src or dest_dir.startswith(src + "/"):
                    raise fail(400, f"フォルダをそれ自身の中へは移せません: {src}")
                dest = f"{dest_dir}/{src.rpartition('/')[2]}" if dest_dir else src.rpartition("/")[2]
                if dest == src:
                    continue
                problem = rules.cloud_name_problem(dest)
                if problem:
                    raise fail(400, f"{dest}: {problem}")
                clash = rules.case_conflict(rc.path, dest, src)
                if rules.name_key(dest) in dests or os.path.lexists(rc.path / dest) or clash:
                    raise fail(409, f"移動先に同じ名前のものがあります: {clash or dest}")
                dests.add(rules.name_key(dest))
                plan.ops.append(PlanOp(ACTION_MOVE, src, is_dir, fp.files, fp.size, fp.inode, fp.mtime_ns, CAT_MOVE, "移動", dest=dest))
            elif op_kind == "rename":
                name = str(it.get("new_name", "")).strip()
                if not name or "/" in name or name in (".", ".."):
                    raise fail(400, f"新しい名前が不正です: {name!r}")
                dest = f"{src.rpartition('/')[0]}/{name}" if "/" in src else name
                if dest == src:
                    continue
                problem = rules.cloud_name_problem(dest)
                if problem:
                    raise fail(400, f"{name}: {problem}")
                # 大文字・小文字だけの変更は、同じものとして見えるので許す（同じ inode か）
                target = rc.path / dest
                clash = rules.case_conflict(rc.path, dest, src)
                if rules.name_key(dest) in dests or (target.exists() and os.stat(target).st_ino != fp.inode) or clash:
                    raise fail(409, f"同じ名前のものが既にあります: {clash.rpartition('/')[2] if clash else name}")
                dests.add(rules.name_key(dest))
                plan.ops.append(PlanOp(ACTION_MOVE, src, is_dir, fp.files, fp.size, fp.inode, fp.mtime_ns, CAT_MOVE, "名前の変更", dest=dest))
            else:
                plan.ops.append(
                    PlanOp(ACTION_QUARANTINE, src, is_dir, fp.files, fp.size, fp.inode, fp.mtime_ns, CAT_DELETE, "ファイルブラウザから削除")
                )
        if not plan.ops:
            raise fail(400, "変えるものがありません（名前も場所も同じ）")
        write_plan(plan, settings.plans_dir / f"{plan.id}.toml")
        if op_kind in ("rename", "move"):
            # 利用者が決めた名前・置き場所は、提案の手本になる（ファイルだけ）
            _log_accepts(settings, config, rc, plan, items, source=f"browser-{op_kind}")
        return {"plan_id": plan.id, "ops": len(plan.ops), "job": start_apply(plan)}

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


def _local_models(oc: Any) -> list[str]:
    """ollama の手元のモデルと、[organize.endpoints] の送り先のモデル（「モデル名@送り先」）の名前（取れなければ空）。"""
    out: list[str] = []
    try:
        with urllib.request.urlopen(oc.ollama_url.rstrip("/") + "/api/tags", timeout=5) as res:
            out += [m["name"] for m in json.load(res).get("models", [])]
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        pass
    for key, url in (getattr(oc, "endpoints", None) or {}).items():
        try:
            with urllib.request.urlopen(url + "/models", timeout=5) as res:
                out += [f"{m['id']}@{key}" for m in json.load(res).get("data", []) if m.get("id")]
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
            continue
    return out


def _log_accepts(
    settings: Settings, config: Config, rc: RootConfig, plan: Plan, items: list[dict[str, Any]], source: str = "organize"
) -> None:
    """整理プランにした判断を、判断のログに「採用」として残す（提案があれば、提案と最終的な判断の対）。

    フォルダの移動・改名は、ファイルの名前の手本にならないので残さない。
    """
    by_path = {str(it.get("path", "")): it for it in items}
    store = settings.tags(config)
    conn = connect(settings.db_path)
    events = []
    try:
        row = conn.execute("SELECT id FROM roots WHERE name = ?", (rc.name,)).fetchone()
        for op in plan.ops:
            if op.is_dir or op.action != ACTION_MOVE:
                continue
            it = by_path.get(op.path, {})
            model = str(it.get("model") or config.organize.model)
            item = organize.catalog_item(conn, int(row["id"]), op.path) if row is not None else None
            sug = organize.load_cached(conn, item.sha256, model) if item is not None and item.sha256 else None
            dest_dir, _, name = op.dest.rpartition("/")
            events.append(
                {
                    "root": rc.name,
                    "path": op.path,
                    "sha256": item.sha256 if item else None,
                    "model": model if sug is not None else "",
                    "info": sug.info if sug is not None else {},
                    "suggested": {
                        "name": sug.new_name,
                        "destinations": [d["relpath"] for d in sug.destinations],
                        "tags": sug.tags,
                    }
                    if sug is not None
                    else None,
                    "final": {"name": name, "dest_dir": dest_dir, "tags": [t["tag"] for t in store.tags_of(rc.name, op.path)["direct"]]},
                    "plan_id": plan.id,
                    "source": source,
                }
            )
    finally:
        conn.close()
    settings.decision_log.accept(events)


def _root_state(
    conn: sqlite3.Connection,
    rc: RootConfig,
    relink_days: int = 0,
    skipped: set[tuple[str, str]] | None = None,
    config: Config | None = None,
    types: dict[str, str] | None = None,
) -> dict[str, Any]:
    """ルートの概要（ファイル数・容量・ハッシュの計算状況・最後の走査・移動の確認待ち・同期クライアントの状態）。"""
    skipped = skipped or set()
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
        # 見えなくなったが、消えたとの確定を保留しているファイル（次の走査でも見えなければ確定する）
        (held,) = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE root_id = ? AND gone_at IS NULL AND is_dir = 0 AND missing_count > 0",
            (row["id"],),
        ).fetchone()
        out.update(
            files=n,
            size=size,
            hashed=hashed or 0,
            hashed_size=hashed_size,
            held_files=held,
            relink_pending=len(relink_candidates(conn, row["id"], days=relink_days, skipped=skipped)) if relink_days else 0,
            scanned=scan is not None,
            last_scan=dict(scan) if scan is not None else None,
        )
    out["excluded"] = [{"path": p, "reason": why} for p, why in sorted(scan_exclusions(config, rc, types or {}).items())] if config else []
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


_ATTR_COND = re.compile(r"^(.+?)(>=|<=|=|>|<)(.*)$")


def _parse_attr_cond(raw: str) -> tuple[str, str, str]:
    """属性の条件（「名前」「名前=値」「名前>=値」など）を（名前, 演算子, 値）にする。名前だけなら「付いている」。"""
    m = _ATTR_COND.match(raw.strip())
    if m is None:
        return normalize_tag(raw), "exists", ""
    return normalize_tag(m.group(1)), m.group(2), m.group(3).strip()


def _attrs_match(path: str, conds: list[tuple[str, str, str]], values: dict[str, dict[str, Any]], defs: dict[str, dict[str, Any]]) -> bool:
    """アイテムの属性（自身に無ければ、近い祖先のフォルダの値を継承）が、すべての条件に合うか。"""
    from tag_keeper.tags import AttrDef, match_attr

    for name, op, want in conds:
        value = None
        p = unicodedata.normalize("NFC", path)  # 属性はパスを NFC にそろえて持つ（F-ID-4）
        while True:
            v = values.get(p, {})
            if name in v:
                value = v[name]
                break
            if not p:
                break
            p = p.rsplit("/", 1)[0] if "/" in p else ""
        d = defs.get(name)
        if not match_attr(AttrDef(name, d["type"], tuple(d["choices"])) if d else None, value, op, want):
            return False
    return True


# ルートに対する処理の題名（画面の英語表示は i18n.js の辞書で訳す）
ROOT_ACTION_TITLES = {"scan": "走査", "hash": "ハッシュ計算", "refresh": "最新にする（走査とハッシュ計算）"}


def root_job(settings: Settings, config: Config, rc: RootConfig, action: str) -> Any:
    """ルートを走査する・ハッシュを計算する（refresh は両方）ジョブの本体を作る。

    走査の後はツールの外での移動・改名にタグを追従させ（P4）、ハッシュの後は inode で結び付かなかった移動を
    内容ハッシュで結び付ける（F-ID-1。クラウド側での移動が「削除と新規」として届いたものなど）。
    画面のボタンと、自動で最新にする処理（AutoRefresh）の両方から使う。
    """

    def run(rep: JobReporter) -> dict[str, Any]:
        conn = connect(settings.db_path)
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
                rl = relink_by_hash(conn, root_id, days=config.scan.relink_days)
                out["relink"] = {
                    "files": rl.files,
                    "ambiguous": rl.ambiguous,
                    "tag_moves": settings.tags(config).follow_moves(rc.name, rl.moves),
                }
                if rl.files:
                    rep.note(f"内容ハッシュで結び付けた移動 {rl.files:,} 件")
                # 新しくハッシュを計算したファイルの本文を、裏ですぐに抽出させる
                if settings.extractor is not None:
                    settings.extractor.poke()
            return out
        finally:
            conn.close()

    return run


# 自動で最新にするジョブの題名
AUTO_REFRESH_TITLE = "自動で最新にする"


def _epoch(iso: object) -> float | None:
    """ISO 8601 の日時を UNIX 時刻にする。読めなければ None（時差の無いものは UTC とみなす）。"""
    if not isinstance(iso, str) or not iso:
        return None
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.timestamp()


def root_timings(settings: Settings) -> list[RootTiming]:
    """各ルートの最後の走査と、同期の状態（自動で最新にするかの判断に使う）。"""
    config = settings.config()
    if config.scan.auto_refresh <= 0 and not config.scan.after_sync:
        return []
    conn = connect(settings.db_path)
    try:
        out: list[RootTiming] = []
        for rc in config.roots:
            row = conn.execute("SELECT id FROM roots WHERE name = ?", (rc.name,)).fetchone()
            scan = last_scan(conn, row["id"]) if row is not None else None
            busy, finished = False, None
            guard = _guard(rc)
            # 状態を読めるのは cloud-sync だけ（onedrive の常駐の同期は、間隔だけで判断する）
            if isinstance(guard, CloudSyncGuard):
                state = guard.read_state()
                busy = state.get("state") in ("running", "waiting")
                finished = _epoch(state.get("finished"))
            out.append(
                RootTiming(
                    rc.name,
                    config.scan.auto_refresh * 60.0,
                    config.scan.after_sync,
                    _epoch(scan["finished_at"]) if scan is not None else None,
                    busy,
                    finished,
                )
            )
        return out
    finally:
        conn.close()


def start_auto_refresh(settings: Settings, jobs: JobManager, name: str) -> bool:
    """ルートを最新にするジョブを、自動の印を付けて始める。別のジョブが動いていれば False。"""
    config = settings.config()
    rc = config.find_root(name)
    if rc is None:
        # 設定から消えた。始めたことにして、次の機会まで飛ばす
        return True
    try:
        jobs.start(
            "refresh",
            f"{AUTO_REFRESH_TITLE}: {rc.name}",
            root_job(settings, config, rc, "refresh"),
            target=rc.name,
            auto=True,
        )
    except JobBusyError:
        return False
    return True


def _auto_refresh_loop(
    auto: AutoRefresh, stop: threading.Event, first: float = 60.0, every: float = 30.0, extra: list[Any] | None = None
) -> None:
    """自動で最新にするかを、一定の間隔で確かめ続ける（serve の裏のスレッド）。extra の関数も毎回呼ぶ（見張り・定期レポート）。

    起動の直後は、ネットワークなどが落ち着くまで first 秒待つ。
    """
    if stop.wait(first):
        return
    while True:
        for fn in [auto.tick, *(extra or [])]:
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - 一時的な失敗（カタログのロックなど）で止めない
                log.warning("裏の定期の処理に失敗しました: %s", e)
        if stop.wait(every):
            return


def watch_snapshots(settings: Settings) -> None:
    """各ルートの最新の2つのスナップショットを比べる（同じ組は保存した結果を使うので、ふだんは何もしない）。F-VS-5。"""
    config = settings.config()
    for rc in config.roots:
        skip = scan_exclusions(config, rc, TagStore(config.tags.dir).dir_types_of(rc.name))
        settings.snapwatch.check(rc.name, rc.path, exclude=rc.exclude, exclude_paths=skip)


def write_digest_if_due(settings: Settings) -> None:
    """[digest] path があり、前に書いてから every_hours 以上たっていれば、定期レポートを書き直す。F-UI-3。"""
    import time as _time

    from tag_keeper import digest

    config = settings.config()
    if not config.digest.path:
        return
    path = Path(config.digest.path)
    if path.exists() and _time.time() - path.stat().st_mtime < config.digest.every_hours * 3600:
        return
    conn = connect(settings.db_path)
    try:
        text = digest.build(conn, config, settings.tags(config), plans_dir=settings.plans_dir, journal_dir=settings.journal_dir,
                            watch=settings.snapwatch, days=config.digest.days)
    finally:
        conn.close()
    digest.write(path, text)
    log.info("定期レポートを書きました: %s", path)


def _run_scan(conn: sqlite3.Connection, root_id: int, rc: RootConfig, config: Config, rep: JobReporter) -> dict[str, Any]:
    """走査を進捗つきで行う。作業量の目安は前回の件数。"""
    (previous,) = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE root_id = ? AND gone_at IS NULL", (root_id,)
    ).fetchone()
    rep.phase("走査", total=previous or None)
    skip = scan_exclusions(config, rc, TagStore(config.tags.dir).dir_types_of(rc.name))
    if skip:
        rep.note("走査しないフォルダ: " + "、".join(f"{p}（{why}）" for p, why in sorted(skip.items())))
    res = scan_root(conn, root_id, rc.path, config.safety, exclude=rc.exclude, on_progress=rep.set_done, exclude_paths=skip)
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
    jobs = JobManager(settings.job_log_dir)
    app = create_app(settings, credentials, bind_host=host, jobs=jobs)
    server = make_server(host, port, app, server_class=_ThreadingWSGIServer, handler_class=_QuietHandler)
    shown = f"[{host}]" if ":" in host else host
    log.info("Web の画面: http://%s:%d/（認証: %s）", shown, port, "あり" if credentials else "なし")
    # 動かしている間は、ルートを自動で最新に保つ（[scan] の auto_refresh・after_sync）
    stop = threading.Event()
    auto = AutoRefresh(lambda: root_timings(settings), lambda name: start_auto_refresh(settings, jobs, name))
    extra = [lambda: watch_snapshots(settings), lambda: write_digest_if_due(settings)]
    threading.Thread(target=_auto_refresh_loop, args=(auto, stop), kwargs={"extra": extra}, name="auto-refresh", daemon=True).start()
    # 裏で本文を抽出し、全文検索の索引を作る（[extract] enabled。止めていても、設定を変えれば再起動なしで始まる）
    settings.extractor = ExtractWorker(settings.db_path, settings.config)
    settings.extractor.start()
    # 新しい書類・画像を裏で読み取る（[ai] auto = true のときだけ動く。設定を変えれば再起動なしで始まる）
    settings.analyzer = AnalyzeWorker(settings.db_path, settings.config)
    settings.analyzer.start()
    # 新しい音声・動画を裏で書き起こす（[transcribe] auto = true で、faster-whisper があるときだけ動く）
    from tag_keeper.transcribe import TranscribeWorker

    settings.transcriber = TranscribeWorker(settings.db_path, settings.config)
    settings.transcriber.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("止めました")
    finally:
        stop.set()
        settings.extractor.stop()
        settings.analyzer.stop()
        settings.transcriber.stop()
        server.server_close()
    return 0
