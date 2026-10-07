"""同期クライアントとの連携（利用者が意図した大量の削除で、同期を止めないための仕組み）。

Linux 版 onedrive クライアント（abraunegg/onedrive）は、子を一定数（classify_as_big_delete、既定 1000）以上
持つパスがローカルで消えると「大量削除」とみなし、クラウドのデータを守るために終了する。
常駐（--monitor）を systemd で動かしていると、再起動のたびに同じ理由で終了し、同期が止まったままになる。

tag-keeper が利用者の承認のもとで大量に隔離するときは、次の順で行う。

1. 常駐の同期を止める（systemctl --user stop）
2. 隔離する（plan.apply_plan）
3. 1回限りの同期（onedrive --sync）を、しきい値を「この実行で消す件数より少し上」に上げて実行し、
   削除をクラウドに反映する。--force は使わない（意図しない大量削除まで通してしまうため）
4. 常駐の同期を再開する

3 が失敗したら再開しない（再開しても同じ理由で終了を繰り返すため）。
状態は実行記録に残し、画面やコマンドから再試行できるようにする（plan.retry_sync）。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

# onedrive クライアントの出力（v2.5 で確認）
_DELETED_ONLINE = re.compile(r"Deleting item from Microsoft OneDrive: (?:\./)?(.+?)\s*$")
_BIG_DELETE = "An attempt to remove a large volume of data"
_CONFIG_LINE = re.compile(r"^Config option '([^']+)'\s*=\s*(.*?)\s*$")


class SyncError(RuntimeError):
    """同期クライアントの停止・反映・再開に失敗したときの例外。"""


class SyncGuard(Protocol):
    """同期クライアントとの連携の窓口。"""

    name: str

    def needs_pause(self, children: Sequence[int]) -> bool:
        """消すパスごとの子の数から、同期を止めて連携する必要があるかを返す。"""
        ...

    def pause(self) -> None:
        """常駐の同期を止める。"""
        ...

    def flush(
        self,
        max_children: int,
        on_deleted: Callable[[str], None] | None = None,
        on_line: Callable[[str], None] | None = None,
    ) -> None:
        """ローカルでの削除をクラウドに反映する（1回限りの同期）。

        Args:
            max_children: この実行で消したパスの子の数の最大値（しきい値の決定に使う）。
            on_deleted: クラウドで1件消えるたびに、同期フォルダからの相対パスを渡して呼ぶ。
            on_line: 同期クライアントの出力を1行ずつ渡して呼ぶ。
        """
        ...

    def resume(self) -> None:
        """常駐の同期を再開する。"""
        ...

    def relpath(self, path: Path) -> str:
        """パスを、同期クライアントが表示する形（同期フォルダからの相対パス）にする。"""
        ...

    def status(self) -> dict[str, str | int | bool]:
        """常駐の同期の状態（画面の表示用）。"""
        ...


def count_children(path: Path) -> int:
    """フォルダの配下にあるもの（ファイルとフォルダ）の数を返す。ファイルや存在しないものは 0。"""
    if not path.is_dir() or path.is_symlink():
        return 0
    n = 0
    for _dirpath, dirnames, filenames in os.walk(path):
        n += len(dirnames) + len(filenames)
    return n


@dataclass
class OneDriveGuard:
    """Linux 版 onedrive クライアントとの連携。

    Attributes:
        service: 常駐の同期の systemd --user ユニット名。
        mode: "auto"（大量削除とみなされるときだけ連携）か "always"（毎回連携）。
        command: onedrive の実行ファイル。
        systemctl: systemctl の実行ファイル。
    """

    service: str = "onedrive.service"
    mode: str = "auto"
    command: str = "onedrive"
    systemctl: str = "systemctl"
    name: str = "onedrive"
    _config: dict[str, str] | None = field(default=None, init=False, repr=False)

    def _run(self, cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        """コマンドを実行する。失敗したら SyncError。"""
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise SyncError(f"実行できません: {' '.join(cmd)} ({e})") from e
        if check and res.returncode != 0:
            raise SyncError(f"失敗しました: {' '.join(cmd)}: {(res.stderr or res.stdout).strip()}")
        return res

    def config(self) -> dict[str, str]:
        """onedrive の実効設定（--display-config の出力）を読む。"""
        if self._config is None:
            res = self._run([self.command, "--display-config"])
            cfg: dict[str, str] = {}
            for line in res.stdout.splitlines():
                m = _CONFIG_LINE.match(line.strip())
                if m:
                    cfg[m.group(1)] = m.group(2)
            self._config = cfg
        return self._config

    def threshold(self) -> int:
        """大量削除とみなす子の数（classify_as_big_delete）。"""
        try:
            return int(self.config().get("classify_as_big_delete", "1000"))
        except ValueError:
            return 1000

    def sync_dir(self) -> Path:
        """同期フォルダ（シンボリックリンクは解決する）。"""
        raw = self.config().get("sync_dir", "~/OneDrive")
        return Path(os.path.expanduser(raw)).resolve()

    def relpath(self, path: Path) -> str:
        """同期フォルダからの相対パス。消えた後でも求められるよう、親だけを解決する。"""
        resolved = path.parent.resolve() / path.name
        try:
            return resolved.relative_to(self.sync_dir()).as_posix()
        except ValueError:
            return path.as_posix()

    def needs_pause(self, children: Sequence[int]) -> bool:
        """大量削除とみなされるパスがあるか（mode が "always" なら常に True）。"""
        if self.mode == "always":
            return True
        return max(children, default=0) >= self.threshold()

    def is_active(self) -> str:
        """常駐の同期の状態（systemctl is-active の出力: active / inactive / failed など）。"""
        res = self._run([self.systemctl, "--user", "is-active", self.service], check=False)
        return res.stdout.strip() or "unknown"

    def pause(self) -> None:
        """常駐の同期を止める。止まったことを確かめられなければ SyncError。"""
        log.info("常駐の同期を止めます: %s", self.service)
        self._run([self.systemctl, "--user", "stop", self.service])
        state = self.is_active()
        if state == "active":
            raise SyncError(f"常駐の同期が止まりません: {self.service}")

    def flush(
        self,
        max_children: int,
        on_deleted: Callable[[str], None] | None = None,
        on_line: Callable[[str], None] | None = None,
    ) -> None:
        """1回限りの同期で、ローカルでの削除をクラウドに反映する。

        しきい値は、この実行で消したパスの子の数に余裕を足した値まで上げる（--force は使わない）。
        """
        limit = max(self.threshold(), max_children + max(100, max_children // 10) + 1)
        cmd = [self.command, "--sync", "--classify-as-big-delete", str(limit)]
        log.info("削除をクラウドに反映します: %s", " ".join(cmd))
        tail: list[str] = []
        big_delete = False
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
            )
        except OSError as e:
            raise SyncError(f"onedrive を実行できません: {e}") from e
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            tail = (tail + [line])[-20:]
            if on_line is not None:
                on_line(line)
            if _BIG_DELETE in line:
                big_delete = True
            m = _DELETED_ONLINE.search(line)
            if m and on_deleted is not None:
                on_deleted(m.group(1))
        code = proc.wait()
        if big_delete:
            raise SyncError(
                "onedrive が大量削除として止めました（しきい値 "
                f"{limit} を超える削除が他にもある可能性があります）: " + " / ".join(tail[-3:])
            )
        if code != 0:
            raise SyncError(f"onedrive --sync が失敗しました（終了コード {code}）: " + " / ".join(tail[-3:]))

    def resume(self) -> None:
        """常駐の同期を再開する。"""
        log.info("常駐の同期を再開します: %s", self.service)
        self._run([self.systemctl, "--user", "start", self.service])

    def status(self) -> dict[str, str | int | bool]:
        """常駐の同期の状態（画面の表示用）。"""
        state = self.is_active()
        out: dict[str, str | int | bool] = {"client": self.name, "service": self.service, "state": state}
        try:
            out["threshold"] = self.threshold()
            out["sync_dir"] = str(self.sync_dir())
        except SyncError as e:
            out["error"] = str(e)
        return out


def make_guard(sync_client: str, service: str, mode: str) -> SyncGuard | None:
    """ルートの設定から連携の窓口を作る。連携しないなら None。"""
    if sync_client == "onedrive":
        return OneDriveGuard(service=service, mode=mode)
    return None
