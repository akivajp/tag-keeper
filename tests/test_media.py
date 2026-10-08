"""ブラウザが直接は再生できない動画（flv・mkv など）を、再生用の MP4 にするテスト。"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tag_keeper.media import MediaConverter, MediaError, ffmpeg_args, probe

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg が無い")


def test_ffmpeg_args_choose_copy_or_transcode() -> None:
    args, mode = ffmpeg_args({"video": "h264", "pix_fmt": "yuv420p", "audio": "aac", "duration": 10})
    assert mode == "copy" and args[args.index("-c:v") + 1] == "copy" and args[args.index("-c:a") + 1] == "copy"
    # 古い形式の映像・ブラウザが再生できない音声は作り直す
    args, mode = ffmpeg_args({"video": "flv1", "pix_fmt": "yuv420p", "audio": "nellymoser", "duration": 10})
    assert mode == "transcode" and "libx264" in args and args[args.index("-c:a") + 1] == "aac"
    # 10bit の H.264 はブラウザで再生できないことがあるので作り直す
    assert ffmpeg_args({"video": "h264", "pix_fmt": "yuv420p10le", "audio": None, "duration": 1})[1] == "transcode"
    with pytest.raises(MediaError):
        ffmpeg_args({"video": None, "pix_fmt": None, "audio": "mp3", "duration": 1})


def make_flv(path: Path, vcodec: str) -> Path:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x48:rate=10",
         "-f", "lavfi", "-i", "sine=duration=1", "-c:v", vcodec, "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )
    return path


def wait(conv: MediaConverter, path: Path, sha: str) -> object:
    job = conv.status(path, sha)
    deadline = time.time() + 60
    while job.state == "running" and time.time() < deadline:
        time.sleep(0.05)
    return job


@needs_ffmpeg
@pytest.mark.parametrize(("vcodec", "mode"), [("libx264", "copy"), ("flv1", "transcode")])
def test_convert_flv(tmp_path: Path, vcodec: str, mode: str) -> None:
    src = make_flv(tmp_path / "a.flv", vcodec)
    assert probe(src)["video"] == ("h264" if vcodec == "libx264" else "flv1")
    conv = MediaConverter(tmp_path / "cache")
    job = wait(conv, src, "sha1")
    assert (job.state, job.mode) == ("done", mode)
    assert probe(conv.output_path("sha1"))["video"] == "h264"
    # 2回目は保存したものを使う
    assert MediaConverter(tmp_path / "cache").status(src, "sha1").state == "done"


@needs_ffmpeg
def test_broken_file_reports_error_and_can_retry(tmp_path: Path) -> None:
    src = tmp_path / "broken.flv"
    src.write_bytes(b"not a video")
    conv = MediaConverter(tmp_path / "cache")
    job = wait(conv, src, "sha2")
    assert job.state == "error" and job.error
    # 失敗は、問い合わせのたびにはやり直さない
    assert conv.status(src, "sha2") is job
    assert conv.status(src, "sha2", retry=True) is not job
