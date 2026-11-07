"""ブラウザが直接は再生できない動画（flv・mkv・avi など）を、再生できる MP4 にする。

ffmpeg（と ffprobe）を使う。中の映像が H.264 なら、映像は作り直さずに入れ物だけを MP4 に替える
（速く、画質も落ちない）。それ以外は H.264 と AAC に作り直す。音声も AAC・MP3 ならそのまま使う。

- できた MP4 は内容ハッシュをキーに保存し、2回目からは作らない（シークもできる）
- 作るのは裏のスレッドで行い、進み具合（%）を画面に返す。同じファイルを同時に2回作らない
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ブラウザがそのまま再生できる種類
NATIVE_VIDEO = frozenset({".mp4", ".m4v", ".webm", ".mov", ".ogv"})
# ffmpeg で MP4 にすれば再生できる種類
CONVERT_VIDEO = frozenset({".flv", ".f4v", ".mkv", ".avi", ".wmv", ".asf", ".mpg", ".mpeg", ".ts", ".m2ts", ".mts", ".3gp", ".vob", ".divx"})
# そのまま使える音声
COPY_AUDIO = frozenset({"aac", "mp3"})
# 再生用の MP4 を保存しておく容量の上限（超えたら、しばらく使っていないものから消す。元のファイルには触れない）
CACHE_MAX_BYTES = 5 * 1024**3


class MediaError(RuntimeError):
    """動画を変換できないときの例外。"""


def available() -> bool:
    """ffmpeg と ffprobe が使えるか。"""
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def probe(path: Path) -> dict[str, Any]:
    """ffprobe で、最初の映像・音声の種類と長さ（秒）を調べる。"""
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt:format=duration", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise MediaError(f"ffprobe を実行できません: {e}") from e
    if res.returncode != 0:
        raise MediaError(f"動画として読めません: {res.stderr.strip()[:200]}")
    data = json.loads(res.stdout or "{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    try:
        duration = float(data.get("format", {}).get("duration") or 0)
    except ValueError:
        duration = 0.0
    return {
        "video": video.get("codec_name") if video else None,
        "pix_fmt": video.get("pix_fmt") if video else None,
        "audio": audio.get("codec_name") if audio else None,
        "duration": duration,
    }


def ffmpeg_args(info: dict[str, Any]) -> tuple[list[str], str]:
    """変換の ffmpeg の引数（入力と出力を除く）と、方式（copy: 入れ物だけ替える / transcode: 作り直す）。"""
    if info["video"] is None:
        raise MediaError("映像がありません")
    # 8bit の H.264 なら、映像はそのまま使う（10bit などはブラウザで再生できないことがある）
    copy_video = info["video"] == "h264" and (info.get("pix_fmt") or "yuv420p") in ("yuv420p", "yuvj420p")
    args = ["-map", "0:v:0", "-map", "0:a:0?"]
    args += ["-c:v", "copy"] if copy_video else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
    if info["audio"] is None or info["audio"] in COPY_AUDIO:
        args += ["-c:a", "copy"]
    else:
        args += ["-c:a", "aac", "-b:a", "160k"]
    # 頭に索引を置き、読み込みながら再生・シークできるようにする
    args += ["-movflags", "+faststart", "-sn", "-dn"]
    return args, "copy" if copy_video else "transcode"


@dataclass
class Conversion:
    """1つの変換の状態。"""

    state: str = "running"  # running / done / error
    mode: str = ""
    progress: float = 0.0  # 0〜1
    error: str = ""
    output: Path | None = None
    thread: threading.Thread | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """画面用の辞書。"""
        return {"state": self.state, "mode": self.mode, "progress": round(self.progress, 3), "error": self.error}


class MediaConverter:
    """動画の変換を裏で行い、できた MP4 を内容ハッシュで保存する。

    保存した MP4 の合計が max_bytes を超えたら、しばらく使っていない（開いていない）ものから消す。
    """

    def __init__(self, cache_dir: Path, max_bytes: int = CACHE_MAX_BYTES) -> None:
        self.cache_dir = cache_dir
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.jobs: dict[str, Conversion] = {}

    @staticmethod
    def _touch(path: Path) -> None:
        """使ったことを、更新日時で記録する（容量の上限で消す順番に使う。atime は記録しない環境が多いため）。"""
        try:
            os.utime(path)
        except OSError:
            pass

    def _prune(self, keep: Path) -> None:
        """保存した MP4 の合計が上限を超えていれば、使っていない順に消す（keep と、作っている途中のものは残す）。"""
        try:
            files = [(p.stat().st_mtime, p.stat().st_size, p) for p in self.cache_dir.glob("*.mp4") if not p.name.endswith(".part.mp4")]
        except OSError:
            return
        total = sum(size for _m, size, _p in files)
        busy = {self.output_path(sha) for sha, job in self.jobs.items() if job.state == "running"}
        for _mtime, size, path in sorted(files):
            if total <= self.max_bytes:
                break
            if path == keep or path in busy:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            total -= size
            log.info("再生用の MP4 を消しました（保存の上限 %.1fGB を超えたため）: %s", self.max_bytes / 1024**3, path.name)

    def output_path(self, sha256: str) -> Path:
        """変換した MP4 の置き場所。"""
        return self.cache_dir / f"{sha256}.mp4"

    def status(self, path: Path, sha256: str, *, retry: bool = False) -> Conversion:
        """変換の状態を返す。まだ作っていなければ、裏で作り始める。

        失敗したものは、retry のときだけ作り直す（画面の問い合わせのたびにやり直さないように）。
        """
        out = self.output_path(sha256)
        with self.lock:
            job = self.jobs.get(sha256)
            if job is not None and job.state == "done" and not out.exists():
                # 保存の上限で消したもの。作り直す
                job = None
            if job is not None and not (retry and job.state == "error"):
                if job.state == "done":
                    self._touch(out)
                return job
            if out.exists():
                self._touch(out)
                job = Conversion(state="done", progress=1.0, output=out)
                self.jobs[sha256] = job
                return job
            job = Conversion()
            self.jobs[sha256] = job
            job.thread = threading.Thread(target=self._run, args=(job, path, out), name=f"media-{sha256[:8]}", daemon=True)
            job.thread.start()
            return job

    def _run(self, job: Conversion, path: Path, out: Path) -> None:
        try:
            info = probe(path)
            args, job.mode = ffmpeg_args(info)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".part.mp4")
            cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(path), *args, "-progress", "pipe:1", str(tmp)]
            log.info("動画を再生用に変換します（%s）: %s", job.mode, path.name)
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert proc.stdout is not None
            for line in proc.stdout:
                m = re.match(r"out_time_us=(\d+)", line)
                if m and info["duration"] > 0:
                    job.progress = min(0.99, int(m.group(1)) / 1e6 / info["duration"])
            err = proc.stderr.read() if proc.stderr else ""
            if proc.wait() != 0 or not tmp.exists():
                tmp.unlink(missing_ok=True)
                raise MediaError(f"ffmpeg が失敗しました: {err.strip()[:300]}")
            tmp.replace(out)
            job.output, job.progress, job.state = out, 1.0, "done"
            log.info("動画を変換しました: %s", path.name)
            with self.lock:
                self._prune(keep=out)
        except (MediaError, OSError, ValueError) as e:
            job.state, job.error = "error", str(e)
            log.warning("動画を変換できませんでした: %s: %s", path.name, e)
        finally:
            job.thread = None
