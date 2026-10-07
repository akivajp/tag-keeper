"""受け皿のフォルダ（tmp・未整理など）のファイルについて、名前と移動先を提案する（要件 F-OR-3・4、F-AI-1 の一部）。

流れ（1ファイルごと）:

1. 内容を取り出す（extract）。文字が無ければ画像をモデルに見せる
2. モデルに内容を読み取らせる（題名・種類・相手・キーワード）
3. 移動先の候補を絞る。既存のフォルダの「パス + 中のファイル名」と、2 の結果の
   文字の2文字組（bigram）の TF-IDF の近さで、上位の数件を選ぶ（新しい分類体系は作らない。F-OR-4）
4. 候補のフォルダの命名規則（日付の書式・区切り）と実例を添えて、手元の ollama のモデルに
   名前（既定は YYYYMMDD_題名）・要約・移動先（候補の中から）・タグの候補を JSON で答えさせる
5. 結果を内容ハッシュ・モデル・指示文の版をキーに保存する（同じファイルは移動・改名しても作り直さない）

提案は提案のまま保存し、利用者が画面で採用したものだけが整理プランの「移動・改名」になる（P2）。
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import math
import re
import sqlite3
import unicodedata
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from tag_keeper import rules
from tag_keeper.catalog import utcnow
from tag_keeper.config import OrganizeConfig
from tag_keeper.extract import Extracted, extract
from tag_keeper.hashing import sha256_file

log = logging.getLogger(__name__)

# 指示文を変えたら上げる（保存した提案を作り直すため）
PROMPT_VERSION = 2
# 移動先の候補にするフォルダの深さの上限
MAX_DEPTH = 8
# 候補のフォルダの特徴に使う、中のファイル名の数
PROFILE_NAMES = 40


class OllamaError(RuntimeError):
    """ollama に問い合わせられないときの例外。"""


# --- 受け皿のフォルダとファイル ---


def is_inbox_name(name: str, patterns: Iterable[str]) -> bool:
    """フォルダ名が受け皿のパターンのどれかに一致するか（大文字・小文字を区別しない）。"""
    lower = name.lower()
    return any(fnmatchcase(lower, p.lower()) for p in patterns)


def _hidden_or_vcs(relpath: str) -> bool:
    """隠しフォルダ・ファイルか、バージョン管理の管理フォルダの中か。"""
    return any(part.startswith(".") for part in relpath.split("/"))


def _under_any(relpath: str, dirs: Iterable[str]) -> bool:
    return any(relpath == d or relpath.startswith(d + "/") for d in dirs)


@dataclass
class InboxFile:
    """受け皿にあるファイル1件。"""

    relpath: str
    size: int
    mtime_ns: int
    sha256: str | None  # カタログで計算済みで、計算後に変わっていなければその値


def find_inbox(
    conn: sqlite3.Connection,
    root_id: int,
    patterns: Sequence[str],
    excluded: Iterable[str] = (),
) -> tuple[list[str], list[InboxFile]]:
    """受け皿のフォルダと、その配下のファイルを返す。

    Args:
        conn: カタログへの接続。
        root_id: ルートの ID。
        patterns: 受け皿とみなすフォルダ名のパターン。
        excluded: 対象から外すフォルダ（アプリのデータ・作り直せる生成物など、レポートの候補のフォルダ）。
    """
    excluded = list(excluded)
    dirs = [
        r["relpath"]
        for r in conn.execute("SELECT relpath FROM entries WHERE root_id = ? AND gone_at IS NULL AND is_dir = 1", (root_id,))
    ]
    inbox = sorted(
        d
        for d in dirs
        if is_inbox_name(d.rsplit("/", 1)[-1], patterns) and not _hidden_or_vcs(d) and not _under_any(d, excluded)
    )
    # 受け皿の中の受け皿（99_Inbox/未整理のドキュメント など）は、上のものに含める
    top = [d for d in inbox if not any(d.startswith(o + "/") for o in inbox if o != d)]
    files: list[InboxFile] = []
    for r in conn.execute(
        "SELECT relpath, size, mtime_ns, sha256, hashed_size, hashed_mtime_ns FROM entries"
        " WHERE root_id = ? AND gone_at IS NULL AND is_dir = 0 ORDER BY relpath",
        (root_id,),
    ):
        rel = r["relpath"]
        if not _under_any(rel, top) or _hidden_or_vcs(rel) or _under_any(rel, excluded):
            continue
        match = rules.classify_file(rel, r["size"])
        if match is not None and match.category in (rules.CAT_OS_JUNK, rules.CAT_OFFICE_LOCK):
            continue
        current = r["sha256"] if (r["hashed_size"], r["hashed_mtime_ns"]) == (r["size"], r["mtime_ns"]) else None
        files.append(InboxFile(rel, r["size"], r["mtime_ns"], current))
    return top, files


# --- 移動先の候補（既存のフォルダから選ぶ） ---


def _bigrams(text: str) -> list[str]:
    """表記をそろえ（NFKC・小文字）、数字と記号を除いた文字の2文字組。日本語にも効く。"""
    t = unicodedata.normalize("NFKC", text).lower()
    t = re.sub(r"[\d\W_]+", " ", t)
    out: list[str] = []
    for word in t.split():
        if len(word) == 1:
            out.append(word)
        out.extend(word[i : i + 2] for i in range(len(word) - 1))
    return out


@dataclass
class Folder:
    """移動先の候補にできる既存のフォルダ。"""

    relpath: str
    names: list[str]  # 中のファイル名（命名規則の判定と、モデルに見せる実例）
    vector: dict[str, float] = field(default_factory=dict, repr=False)


class FolderIndex:
    """既存のフォルダの「パス + 中のファイル名」の TF-IDF の索引。"""

    def __init__(self, folders: list[Folder], all_dirs: Iterable[str] = ()) -> None:
        self.folders = folders
        # 既存のすべてのフォルダ（日付のフォルダの置き換え先が既にあるかの判定に使う）
        self.all_dirs = set(all_dirs) | {f.relpath for f in folders}
        df: Counter[str] = Counter()
        raw: list[Counter[str]] = []
        for f in folders:
            # パスの語は中のファイル名より重く見る（フォルダの意味はまず名前に表れる）
            c = Counter(_bigrams(f.relpath.replace("/", " ")) * 3)
            c.update(_bigrams(" ".join(Path(n).stem for n in f.names)))
            raw.append(c)
            df.update(c.keys())
        n = max(len(folders), 1)
        self.idf = {t: math.log(1 + n / d) for t, d in df.items()}
        self.postings: dict[str, list[tuple[int, float]]] = defaultdict(list)
        for i, (f, c) in enumerate(zip(folders, raw, strict=True)):
            vec = {t: (1 + math.log(v)) * self.idf[t] for t, v in c.items()}
            norm = math.sqrt(sum(w * w for w in vec.values())) or 1.0
            f.vector = {t: w / norm for t, w in vec.items()}
            for t, w in f.vector.items():
                self.postings[t].append((i, w))

    def search(self, text: str, k: int, exclude: Iterable[str] = ()) -> list[tuple[Folder, float]]:
        """text に近いフォルダを上位 k 件返す。"""
        q = Counter(_bigrams(text))
        vec = {t: (1 + math.log(v)) * self.idf[t] for t, v in q.items() if t in self.idf}
        norm = math.sqrt(sum(w * w for w in vec.values())) or 1.0
        scores: dict[int, float] = defaultdict(float)
        for t, w in vec.items():
            for i, fw in self.postings.get(t, ()):
                scores[i] += (w / norm) * fw
        skip = set(exclude)
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        out = [(self.folders[i], s) for i, s in ranked if self.folders[i].relpath not in skip]
        return out[:k]


def build_folder_index(
    conn: sqlite3.Connection,
    root_id: int,
    inbox: Iterable[str],
    excluded: Iterable[str] = (),
) -> FolderIndex:
    """移動先の候補になる既存のフォルダ（中にファイルがあるもの）の索引を作る。受け皿と除外の配下は含めない。"""
    skip = list(inbox) + list(excluded)
    all_dirs = [
        r["relpath"] for r in conn.execute("SELECT relpath FROM entries WHERE root_id = ? AND gone_at IS NULL AND is_dir = 1", (root_id,))
    ]
    names: dict[str, list[str]] = defaultdict(list)
    for r in conn.execute("SELECT relpath FROM entries WHERE root_id = ? AND gone_at IS NULL AND is_dir = 0", (root_id,)):
        rel = r["relpath"]
        parent, _, name = rel.rpartition("/")
        if parent == "" or _hidden_or_vcs(rel) or _under_any(parent, skip) or parent.count("/") >= MAX_DEPTH:
            continue
        if len(names[parent]) < PROFILE_NAMES:
            names[parent].append(name)
    return FolderIndex([Folder(d, sorted(ns)) for d, ns in sorted(names.items())], all_dirs)


def adjust_dated_folder(dest: str, date: str | None, known: set[str]) -> tuple[str, str]:
    """移動先のパスに年・月があれば、書類の日付の年・月のフォルダに置き換える。

    例: 書類の日付が 20250414 で、候補が「領収書/2022年/2022年08月」なら「領収書/2025年/2025年04月」。
    置き換えたフォルダが既にあればそれを、無くても年か親のフォルダがあれば新しいフォルダとして返す
    （日付で分けたフォルダの型に沿う新設だけを提案する。F-OR-4）。

    Returns:
        （移動先, 注記）。置き換えなければ（元の移動先, ""）。
    """
    if not date:
        return dest, ""
    y, m = date[:4], date[4:6]
    # 名前全体が日付のフォルダ（2022・2022年・2022年08月・202208・2022-08）だけを置き換える。
    # 「202108_報告」のように日付の後に題名が続くフォルダは、案件などの名前なので変えない
    styles = [
        (re.compile(r"^(19|20)\d{2}年\d{1,2}月$"), f"{y}年{m}月"),
        (re.compile(r"^(19|20)\d{2}-\d{2}$"), f"{y}-{m}"),
        (re.compile(r"^(19|20)\d{4}$"), f"{y}{m}"),
        (re.compile(r"^(19|20)\d{2}年$"), f"{y}年"),
        (re.compile(r"^(19|20)\d{2}$"), y),
    ]
    parts = dest.split("/")
    for k, part in enumerate(parts):
        for pat, repl in styles:
            if pat.match(part):
                parts[k] = repl
                break
    cand = "/".join(parts)
    if cand == dest:
        return dest, ""
    if cand in known:
        return cand, "書類の日付の年・月のフォルダに置き換えた"
    parent = cand.rpartition("/")[0]
    if parent in known or parent == "":
        return cand, "書類の日付の年・月のフォルダを新しく作る"
    return dest, ""


# --- 命名規則 ---

_DATE_STYLES = [
    ("YYYYMMDD_", re.compile(r"^(19|20)\d{6}[_\- ]")),
    ("YYYY-MM-DD_", re.compile(r"^(19|20)\d{2}-\d{2}-\d{2}")),
    ("YYYYMM_", re.compile(r"^(19|20)\d{4}[_\- ]")),
    ("YYYY_", re.compile(r"^(19|20)\d{2}[_\- ]")),
]


def naming_style(names: Sequence[str]) -> str:
    """フォルダの中のファイル名から、日付の付け方の多数派を返す（無ければ「日付なし」）。"""
    if not names:
        return "不明"
    counts: Counter[str] = Counter()
    for n in names:
        style = next((label for label, pat in _DATE_STYLES if pat.match(n)), "日付なし")
        counts[style] += 1
    style, count = counts.most_common(1)[0]
    return f"{style}（{count}/{len(names)} 件）"


# --- ollama ---


def ollama_generate(
    config: OrganizeConfig, prompt: str, image: bytes | None = None, *, fmt: str | dict[str, Any] = "json"
) -> dict[str, Any]:
    """ollama の generate API に問い合わせ、JSON の答えを返す。"""
    body: dict[str, Any] = {
        "model": config.model,
        "prompt": prompt,
        "format": fmt,
        "stream": False,
        "options": {"temperature": config.temperature},
    }
    if image is not None:
        body["images"] = [base64.b64encode(image).decode("ascii")]
    req = urllib.request.Request(
        config.ollama_url.rstrip("/") + "/api/generate",
        json.dumps(body).encode("utf-8"),
        {"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=config.timeout) as res:
            data = json.load(res)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise OllamaError(f"ollama に問い合わせられません（{config.ollama_url}）: {e}") from e
    try:
        out = json.loads(data.get("response", ""))
    except json.JSONDecodeError as e:
        raise OllamaError(f"モデルの答えを JSON として読めません: {data.get('response', '')[:200]}") from e
    if not isinstance(out, dict):
        raise OllamaError(f"モデルの答えの形が想定と違います: {str(out)[:200]}")
    return out


def check_ollama(config: OrganizeConfig) -> None:
    """ollama が動いていて、設定のモデルがあるかを確かめる。無ければ OllamaError。"""
    try:
        with urllib.request.urlopen(config.ollama_url.rstrip("/") + "/api/tags", timeout=10) as res:
            models = {m["name"] for m in json.load(res).get("models", [])}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise OllamaError(f"ollama に接続できません（{config.ollama_url}）: {e}") from e
    if config.model not in models and f"{config.model}:latest" not in models:
        raise OllamaError(f"ollama にモデル {config.model} がありません（ollama pull {config.model}）")


# --- 提案 ---

UNDERSTAND_PROMPT = """ファイルの整理のために、次のファイルの内容を読み取ってください。
- 今の名前: {name}
{body}
書類なら、書類の種類・発行元や相手・日付・題名が分かるように。写真やスクリーンショットなら、何が写っているかを。
JSON で答える: {{"title": "短い題名", "doc_type": "書類の種類（請求書・領収書・契約書・明細書・写真・スクリーンショットなど）", "date": "書類の中の日付 YYYYMMDD または null", "parties": ["発行元・相手・関係する人や組織"], "keywords": ["分類の手がかりになる語（5つまで）"], "summary": "一文の要約"}}"""

NAMING_PROMPT = """あなたはファイルの整理を手伝います。受け皿のフォルダにあるファイルについて、分かりやすい名前と、移動先のフォルダを提案してください。

# ファイル
- 今の名前: {name}
- 今の場所: {folder}
- ファイルの更新日: {mtime}
- 内容の取り出し方: {method}

# 内容
{content}

# 移動先の候補（既存のフォルダ。番号: パス ／ 日付の付け方 ／ 中のファイル名の例）
{candidates}

# 名前の付け方
- 既定の書式は「YYYYMMDD_題名」。日付は書類の発行日・作成日・対象期間など、書類の中の日付を優先する。分からなければファイルの更新日を使う。
- 移動先の候補のフォルダに決まった命名の慣習（日付の書式・区切り・語順）があれば、それに合わせる。
- 今の名前に意味のある題名が既に付いていれば、それを尊重して足りない部分だけを補う。番号やカメラの連番、downloadfile のような意味の無い名前なら付け直す。
- 題名は短く具体的に（誰の・何の書類か）。日本語でよい。\\ / : * ? " < > | は使わない。拡張子は付けない。

# 移動先の選び方
- 候補の番号から、ふさわしい順に最大3つ。どれも合わなければ空の配列にする。新しいフォルダは作らない。

# 答え方（JSON）
{{"date": "YYYYMMDD または null", "date_source": "content / file_date / none", "title": "題名", "new_name": "拡張子なしの新しい名前", "summary": "一文の要約", "doc_type": "書類の種類（請求書・領収書・契約書・写真・スクリーンショットなど）", "destinations": [{{"index": 候補の番号, "reason": "理由"}}], "tags": ["種別:請求書", "相手:〇〇"]}}"""

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def clean_name(new_name: str, original: str) -> str:
    """モデルが出した名前を、ファイル名として使える形に直し、元の拡張子（小文字）を付ける。"""
    ext = Path(original).suffix.lower()
    stem = unicodedata.normalize("NFC", new_name).strip()
    if ext and stem.lower().endswith(ext):
        stem = stem[: -len(ext)]
    stem = _BAD_CHARS.sub("_", stem).strip(" .")
    stem = re.sub(r"\s+", " ", stem)
    if not stem:
        stem = Path(original).stem
    return stem[:150] + ext


@dataclass
class Suggestion:
    """1ファイルへの提案。"""

    relpath: str
    sha256: str
    model: str
    method: str = ""
    new_name: str = ""
    title: str = ""
    date: str | None = None
    date_source: str = ""
    summary: str = ""
    doc_type: str = ""
    destinations: list[dict[str, Any]] = field(default_factory=list)  # [{"relpath", "reason", "score"}]
    tags: list[str] = field(default_factory=list)
    note: str = ""
    error: str = ""
    created_at: str = ""
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        """画面用の辞書。"""
        return asdict(self)


def _format_candidates(cands: Sequence[tuple[Folder, float]]) -> str:
    lines = []
    for i, (f, _score) in enumerate(cands, 1):
        examples = " / ".join(f.names[:4])
        lines.append(f"{i}: {f.relpath} ／ {naming_style(f.names)} ／ {examples}")
    return "\n".join(lines) or "（候補なし）"


def suggest_file(
    root_path: Path,
    item: InboxFile,
    sha256: str,
    index: FolderIndex,
    config: OrganizeConfig,
    inbox: Sequence[str],
) -> Suggestion:
    """1ファイルの名前と移動先を提案する（ollama に2回問い合わせる）。"""
    path = root_path / item.relpath
    sug = Suggestion(item.relpath, sha256, config.model, created_at=utcnow())
    ex: Extracted = extract(path, config.max_chars, config.use_images)
    sug.method, sug.note = ex.method, ex.note
    use_image = ex.image is not None and len(ex.text) < 30
    if use_image:
        body = "- 内容: 添付の画像"
    elif ex.text.strip():
        body = "- 内容（抜粋）:\n" + ex.text[: config.max_chars]
    else:
        body = "- 内容: 読めない（名前と場所から判断する）"
    # 1回目: 内容を理解させる（題名・種類・相手・キーワード）。本文そのものより、これで移動先を絞る方が
    # 数字や定型文に引っ張られない
    info = ollama_generate(config, UNDERSTAND_PROMPT.format(name=Path(item.relpath).name, body=body), ex.image if use_image else None)
    words = [str(info.get(k) or "") for k in ("title", "doc_type", "summary")]
    for k in ("parties", "keywords"):
        v = info.get(k)
        if isinstance(v, list):
            words.extend(str(x) for x in v)
    query = f"{Path(item.relpath).stem} " + " ".join(words)
    content = "（読み取った内容）\n" + json.dumps(info, ensure_ascii=False)
    if ex.text.strip():
        content += "\n（本文の抜粋）\n" + ex.text[:1500]
    cands = index.search(query, config.candidates, exclude=inbox)
    mtime = dt.datetime.fromtimestamp(item.mtime_ns / 1e9).strftime("%Y-%m-%d")
    folder = item.relpath.rpartition("/")[0]
    prompt = NAMING_PROMPT.format(
        name=Path(item.relpath).name,
        folder=folder,
        mtime=mtime,
        method=ex.method,
        content=content[: config.max_chars],
        candidates=_format_candidates(cands),
    )
    # 2回目: 名前と移動先。画像はもう一度見せる（名前の精度が上がる）
    out = ollama_generate(config, prompt, ex.image if use_image else None)
    sug.new_name = clean_name(str(out.get("new_name") or ""), item.relpath)
    sug.title = str(out.get("title") or "")
    date = str(out.get("date") or "")
    sug.date = date if re.fullmatch(r"(19|20)\d{6}", date) else None
    sug.date_source = str(out.get("date_source") or "")
    sug.summary = str(out.get("summary") or "")
    sug.doc_type = str(out.get("doc_type") or "")
    tags = out.get("tags") or []
    sug.tags = [str(t) for t in tags if isinstance(t, str | int)][:5] if isinstance(tags, list) else []
    seen: set[str] = set()
    for d in out.get("destinations") or []:
        try:
            i = int(d.get("index")) if isinstance(d, dict) else int(d)
        except (TypeError, ValueError):
            continue
        if 1 <= i <= len(cands):
            f, score = cands[i - 1]
            dest, note = adjust_dated_folder(f.relpath, sug.date, index.all_dirs)
            if dest in seen:
                continue
            seen.add(dest)
            reason = str(d.get("reason", "")) if isinstance(d, dict) else ""
            sug.destinations.append(
                {"relpath": dest, "reason": reason + (f"（{note}）" if note else ""), "score": round(score, 3), "new": dest not in index.all_dirs}
            )
    return sug


def load_cached(conn: sqlite3.Connection, sha256: str, model: str) -> Suggestion | None:
    """保存した提案を読む。無ければ None。"""
    row = conn.execute(
        "SELECT data FROM suggestions WHERE sha256 = ? AND model = ? AND prompt_version = ?",
        (sha256, model, PROMPT_VERSION),
    ).fetchone()
    if row is None:
        return None
    data = json.loads(row["data"])
    sug = Suggestion(**{k: v for k, v in data.items() if k in Suggestion.__dataclass_fields__})
    sug.cached = True
    return sug


def save(conn: sqlite3.Connection, sug: Suggestion) -> None:
    """提案を保存する（エラーになったものは保存しない。次回にやり直す）。"""
    if sug.error:
        return
    data = sug.to_dict()
    data.pop("cached", None)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO suggestions(sha256, model, prompt_version, created_at, data) VALUES (?, ?, ?, ?, ?)",
            (sug.sha256, sug.model, PROMPT_VERSION, sug.created_at, json.dumps(data, ensure_ascii=False)),
        )


def _hash_and_record(conn: sqlite3.Connection, root_id: int, root_path: Path, item: InboxFile) -> str:
    """ハッシュが未計算のファイルを計算し、変わっていなければカタログにも保存する。"""
    sha = sha256_file(root_path / item.relpath)
    with conn:
        conn.execute(
            "UPDATE entries SET sha256 = ?, hashed_size = size, hashed_mtime_ns = mtime_ns"
            " WHERE root_id = ? AND relpath = ? AND gone_at IS NULL AND size = ? AND mtime_ns = ?",
            (sha, root_id, item.relpath, item.size, item.mtime_ns),
        )
    item.sha256 = sha
    return sha


def run_suggestions(
    conn: sqlite3.Connection,
    root_id: int,
    root_path: Path,
    config: OrganizeConfig,
    excluded: Iterable[str],
    *,
    only: Iterable[str] | None = None,
    force: bool = False,
    on_start: Callable[[int], None] | None = None,
    on_done: Callable[[Suggestion], None] | None = None,
) -> list[Suggestion]:
    """受け皿のファイルについて提案を作る（保存済みのものは使い回す）。

    Args:
        conn: カタログへの接続。
        root_id: ルートの ID。
        root_path: ルートの絶対パス。
        config: 整理の提案の設定。
        excluded: 対象と移動先から外すフォルダ。
        only: この相対パスのファイルだけを対象にする（None なら受け皿のすべて）。
        force: 保存済みの提案があっても作り直す。
        on_start: 対象の件数を渡して、始める前に呼ぶ。
        on_done: 1件終わるたびに呼ぶ。
    """
    excluded = list(excluded)
    inbox, files = find_inbox(conn, root_id, config.inbox_patterns, excluded)
    if only is not None:
        wanted = set(only)
        files = [f for f in files if f.relpath in wanted]
    if on_start is not None:
        on_start(len(files))
    index: FolderIndex | None = None
    out: list[Suggestion] = []
    for item in files:
        try:
            sha = item.sha256 or _hash_and_record(conn, root_id, root_path, item)
        except OSError as e:
            sug = Suggestion(item.relpath, "", config.model, error=f"読めない: {e}")
            out.append(sug)
            if on_done:
                on_done(sug)
            continue
        cached = None if force else load_cached(conn, sha, config.model)
        if cached is not None:
            cached.relpath = item.relpath
            sug = cached
        else:
            if index is None:
                check_ollama(config)
                index = build_folder_index(conn, root_id, inbox, excluded)
                log.info("移動先の候補にするフォルダ: %s 件", f"{len(index.folders):,}")
            try:
                sug = suggest_file(root_path, item, sha, index, config, inbox)
                save(conn, sug)
            except OllamaError as e:
                sug = Suggestion(item.relpath, sha, config.model, error=str(e))
        out.append(sug)
        if on_done is not None:
            on_done(sug)
    return out


def cached_suggestions(conn: sqlite3.Connection, files: Sequence[InboxFile], model: str) -> dict[str, Suggestion]:
    """受け皿のファイルについて、保存済みの提案だけを返す（相対パス → 提案）。画面の表示用。"""
    out: dict[str, Suggestion] = {}
    for f in files:
        if f.sha256 is None:
            continue
        sug = load_cached(conn, f.sha256, model)
        if sug is not None:
            sug.relpath = f.relpath
            out[f.relpath] = sug
    return out
