"""音声・動画の書き起こし（別のプロセス・GPU から CPU へのやり直し・索引への保存）のテスト。

faster-whisper は使わず、子のプロセスを小さな偽物のスクリプトに差し替える。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from conftest import write
from tag_keeper import fulltext, transcribe
from tag_keeper.config import SafetyConfig
from tag_keeper.hashing import hash_root
from tag_keeper.scan import scan_root
from test_web import HOST, env  # noqa: F401 - env は fixture

SAFETY = SafetyConfig(mass_missing_ratio=0.2, mass_missing_min=3)

FAKE_CHILD = r"""
import json, sys
job, out = json.load(open(sys.argv[1], encoding="utf-8")), sys.argv[2]
with open(out, "a", encoding="utf-8") as f:
    for i, p in enumerate(job["paths"]):
        if job["device"] == "cuda" and "crash" in p:
            sys.exit(134)  # cuDNN を読めずに落ちた（例外ではなくプロセスごと）
        if "long" in p:
            f.write(json.dumps({"path": p, "language": "ja", "duration": 99999, "skipped": "長すぎる"}) + "\n")
        else:
            f.write(json.dumps({"path": p, "language": "ja", "duration": 61.0, "text": "[0:00] 会議を始めます\n[0:05] 議題は予算です"}) + "\n")
"""


@pytest.fixture
def fake_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    script = write(tmp_path / "fake_child.py", FAKE_CHILD)
    monkeypatch.setattr(transcribe, "child_command", lambda: [sys.executable, str(script)])
    # device = "auto" のときに GPU を調べに行かないよう、指定のとおりにする
    monkeypatch.setattr(transcribe, "resolve_device", lambda opts: "cuda" if opts.device == "cuda" else "cpu")
    return []


def test_media_is_transcribed_into_the_index(conn: sqlite3.Connection, root_id: int, root: Path, fake_child: list[str]) -> None:
    write(root / "meeting.m4a", b"audio1")
    write(root / "talk-long.mp3", b"audio2")
    write(root / "doc.txt", "not media")
    scan_root(conn, root_id, root, SAFETY)
    hash_root(conn, root_id, root)
    assert sorted(t.relpath for t in transcribe.pending(conn, root_id)) == ["meeting.m4a", "talk-long.mp3"]
    results = transcribe.run_pending(conn, root_id, root, transcribe.Options(device="cpu"))
    assert {Path(r.path).name: (r.skipped, r.language) for r in results} == {"meeting.m4a": ("", "ja"), "talk-long.mp3": ("長すぎる", "ja")}
    assert [r["relpath"] for r in fulltext.find_entries(conn, root_id, text="予算")] == ["meeting.m4a"]
    note = conn.execute("SELECT note FROM extracts WHERE method = 'transcript'").fetchone()[0]
    assert note == "言語: ja・長さ: 1:01"
    assert conn.execute("SELECT method FROM extracts x JOIN entries e ON e.sha256 = x.sha256 WHERE e.relpath = 'talk-long.mp3'").fetchone()[0] == "none"
    assert transcribe.pending(conn, root_id) == []  # 飛ばしたものも二度と試さない


def test_a_crashed_gpu_process_is_retried_on_cpu(tmp_path: Path, fake_child: list[str]) -> None:
    a, b = write(tmp_path / "a.mp3", b"1"), write(tmp_path / "crash.mp3", b"2")
    results = transcribe.run_batch([a, b], transcribe.Options(device="cuda"))
    assert [Path(r.path).name for r in results] == ["a.mp3", "crash.mp3"] and not any(r.error for r in results)


def test_unavailable_without_faster_whisper(env: dict, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(transcribe, "available", lambda: False)
    env["app"].post_json("/api/transcribe/data", {}, headers={"Origin": f"http://{HOST}"}, status=409)



def _child_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """子のプロセスを、渡した中身の偽物に差し替える（CPU で動かす扱いにする）。"""
    script = write(tmp_path / "child.py", "import json, sys, time\njob, out = json.load(open(sys.argv[1], encoding='utf-8')), sys.argv[2]\n" + body)
    monkeypatch.setattr(transcribe, "child_command", lambda: [sys.executable, str(script)])
    monkeypatch.setattr(transcribe, "resolve_device", lambda opts: "cpu")


def test_a_process_that_cannot_start_fails_no_file(conn: sqlite3.Connection, root_id: int, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """モデルを読み込めずに終わった（ダウンロードの失敗など）ときは、どのファイルも失敗として保存しない。直れば書き起こせる。"""
    write(root / "a.mp3", b"1")
    write(root / "b.mp3", b"2")
    scan_root(conn, root_id, root, SAFETY)
    hash_root(conn, root_id, root)
    _child_script(tmp_path, monkeypatch, "sys.stderr.write('OSError: cannot download model\\n'); sys.exit(1)\n")
    assert transcribe.run_pending(conn, root_id, root, transcribe.Options(device="cpu")) == []
    assert conn.execute("SELECT COUNT(*) FROM extracts").fetchone()[0] == 0
    assert len(transcribe.pending(conn, root_id)) == 2


def test_a_crash_fails_only_the_file_being_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """途中でプロセスごと落ちたら、そのとき扱っていた1件だけを失敗にする。残りは結果を返さず、次の機会に試す。"""
    paths = [write(tmp_path / n, b"x") for n in ("a.mp3", "bad.mp3", "c.mp3")]
    _child_script(tmp_path, monkeypatch, """
with open(out, "a", encoding="utf-8") as f:
    f.write(json.dumps({"ready": True}) + "\\n"); f.flush()
    for p in job["paths"]:
        if "bad" in p:
            sys.exit(139)
        f.write(json.dumps({"path": p, "language": "ja", "duration": 1.0, "text": "[0:00] はい"}) + "\\n"); f.flush()
""")
    results = transcribe.run_batch(paths, transcribe.Options(device="cpu"))
    assert [(Path(r.path).name, bool(r.error)) for r in results] == [("a.mp3", False), ("bad.mp3", True)]


def test_noisy_or_stuck_processes_do_not_hang(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """子が stderr に多く書いても固まらない。時間切れになったら子を止める。"""
    import time

    a = write(tmp_path / "a.mp3", b"1")
    # パイプの容量（64KB）を超える量を stderr に書いてから、結果を書く
    _child_script(tmp_path, monkeypatch, """
sys.stderr.write("warning: noisy\\n" * 20000); sys.stderr.flush()
with open(out, "a", encoding="utf-8") as f:
    f.write(json.dumps({"path": job["paths"][0], "language": "ja", "duration": 1.0, "text": "[0:00] はい"}) + "\\n")
""")
    began = time.monotonic()
    results = transcribe.run_batch([a], transcribe.Options(device="cpu"), timeout=20)
    assert [r.error for r in results] == [""] and time.monotonic() - began < 15
    # 終わらない子は、時間切れで止める
    _child_script(tmp_path, monkeypatch, """
with open(out, "a", encoding="utf-8") as f:
    f.write(json.dumps({"ready": True}) + "\\n"); f.flush()
time.sleep(600)
""")
    began = time.monotonic()
    results = transcribe.run_batch([a], transcribe.Options(device="cpu"), timeout=1)
    assert time.monotonic() - began < 15 and "時間切れ" in results[0].error


def test_length_is_read_without_decoding(tmp_path: Path) -> None:
    """長すぎる録音を飛ばす判定のために、長さだけを先に読む（ffmpeg と PyAV があるときだけ確かめる）。"""
    import shutil
    import subprocess

    pytest.importorskip("av")
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg が無い")
    wav = tmp_path / "tone.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", str(wav)], check=True)
    assert transcribe.media_duration(str(wav)) == pytest.approx(3.0, abs=0.2)
    assert transcribe.media_duration(str(write(tmp_path / "broken.mp3", b"not audio"))) is None
