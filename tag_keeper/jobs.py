"""時間のかかる処理（走査・ハッシュ・実行・取り消しなど）を裏で動かし、進み具合を保持する。

Web の画面は、ここに保持した進み具合を1秒ごとに読み出して表示する。

- 同時に動かす処理は1つだけ（カタログとツリーへの書き込みが重ならないように）。
- 進み具合は段階（phase）ごとに持つ。段階内の作業量・済んだ量・速さ・残り時間を画面に出せる。
- 処理中のログは、画面用に直近の行を保持し、あわせてジョブごとのログファイルにも書く。
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from tag_keeper.plan import Reporter

log = logging.getLogger(__name__)

# 画面に保持するログの行数と、覚えておく終わったジョブの数
LOG_LINES = 300
HISTORY = 20


class JobBusyError(RuntimeError):
    """別のジョブが動いているときの例外。"""


@dataclass
class Phase:
    """ジョブの1段階。"""

    name: str
    total: int | None
    unit: str
    done: int = 0
    started: float = field(default_factory=time.time)
    finished: float | None = None

    def to_dict(self, now: float) -> dict[str, Any]:
        """画面用の辞書。速さと残り時間はこの段階の平均から求める。"""
        end = self.finished or now
        elapsed = max(end - self.started, 1e-6)
        rate = self.done / elapsed if self.done else 0.0
        eta = None
        if self.total and rate > 0 and self.finished is None:
            eta = max(self.total - self.done, 0) / rate
        return {
            "name": self.name,
            "total": self.total,
            "unit": self.unit,
            "done": self.done,
            "elapsed": end - self.started,
            "rate": rate,
            "eta": eta,
            "finished": self.finished is not None,
        }


@dataclass
class Job:
    """裏で動く1つの処理。"""

    id: str
    kind: str
    title: str
    target: str = ""  # 対象（ルート名やプラン ID）。画面で結果を対応付けるのに使う
    status: str = "running"  # running / done / failed
    phases: list[Phase] = field(default_factory=list)
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_LINES))
    started: float = field(default_factory=time.time)
    finished: float | None = None
    result: Any = None
    error: str | None = None
    log_file: Path | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def to_dict(self, *, lines: int = 50) -> dict[str, Any]:
        """画面用の辞書（ログは直近の lines 行だけ）。"""
        now = time.time()
        with self.lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "title": self.title,
                "target": self.target,
                "status": self.status,
                "phases": [p.to_dict(now) for p in self.phases],
                "lines": list(self.lines)[-lines:],
                "started": datetime.fromtimestamp(self.started).isoformat(timespec="seconds"),
                "elapsed": (self.finished or now) - self.started,
                "result": self.result,
                "error": self.error,
                "log_file": str(self.log_file) if self.log_file else None,
            }


class JobReporter(Reporter):
    """Reporter の通知をジョブの進み具合に書き込む。"""

    def __init__(self, job: Job) -> None:
        self.job = job

    def phase(self, name: str, total: int | None = None, unit: str = "件") -> None:
        """前の段階を閉じて、新しい段階を始める。"""
        now = time.time()
        with self.job.lock:
            if self.job.phases and self.job.phases[-1].finished is None:
                self.job.phases[-1].finished = now
            self.job.phases.append(Phase(name, total, unit, started=now))
        log.info("段階: %s", name)

    def set_total(self, total: int | None) -> None:
        """今の段階の作業量を後から決める（ハッシュ計算など、始めてから分かる場合）。"""
        with self.job.lock:
            if self.job.phases:
                self.job.phases[-1].total = total

    def advance(self, n: int = 1) -> None:
        """今の段階を n だけ進める。"""
        with self.job.lock:
            if self.job.phases:
                self.job.phases[-1].done += n

    def set_done(self, done: int) -> None:
        """今の段階の済んだ量を直接決める（走査の件数など）。"""
        with self.job.lock:
            if self.job.phases:
                self.job.phases[-1].done = done

    def note(self, line: str) -> None:
        """画面のログに1行足す（ログファイルにも書く）。"""
        log.info("%s", line)


class _JobLogHandler(logging.Handler):
    """ジョブを動かしているスレッドのログだけを、そのジョブの画面用ログに入れる。"""

    def __init__(self, job: Job, thread_id: int) -> None:
        super().__init__(level=logging.INFO)
        self.job = job
        self.thread_id = thread_id
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self.thread_id:
            return
        line = self.format(record)
        with self.job.lock:
            self.job.lines.append(line)


class JobManager:
    """ジョブを1つずつ裏で動かし、進み具合と直近の履歴を保持する。"""

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.lock = threading.Lock()
        self.current: Job | None = None
        self.history: deque[Job] = deque(maxlen=HISTORY)
        self._seq = 0

    def busy(self) -> bool:
        """ジョブが動いているか。"""
        with self.lock:
            return self.current is not None and self.current.status == "running"

    def get(self, job_id: str) -> Job | None:
        """ID でジョブを探す（動いているもの、または直近の履歴から）。"""
        with self.lock:
            if self.current is not None and self.current.id == job_id:
                return self.current
            return next((j for j in self.history if j.id == job_id), None)

    def latest(self) -> Job | None:
        """動いているジョブ、無ければ最後に終わったジョブ。"""
        with self.lock:
            return self.current

    def start(
        self,
        kind: str,
        title: str,
        fn: Callable[[JobReporter], Any],
        *,
        target: str = "",
    ) -> Job:
        """ジョブを裏で始める。別のジョブが動いていれば JobBusyError。

        Args:
            kind: ジョブの種類（scan / hash / plan / check / apply / undo / sync など）。
            title: 画面に出す題名。
            fn: 実際の処理。JobReporter を受け取り、画面に返す結果（JSON にできる値）を返す。
            target: 対象（ルート名やプラン ID）。
        """
        with self.lock:
            if self.current is not None and self.current.status == "running":
                raise JobBusyError(f"別の処理が動いています: {self.current.title}")
            self._seq += 1
            job_id = f"{datetime.now():%Y%m%d-%H%M%S}-{self._seq}"
            job = Job(job_id, kind, title, target=target)
            job.log_file = self.log_dir / f"{job_id}-{kind}.log"
            if self.current is not None:
                self.history.appendleft(self.current)
            self.current = job
        thread = threading.Thread(target=self._run, args=(job, fn), name=f"job-{job_id}", daemon=True)
        thread.start()
        return job

    def _run(self, job: Job, fn: Callable[[JobReporter], Any]) -> None:
        """ジョブの本体。ログをジョブに結び付け、例外は失敗として記録する。"""
        root_logger = logging.getLogger("tag_keeper")
        handler = _JobLogHandler(job, threading.get_ident())
        root_logger.addHandler(handler)
        file_handler: logging.Handler | None = None
        if job.log_file is not None:
            job.log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(job.log_file, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            file_handler.addFilter(lambda r: r.thread == handler.thread_id)
            root_logger.addHandler(file_handler)
        if root_logger.level == logging.NOTSET or root_logger.level > logging.INFO:
            root_logger.setLevel(logging.INFO)
        log.info("開始: %s", job.title)
        try:
            result = fn(JobReporter(job))
            with job.lock:
                job.result = result
                job.status = "done"
            log.info("完了: %s", job.title)
        except Exception as e:  # noqa: BLE001 - 画面に失敗として見せるため、すべて受け止める
            log.error("失敗: %s: %s", job.title, e)
            log.debug("%s", traceback.format_exc())
            with job.lock:
                job.error = str(e)
                job.status = "failed"
        finally:
            now = time.time()
            with job.lock:
                job.finished = now
                if job.phases and job.phases[-1].finished is None:
                    job.phases[-1].finished = now
            root_logger.removeHandler(handler)
            if file_handler is not None:
                root_logger.removeHandler(file_handler)
                file_handler.close()

    def wait(self, timeout: float = 30.0) -> Job | None:
        """動いているジョブが終わるまで待つ（テスト用）。"""
        deadline = time.time() + timeout
        while self.busy() and time.time() < deadline:
            time.sleep(0.02)
        return self.latest()
