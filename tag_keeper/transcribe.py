"""音声・動画の書き起こし（要件 F-EX-2）。faster-whisper を使う（任意の追加の依存 transcribe / transcribe-gpu）。

- 書き起こしは**別のプロセス**で動かす（`python -m tag_keeper.transcribe child ...`）。
  - CTranslate2 は cuDNN を実行時に dlopen で探し、見つからないと**例外ではなくプロセスごと落ちる**ので、
    Web の画面のサーバーを巻き込まないため。GPU のプロセスが落ちたら、CPU でやり直す
  - cuDNN・cuBLAS は pip の nvidia-* のパッケージのものを使う。dlopen に間に合うよう、子のプロセスを起動する前に
    LD_LIBRARY_PATH へ足す（プロセスの中で os.environ を書き換えても間に合わない）
- 書き起こしの文は、区間の始まりの時刻を付けて（[12:34] …）全文検索の索引（extracts、method = transcript）に入れる
- 無音の区間に定型文（「ご視聴ありがとうございました」など）をでっち上げることがあるので、VAD のしきい値を下げて使い（0.2）、
  無音らしさ（no_speech_prob）の高い区間は捨てる
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 書き起こす種類（音声と動画）
MEDIA_EXTS = frozenset({
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".flv", ".wmv", ".mpg", ".mpeg", ".ts", ".m2ts", ".mts", ".3gp",
})
# この値より無音らしい区間は捨てる（無音への作文を落とす）
NO_SPEECH_MAX = 0.6


def available() -> bool:
    """faster-whisper が入っているか（pip install 'tag-keeper[transcribe]'）。"""
    return importlib.util.find_spec("faster_whisper") is not None


def nvidia_lib_dirs() -> list[str]:
    """この環境に pip で入った NVIDIA のライブラリ（cuDNN・cuBLAS など）のフォルダ。"""
    out: list[str] = []
    for base in {sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]}:
        nv = Path(base) / "nvidia"
        if nv.is_dir():
            out += [str(p) for p in sorted(nv.glob("*/lib")) if p.is_dir()]
    return out


def gpu_ready() -> bool:
    """GPU で書き起こせそうか（NVIDIA の GPU が見え、cuDNN がこの環境かシステムにある）。"""
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        if subprocess.run(["nvidia-smi", "-L"], capture_output=True, timeout=10, check=False).returncode != 0:
            return False
    except (OSError, subprocess.TimeoutExpired):
        return False
    if any(Path(d, "libcudnn_ops.so.9").exists() for d in nvidia_lib_dirs()):
        return True
    try:
        res = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True, timeout=10, check=False)
        return "libcudnn_ops.so.9" in res.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


@dataclass(frozen=True)
class Options:
    """書き起こしの設定（[transcribe]）。"""

    model: str = "large-v3-turbo"
    device: str = "auto"  # auto / cuda / cpu
    device_index: int = 0
    compute_type: str = "auto"  # auto なら GPU は float16、CPU は int8
    language: str = ""  # 空なら自動で判定
    vad_threshold: float = 0.2
    max_minutes: int = 240
    max_chars: int = 200_000


def resolve_device(opts: Options) -> str:
    if opts.device in ("cuda", "cpu"):
        return opts.device
    return "cuda" if gpu_ready() else "cpu"


@dataclass
class Result:
    """1件の書き起こしの結果。"""

    path: str
    text: str = ""
    language: str = ""
    duration: float = 0.0
    error: str = ""
    skipped: str = ""  # 長すぎるなどで書き起こさなかった理由


def _fmt_time(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


# ---------- 子のプロセス（faster-whisper を読み込む側） ----------


def _child(job_file: Path, out_file: Path) -> int:
    """子のプロセスの本体。job_file の設定とファイルの一覧を読み、1件ずつ書き起こして out_file に JSONL で書く。"""
    job = json.loads(job_file.read_text(encoding="utf-8"))
    opts = Options(**job["options"])
    device = job["device"]
    compute = opts.compute_type if opts.compute_type != "auto" else ("float16" if device == "cuda" else "int8")
    from faster_whisper import WhisperModel  # 重いので、子のプロセスでだけ読み込む

    model = WhisperModel(opts.model, device=device, device_index=opts.device_index, compute_type=compute)
    with out_file.open("a", encoding="utf-8") as out:
        for path in job["paths"]:
            res: dict[str, Any] = {"path": path, "device": device}
            try:
                segments, info = model.transcribe(
                    path,
                    language=opts.language or None,
                    vad_filter=True,
                    vad_parameters={"threshold": opts.vad_threshold, "min_silence_duration_ms": 1000},
                    condition_on_previous_text=False,
                    beam_size=5,
                )
                res["language"], res["duration"] = info.language, float(info.duration)
                if info.duration > opts.max_minutes * 60:
                    res["skipped"] = f"{int(info.duration // 60)} 分あり、上限（{opts.max_minutes} 分）より長い"
                else:
                    lines = [
                        f"[{_fmt_time(s.start)}] {s.text.strip()}"
                        for s in segments
                        if s.text.strip() and s.no_speech_prob <= NO_SPEECH_MAX
                    ]
                    res["text"] = "\n".join(lines)[: opts.max_chars]
            except Exception as e:  # noqa: BLE001 - 1件の失敗で残りを止めない
                res["error"] = f"{type(e).__name__}: {e}"[:300]
            out.write(json.dumps(res, ensure_ascii=False) + "\n")
            out.flush()
    return 0


# ---------- 親のプロセス（サーバー・CLI の側） ----------


def child_command() -> list[str]:
    """子のプロセスを起動するコマンド（テストで差し替える）。"""
    return [sys.executable, "-m", "tag_keeper.transcribe", "child"]


def run_batch(paths: list[Path], opts: Options, *, on_result: Callable[[Result], None] | None = None, timeout: float = 6 * 3600) -> list[Result]:
    """ファイルをまとめて、別のプロセスで書き起こす。GPU のプロセスが途中で落ちたら、残りを CPU でやり直す。"""
    results: dict[str, Result] = {}
    todo = [str(p) for p in paths]
    devices = [resolve_device(opts)]
    if devices[0] == "cuda":
        devices.append("cpu")
    for device in devices:
        if not todo:
            break
        with tempfile.TemporaryDirectory(prefix="tag-keeper-transcribe-") as tmp:
            job_file, out_file = Path(tmp) / "job.json", Path(tmp) / "out.jsonl"
            job_file.write_text(json.dumps({"options": opts.__dict__, "device": device, "paths": todo}, ensure_ascii=False), encoding="utf-8")
            env = dict(os.environ)
            if device == "cuda":
                env["LD_LIBRARY_PATH"] = ":".join([*nvidia_lib_dirs(), env.get("LD_LIBRARY_PATH", "")]).strip(":")
            log.info("書き起こします（%s・%s）: %d 件", opts.model, device, len(todo))
            # 他の処理の邪魔をしないよう、優先度を下げる
            cmd = (["nice", "-n", "10"] if shutil.which("nice") else []) + child_command() + [str(job_file), str(out_file)]
            seen = 0
            try:
                proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                # 結果のファイルを読みながら待つ（1件ごとに知らせる）
                while True:
                    try:
                        proc.wait(timeout=2)
                        done = True
                    except subprocess.TimeoutExpired:
                        done = False
                    if out_file.exists():
                        lines = out_file.read_text(encoding="utf-8").splitlines()
                        for line in lines[seen:]:
                            d = json.loads(line)
                            r = Result(d["path"], d.get("text", ""), d.get("language", ""), float(d.get("duration") or 0), d.get("error", ""), d.get("skipped", ""))
                            results[r.path] = r
                            if on_result is not None:
                                on_result(r)
                        seen = len(lines)
                    if done:
                        break
                stderr = proc.stderr.read() if proc.stderr else ""
                code = proc.returncode
            except OSError as e:
                stderr, code = str(e), -1
            todo = [p for p in todo if p not in results]
            if todo:
                last = (stderr.strip().splitlines() or [f"終了コード {code}"])[-1][:300]
                log.warning("書き起こしのプロセスが途中で止まりました（%s・残り %d 件）: %s", device, len(todo), last)
                if device == devices[-1]:
                    for p in todo:
                        r = Result(p, error=f"書き起こしのプロセスが止まりました: {last}")
                        results[p] = r
                        if on_result is not None:
                            on_result(r)
    return [results[str(p)] for p in paths if str(p) in results]


@dataclass(frozen=True)
class Target:
    sha256: str
    relpath: str
    size: int


def pending(conn: sqlite3.Connection, root_id: int, *, limit: int | None = None, paths: Iterable[str] | None = None,
            force: bool = False, app_data: Iterable[str] = (), no_extract: Iterable[str] = ()) -> list[Target]:
    """まだ書き起こしていない音声・動画（新しいものから。同じ内容は1件）。

    作り直せる生成物・バージョン管理・アプリのデータのフォルダの中は対象にしない（全文検索の索引と同じ）。
    """
    from tag_keeper import fulltext

    import unicodedata

    wanted = set(paths) if paths is not None else None
    app = frozenset(app_data)
    closed = [unicodedata.normalize("NFC", p) for p in no_extract]
    rows = conn.execute(
        "SELECT e.relpath, e.sha256, e.size, x.method FROM entries e LEFT JOIN extracts x ON x.sha256 = e.sha256"
        " WHERE e.root_id = ? AND e.gone_at IS NULL AND e.is_dir = 0 AND e.sha256 IS NOT NULL"
        " AND e.hashed_size IS e.size AND e.hashed_mtime_ns IS e.mtime_ns ORDER BY e.mtime_ns DESC",
        (root_id,),
    )
    out: dict[str, Target] = {}
    for r in rows:
        rel, sha = str(r["relpath"]), str(r["sha256"])
        if wanted is not None and rel not in wanted:
            continue
        if fulltext.ext_of(rel) not in MEDIA_EXTS or sha in out:
            continue
        if r["method"] is not None and not force:
            continue
        if wanted is None and fulltext._skipped(rel, app):
            continue
        if wanted is None and closed and any(unicodedata.normalize("NFC", rel).startswith(c + "/") for c in closed):
            continue
        out[sha] = Target(sha, rel, int(r["size"]))
        if limit and len(out) >= limit:
            break
    return list(out.values())


def store_result(conn: sqlite3.Connection, sha256: str, r: Result) -> None:
    """書き起こしの結果を、全文検索の索引に入れる（失敗・長すぎて飛ばしたものも、理由を残して二度と試さない）。"""
    from tag_keeper import extract, fulltext

    if r.error:
        ex = extract.Extracted("error", note=r.error)
    elif r.skipped:
        ex = extract.Extracted("none", note=r.skipped)
    else:
        note = f"言語: {r.language}・長さ: {_fmt_time(r.duration)}"
        ex = extract.Extracted("transcript", extract.normalize_text(r.text, len(r.text) + 1), note=note)
    fulltext.store(conn, sha256, ex)


def run_pending(conn: sqlite3.Connection, root_id: int, root: Path, opts: Options, *, limit: int | None = None,
                paths: Iterable[str] | None = None, force: bool = False, app_data: Iterable[str] = (), no_extract: Iterable[str] = (),
                on_start: Callable[[int], None] | None = None, on_item: Callable[[Target, Result], None] | None = None) -> list[Result]:
    """まだ書き起こしていないもの（または paths のもの）を書き起こして保存する。"""
    targets = pending(conn, root_id, limit=limit, paths=paths, force=force, app_data=app_data, no_extract=no_extract)
    if on_start is not None:
        on_start(len(targets))
    if not targets:
        return []
    by_path = {str(root / t.relpath): t for t in targets}

    def handle(r: Result) -> None:
        t = by_path.get(r.path)
        if t is None:
            return
        store_result(conn, t.sha256, r)
        if on_item is not None:
            on_item(t, r)

    return run_batch([root / t.relpath for t in targets], opts, on_result=handle)


class TranscribeWorker:
    """Web の画面を動かしている間、新しい音声・動画を裏で書き起こす（[transcribe] auto = true のときだけ）。"""

    def __init__(self, db_path: Path, config: Callable[[], Any], *, idle: float = 900.0, first: float = 180.0) -> None:
        self.db_path, self.config, self.idle, self.first = db_path, config, idle, first
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.current: str | None = None

    def start(self) -> None:
        threading.Thread(target=self._loop, name="transcribe", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        if self._stop.wait(self.first):
            return
        while not self._stop.is_set():
            worked = False
            try:
                worked = self.run_once()
            except Exception as e:  # noqa: BLE001 - 一時的な失敗で止めない
                log.warning("書き起こしに失敗しました: %s", e)
            if not worked:
                self._wake.wait(self.idle)
                self._wake.clear()

    def run_once(self) -> bool:
        from tag_keeper.catalog import connect
        from tag_keeper.fulltext import app_data_names

        config = self.config()
        tc = config.transcribe
        if not tc.auto or not available():
            return False
        conn = connect(self.db_path)
        try:
            for rc in config.roots:
                row = conn.execute("SELECT id FROM roots WHERE name = ?", (rc.name,)).fetchone()
                if row is None:
                    continue
                from tag_keeper.tags import TagStore

                results = run_pending(conn, int(row["id"]), rc.path, tc.options(), limit=tc.batch, app_data=app_data_names(config),
                                      no_extract=TagStore(config.tags.dir).no_extract_of(rc.name),
                                      on_item=lambda t, r, rc=rc: setattr(self, "current", f"{rc.name}/{t.relpath}"))
                if results:
                    return any(not r.error for r in results)
            return False
        finally:
            self.current = None
            conn.close()


def main(argv: list[str] | None = None) -> int:
    """子のプロセスの入口（python -m tag_keeper.transcribe child JOB OUT）。"""
    ap = argparse.ArgumentParser(prog="python -m tag_keeper.transcribe", description="tag-keeper の書き起こしの子のプロセス（内部用）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("child", help="ジョブのファイルの一覧を書き起こし、JSONL に書く")
    p.add_argument("job", type=Path)
    p.add_argument("out", type=Path)
    args = ap.parse_args(argv)
    return _child(args.job, args.out)


if __name__ == "__main__":
    sys.exit(main())
