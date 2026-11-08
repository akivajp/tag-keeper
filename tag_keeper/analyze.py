"""書類の構造化抽出と画像の内容推定（要件 F-AI-1〜3・F-EX-3）。

- 書類: 全文検索のために抽出した本文（extracts）を、モデルに読ませて、書類の種類・日付・相手・金額・期限・必要な対応・
  分類のラベル・要約を JSON で取り出す
- 画像: 画像を読めるモデルに見せて、書類か写真かと、内容の説明・ラベルを取り出す。説明は全文検索の索引にも入れる
  （写真も内容の言葉で探せるように）
- 結果はカタログの analyses 表に、内容ハッシュをキーに保存する（作り直せる派生物）。確認の画面で利用者が承認したものだけを、
  タグ（出所 ai）と属性として付ける。承認・却下の判断はタグのログ（review）に残す
- 外部へ送らないルート（allow_cloud = false）では、クラウドのモデルを使わない（organize.config_for_root）
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tag_keeper import extract, fulltext, organize
from tag_keeper.catalog import connect, utcnow
from tag_keeper.config import Config, OrganizeConfig, RootConfig

log = logging.getLogger(__name__)

# モデルに渡す本文の上限（文字数）
MAX_TEXT = 6000
# 承認したときに付ける属性（読み取った項目 → (属性の名前, 型)）。無ければ定義してから付ける
FIELD_ATTRS: dict[str, tuple[str, str]] = {
    "date": ("日付", "date"),
    "party": ("相手", "text"),
    "amount": ("金額", "number"),
    "due": ("期限", "date"),
    "action": ("必要な対応", "text"),
}
# 必要な対応か期限があるとき、一緒に付けるフラグ
FLAG_ATTR = "要対応"
# 承認したときに付けるタグの名前空間
DOC_TYPE_NS = "種別"
LABEL_NS = "分類"
IMAGE_KIND_NS = "画像"
# 画像の種類の表示名（タグの値）
IMAGE_KINDS = {"document": "書類", "photo": "写真", "screenshot": "画面", "illustration": "絵・図", "other": "その他"}

DOCUMENT_PROMPT = """次の書類を読み、内容を JSON で答えてください。値は書類の言語で書き、書類から読み取れないものは null にします。推測で埋めないでください。

{{
  "doc_type": "書類の種類（例: 請求書・領収書・契約書・見積書・通知・明細・案内・報告書）",
  "date": "書類の日付（発行日など。YYYY-MM-DD）",
  "party": "相手（発行元・宛先の会社や役所など。自分以外）",
  "amount": 合計金額（数だけ。通貨記号やカンマは付けない）,
  "currency": "通貨（JPY・USD など）",
  "due": "期限（支払期限・提出期限など。YYYY-MM-DD）",
  "action": "受け取った人がしなければならないこと（支払い・提出・返送・確認など。無ければ null）",
  "labels": ["分類のラベル（経費・税金・行政手続き・医療・保険・契約・給与・学校・住まい などから当てはまるもの。0〜3個）"],
  "summary": "1〜2文の要約"
}}

ファイル名: {name}
本文:
{text}
"""

IMAGE_PROMPT = """この画像を見て、内容を JSON で答えてください。値は日本語で書きます。

{{
  "kind": "document（書類・レシート・紙のスキャン）か photo（写真）か screenshot（画面の写し）か illustration（絵・図）か other",
  "description": "何が写っているかの説明（1〜2文。人物の名前は推測しない）",
  "labels": ["内容のラベル（風景・料理・人物・書類・地図 など。0〜3個）"]
}}

ファイル名: {name}
"""


@dataclass
class Analysis:
    """読み取りの結果1件。"""

    sha256: str
    kind: str  # document / image
    model: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"sha256": self.sha256, "kind": self.kind, "model": self.model, "data": self.data, "error": self.error, "created_at": self.created_at}


def _clean_date(value: Any) -> str | None:
    import datetime as dt

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return dt.date.fromisoformat(value.strip()[:10]).isoformat()
    except ValueError:
        return None


def _clean_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = float(str(value).replace(",", "").replace("¥", "").replace("円", "").strip())
    except ValueError:
        return None
    return int(n) if n.is_integer() else n


def _clean_text(value: Any, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    t = " ".join(value.split())
    return t[:limit] if t and t.lower() not in ("null", "none", "なし", "-") else None


def normalize_document(raw: dict[str, Any]) -> dict[str, Any]:
    """モデルの答え（書類）を、決まった形にそろえる（型の合わない値は捨てる）。"""
    labels = [x for x in (_clean_text(v, 30) for v in raw.get("labels") or [] if isinstance(raw.get("labels"), list)) if x][:3]
    return {
        "doc_type": _clean_text(raw.get("doc_type"), 40),
        "date": _clean_date(raw.get("date")),
        "party": _clean_text(raw.get("party"), 100),
        "amount": _clean_number(raw.get("amount")),
        "currency": _clean_text(raw.get("currency"), 10),
        "due": _clean_date(raw.get("due")),
        "action": _clean_text(raw.get("action"), 200),
        "labels": labels,
        "summary": _clean_text(raw.get("summary"), 400),
    }


def normalize_image(raw: dict[str, Any]) -> dict[str, Any]:
    """モデルの答え（画像）を、決まった形にそろえる。"""
    kind = str(raw.get("kind") or "other").strip().lower()
    if kind not in ("document", "photo", "screenshot", "illustration", "other"):
        kind = "other"
    labels = [x for x in (_clean_text(v, 30) for v in raw.get("labels") or [] if isinstance(raw.get("labels"), list)) if x][:3]
    return {"kind": kind, "description": _clean_text(raw.get("description"), 400), "labels": labels}


def analyze_document(oc: OrganizeConfig, name: str, text: str) -> dict[str, Any]:
    """書類の本文から、項目を読み取る（OllamaError は呼び出し側に任せる）。"""
    prompt = DOCUMENT_PROMPT.format(name=name, text=text[:MAX_TEXT])
    return normalize_document(organize.ollama_generate(oc, prompt))


def analyze_image(oc: OrganizeConfig, name: str, image: bytes) -> tuple[dict[str, Any], str]:
    """画像の内容を推定する。画像を読めるモデルが無ければ OllamaError。（結果, 使ったモデル）を返す。"""
    model = organize.image_model(oc)
    if model is None:
        raise organize.OllamaError("画像を読めるモデルがありません（[organize] vision_model を設定してください）")
    return normalize_image(organize.ollama_generate(oc, IMAGE_PROMPT.format(name=name), image, model=model)), model


def save(conn: sqlite3.Connection, a: Analysis) -> None:
    """結果を保存する（同じ内容ハッシュのものは置き換える）。"""
    with conn:
        conn.execute(
            "INSERT INTO analyses(sha256, kind, model, created_at, data, error) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(sha256) DO UPDATE SET kind = excluded.kind, model = excluded.model, created_at = excluded.created_at,"
            " data = excluded.data, error = excluded.error",
            (a.sha256, a.kind, a.model, a.created_at or utcnow(), json.dumps(a.data, ensure_ascii=False), a.error),
        )


def load(conn: sqlite3.Connection, sha256: str) -> Analysis | None:
    row = conn.execute("SELECT * FROM analyses WHERE sha256 = ?", (sha256,)).fetchone()
    if row is None:
        return None
    return Analysis(row["sha256"], row["kind"], row["model"], json.loads(row["data"]), row["error"], row["created_at"])


def _image_ok(relpath: str) -> bool:
    return fulltext.ext_of(relpath) in extract.IMAGE_EXTS


@dataclass(frozen=True)
class Target:
    """読み取る対象（同じ内容のものは1件にまとめる）。"""

    sha256: str
    relpath: str
    kind: str  # document / image
    mtime_ns: int


def pending(
    conn: sqlite3.Connection, root_id: int, *, images: bool = True, limit: int | None = None, paths: Iterable[str] | None = None, force: bool = False
) -> list[Target]:
    """まだ読み取っていないもの（新しいものから）。paths を渡せば、その中から選ぶ（force なら読み取り済みでも選ぶ）。

    書類は本文を抽出済みのもの（pdftotext・office・text・ocr）、画像は jpg・png などの画像ファイル。
    """
    wanted = set(paths) if paths is not None else None
    rows = conn.execute(
        "SELECT e.relpath, e.sha256, e.mtime_ns, x.method, a.sha256 AS done FROM entries e"
        " LEFT JOIN extracts x ON x.sha256 = e.sha256 LEFT JOIN analyses a ON a.sha256 = e.sha256"
        " WHERE e.root_id = ? AND e.gone_at IS NULL AND e.is_dir = 0 AND e.sha256 IS NOT NULL"
        " AND e.hashed_size IS e.size AND e.hashed_mtime_ns IS e.mtime_ns ORDER BY e.mtime_ns DESC",
        (root_id,),
    )
    out: dict[str, Target] = {}
    for r in rows:
        rel, sha = str(r["relpath"]), str(r["sha256"])
        if wanted is not None and rel not in wanted:
            continue
        if sha in out or (r["done"] is not None and not force):
            continue
        if r["method"] in ("pdftotext", "office", "text", "ocr"):
            kind = "document"
        elif images and _image_ok(rel):
            kind = "image"
        else:
            continue
        out[sha] = Target(sha, rel, kind, int(r["mtime_ns"]))
        if limit and len(out) >= limit:
            break
    return list(out.values())


def run_one(conn: sqlite3.Connection, root: Path, target: Target, oc: OrganizeConfig) -> Analysis:
    """1件を読み取って保存する。モデルの失敗も結果（error）として保存する（同じものを何度も問い合わせないように）。"""
    name = target.relpath.rsplit("/", 1)[-1]
    try:
        if target.kind == "document":
            row = conn.execute("SELECT text FROM extracts WHERE sha256 = ?", (target.sha256,)).fetchone()
            text = str(row["text"]) if row is not None else ""
            data, model = analyze_document(oc, name, text), oc.model
        else:
            path = root / target.relpath
            if path.stat().st_size > extract.MAX_IMAGE_BYTES:
                raise organize.OllamaError("画像が大きすぎる")
            data, model = analyze_image(oc, name, path.read_bytes())
            # 説明を全文検索の索引に入れる（本文を抽出していない画像だけ。OCR の結果は上書きしない）
            if data.get("description"):
                desc = extract.Extracted("describe", extract.normalize_text(" ".join([data["description"], *data["labels"]]), 2000))
                if conn.execute("SELECT 1 FROM extracts WHERE sha256 = ?", (target.sha256,)).fetchone() is None:
                    fulltext.store(conn, target.sha256, desc)
        a = Analysis(target.sha256, target.kind, model, data, "", utcnow())
    except (organize.OllamaError, OSError) as e:
        a = Analysis(target.sha256, target.kind, oc.model, {}, str(e), utcnow())
    save(conn, a)
    return a


def run_pending(
    conn: sqlite3.Connection,
    root_id: int,
    rc: RootConfig,
    config: Config,
    *,
    limit: int | None = None,
    paths: Iterable[str] | None = None,
    force: bool = False,
    on_start: Callable[[int], None] | None = None,
    on_item: Callable[[Target, Analysis], None] | None = None,
) -> list[Analysis]:
    """まだ読み取っていないもの（または paths のもの）を読み取る。"""
    oc, note = organize.config_for_root(config.organize, rc.allow_cloud)
    if note:
        log.info("%s", note)
    targets = pending(conn, root_id, images=config.ai.images, limit=limit, paths=paths, force=force)
    if on_start is not None:
        on_start(len(targets))
    out = []
    for t in targets:
        a = run_one(conn, rc.path, t, oc)
        out.append(a)
        if on_item is not None:
            on_item(t, a)
    return out


# ---------- 確認（承認・却下） ----------


def proposal(a: Analysis) -> dict[str, Any]:
    """読み取りの結果から、付ける候補（タグと属性）を作る。確認の画面で、ここから選んでもらう。"""
    d = a.data
    tags: list[str] = []
    attrs: dict[str, Any] = {}
    if a.kind == "document":
        if d.get("doc_type"):
            tags.append(f"{DOC_TYPE_NS}:{d['doc_type']}")
        tags += [f"{LABEL_NS}:{x}" for x in d.get("labels") or []]
        for key, (name, _type) in FIELD_ATTRS.items():
            if d.get(key) not in (None, ""):
                attrs[name] = d[key]
        if d.get("action") or d.get("due"):
            attrs[FLAG_ATTR] = True
    else:
        if d.get("kind"):
            tags.append(f"{IMAGE_KIND_NS}:{IMAGE_KINDS.get(d['kind'], 'その他')}")
        tags += [f"{LABEL_NS}:{x}" for x in d.get("labels") or []]
    return {"tags": tags, "attrs": attrs}


def review_items(conn: sqlite3.Connection, root_id: int, root: str, decided: set[tuple[str, str]], limit: int = 200) -> list[dict[str, Any]]:
    """確認待ち（読み取ったが、承認も却下もしていない）のもの。新しいものから。

    Args:
        decided: 判断済みの（相対パス, 内容ハッシュ）。
    """
    import unicodedata

    rows = conn.execute(
        "SELECT e.relpath, e.size, e.mtime_ns, a.* FROM entries e JOIN analyses a ON a.sha256 = e.sha256"
        " WHERE e.root_id = ? AND e.gone_at IS NULL AND e.is_dir = 0 AND a.error = '' ORDER BY a.created_at DESC, e.relpath",
        (root_id,),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        if (unicodedata.normalize("NFC", str(r["relpath"])), str(r["sha256"])) in decided:
            continue
        a = Analysis(r["sha256"], r["kind"], r["model"], json.loads(r["data"]), "", r["created_at"])
        prop = proposal(a)
        if not prop["tags"] and not prop["attrs"]:
            continue
        out.append({"path": str(r["relpath"]), "size": int(r["size"]), "analysis": a.to_dict(), "proposal": prop})
        if len(out) >= limit:
            break
    return out


def apply_accept(store: Any, root: str, path: str, tags: Iterable[str], attrs: dict[str, Any]) -> dict[str, int]:
    """承認したタグと属性を付ける（出所は ai）。足りない属性は、決まった型で定義してから付ける。"""
    known = {d["name"]: d for d in store.attr_defs_list()}
    types = {name: t for name, t in FIELD_ATTRS.values()} | {FLAG_ATTR: "flag"}
    n_tags = n_attrs = 0
    for tag in tags:
        n_tags += store.add(root, [path], tag, source="ai")
    for name, value in attrs.items():
        if name not in known:
            if name not in types:
                continue
            store.define_attr(name, types[name])
            known[name] = {"name": name}
        n_attrs += store.set_attr(root, [path], name, value, source="ai")
    return {"tags": n_tags, "attrs": n_attrs}


# ---------- 裏で読み取り続ける ----------


class AnalyzeWorker:
    """Web の画面を動かしている間、新しい書類・画像を裏で読み取る（[ai] auto = true のときだけ）。"""

    def __init__(self, db_path: Path, config: Callable[[], Config], *, idle: float = 600.0, first: float = 120.0) -> None:
        self.db_path = db_path
        self.config = config
        self.idle = idle
        self.first = first
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.current: str | None = None

    def start(self) -> None:
        threading.Thread(target=self._loop, name="analyze", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def poke(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        if self._stop.wait(self.first):
            return
        while not self._stop.is_set():
            worked = False
            try:
                worked = self.run_once()
            except Exception as e:  # noqa: BLE001 - 一時的な失敗（ollama の停止など）で止めない
                log.warning("書類の読み取りに失敗しました: %s", e)
            if not worked:
                self._wake.wait(self.idle)
                self._wake.clear()

    def run_once(self) -> bool:
        """読み取っていないものを1まとまり読み取る。何か読み取ったら True。"""
        config = self.config()
        if not config.ai.auto:
            return False
        conn = connect(self.db_path)
        try:
            for rc in config.roots:
                row = conn.execute("SELECT id FROM roots WHERE name = ?", (rc.name,)).fetchone()
                if row is None:
                    continue
                results = run_pending(
                    conn, int(row["id"]), rc, config, limit=config.ai.batch,
                    on_item=lambda t, a, rc=rc: setattr(self, "current", f"{rc.name}/{t.relpath}"),
                )
                if results:
                    # すべて失敗（ollama に繋がらないなど）なら、間を置いてから試す
                    return any(not a.error for a in results)
            return False
        finally:
            self.current = None
            conn.close()


def is_supported(path: str) -> bool:
    """読み取りの対象になりうる種類か（書類か画像）。"""
    ext = fulltext.ext_of(path)
    return ext in extract.index_exts(False) or ext in extract.IMAGE_EXTS

