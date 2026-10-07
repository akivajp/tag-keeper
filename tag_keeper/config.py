"""設定ファイル（TOML）の読み込みと、既定のパス。

設定ファイルの例::

    [[roots]]
    name = "onedrive"
    path = "~/CloudSync/OneDrive"
    exclude = ["*.partial"]          # 走査しないパス（ルートからの相対パスに対する glob）
    sync_client = "onedrive"         # 同期クライアント（大量に隔離するとき、同期を止めずに削除を反映する）
    sync_service = "onedrive.service"  # 常駐の同期の systemd --user ユニット
    sync_guard = "auto"              # auto: 大量削除になるときだけ連携する / always: 実行のたびに連携する

    [hygiene]
    allow = ["archive/**"]           # 報告しないパス（「残す」と判断したもの）
    device_names = ["zefat", "hermon"]  # 競合コピーの名前に付く端末名
    app_data_names = ["MyGameLauncher"] # 既定に加えて、アプリのデータとみなすフォルダ名

    [safety]
    mass_missing_ratio = 0.2         # 一度に消えたとみなす割合がこれを超えたら確定を保留する
    mass_missing_min = 100

    [plan]
    quarantine_dir = "~/.local/share/tag-keeper/quarantine"  # 隔離先（ルートと同じファイルシステム）
    snapper_config = "home"          # 実行の前後にスナップショットを撮る snapper の設定名（"" で撮らない）

    [tags]
    dir = "~/.local/share/tag-keeper/tags"  # タグのログ（端末ごとの JSONL）の置き場所

    [organize]
    inbox_patterns = ["tmp", "temp", "*未整理*", "*inbox*"]  # 受け皿とみなすフォルダ名（大文字・小文字を区別しない glob）
    ollama_url = "http://127.0.0.1:11434"
    model = "gemma3:12b"             # 名前と移動先の提案に使うモデル（画像を読めるもの）
    use_images = true                # 画像と、文字の無いスキャン PDF をモデルに見せる
    max_chars = 4000                 # モデルに渡す本文の上限（文字数）
    candidates = 15                  # モデルに選ばせる移動先の候補の数
"""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def default_config_path() -> Path:
    """既定の設定ファイルのパス（XDG_CONFIG_HOME に従う）を返す。"""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "tag-keeper" / "config.toml"


def default_data_dir() -> Path:
    """既定のデータフォルダ（XDG_DATA_HOME に従う）を返す。

    カタログ・整理プラン・実行記録・隔離フォルダは、管理対象のツリーの外のここに置く。
    クラウド同期の対象にしないため。
    """
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "tag-keeper"


def default_db_path() -> Path:
    """既定のカタログ（SQLite）のパスを返す。"""
    return default_data_dir() / "catalog.db"


@dataclass
class RootConfig:
    """管理対象のルート（トップフォルダ）の設定。"""

    name: str
    path: Path
    # 走査しないパス。ルートからの相対パス（区切りは /）に対する glob
    exclude: list[str] = field(default_factory=list)
    # ルートを同期している同期クライアント。"onedrive"（Linux 版 onedrive クライアント）か ""（連携しない）
    sync_client: str = ""
    # 常駐の同期を動かしている systemd --user のユニット名
    sync_service: str = "onedrive.service"
    # 同期クライアントと連携する条件。"auto": 大量削除とみなされるときだけ / "always": 実行のたびに
    sync_guard: str = "auto"


@dataclass
class HygieneConfig:
    """整理候補の検出に関する設定。"""

    # 報告しないパス（「残す」と判断したもの）。ルートからの相対パスに対する glob
    allow: list[str] = field(default_factory=list)
    # 競合コピーの名前に付く端末名（例: "report-zefat.xlsx"）。既定はこのマシンのホスト名
    device_names: list[str] = field(default_factory=lambda: [socket.gethostname()])
    # 既定に加えて、アプリのデータとみなすフォルダ名
    app_data_names: list[str] = field(default_factory=list)
    # この数以上のファイルを抱え、拡張子の無いファイルが過半数のフォルダをアプリのデータとみなす
    app_data_min_files: int = 500


@dataclass
class SafetyConfig:
    """大量消失の安全弁の設定（要件 F-SC-7）。"""

    # 一度の走査で消えたファイルの割合がこれを超えたら、削除として確定せずに保留する
    mass_missing_ratio: float = 0.2
    # ただし、消えた件数がこの数以下なら保留しない（小さいルートで誤って止まらないように）
    mass_missing_min: int = 100


@dataclass
class PlanConfig:
    """整理プランの実行に関する設定（要件 F-PL・F-VS-3）。"""

    # 隔離先のフォルダ。プランごとに <quarantine_dir>/<プラン ID>/ の下へ、元の相対パスのまま移す。
    # 移動は改名で行うので、ルートと同じファイルシステム（btrfs なら同じサブボリューム）に置く
    quarantine_dir: Path = field(default_factory=lambda: default_data_dir() / "quarantine")
    # 実行の前後にスナップショットを撮る snapper の設定名。空なら撮らない
    snapper_config: str = ""


@dataclass
class TagsConfig:
    """タグの設定。"""

    # タグのログ（端末ごとの追記専用 JSONL）の置き場所。ツリーの外に置く（P6）
    dir: Path = field(default_factory=lambda: default_data_dir() / "tags")


@dataclass
class OrganizeConfig:
    """受け皿のフォルダの整理の提案（名前と移動先）の設定。"""

    # 受け皿とみなすフォルダ名。大文字・小文字を区別しない glob で、フォルダ名（パスの最後）に照合する
    inbox_patterns: list[str] = field(default_factory=lambda: ["tmp", "temp", "*未整理*", "*inbox*"])
    # ollama の API の URL（外部のサービスには送らない。手元のモデルだけを使う）
    ollama_url: str = "http://127.0.0.1:11434"
    # 名前と移動先の提案に使うモデル。画像を読めるものにする
    model: str = "gemma3:12b"
    # 画像と、文字を取り出せないスキャン PDF を、画像としてモデルに見せるか
    use_images: bool = True
    # モデルに渡す本文の上限（文字数）
    max_chars: int = 4000
    # 似ているフォルダから絞り込み、モデルに選ばせる移動先の候補の数
    candidates: int = 15
    # 生成の温度（低いほど毎回同じ提案になる）
    temperature: float = 0.2
    # 1件の問い合わせの待ち時間の上限（秒）
    timeout: float = 300.0


@dataclass
class Config:
    """tag-keeper 全体の設定。"""

    roots: list[RootConfig] = field(default_factory=list)
    hygiene: HygieneConfig = field(default_factory=HygieneConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    plan: PlanConfig = field(default_factory=PlanConfig)
    tags: TagsConfig = field(default_factory=TagsConfig)
    organize: OrganizeConfig = field(default_factory=OrganizeConfig)

    def find_root(self, name: str) -> RootConfig | None:
        """名前でルートを探す。見つからなければ None を返す。"""
        return next((r for r in self.roots if r.name == name), None)


class ConfigError(ValueError):
    """設定ファイルの内容が不正なときの例外。"""


def _expand(path: str) -> Path:
    """`~` と環境変数を展開した絶対パスを返す。"""
    return Path(os.path.expandvars(os.path.expanduser(path))).resolve()


def load_config(path: Path | None = None) -> Config:
    """設定ファイルを読み込む。ファイルが無ければ既定値の設定を返す。

    Args:
        path: 設定ファイルのパス。None なら既定のパスを使う。
    """
    path = path or default_config_path()
    if not path.exists():
        return Config()
    with path.open("rb") as f:
        data = tomllib.load(f)

    roots: list[RootConfig] = []
    for i, item in enumerate(data.get("roots", [])):
        if "name" not in item or "path" not in item:
            raise ConfigError(f"{path}: roots[{i}] には name と path が必要です")
        sync_client = str(item.get("sync_client", ""))
        if sync_client not in ("", "onedrive"):
            raise ConfigError(f"{path}: roots[{i}] の sync_client は \"onedrive\" か空にしてください: {sync_client}")
        sync_guard = str(item.get("sync_guard", "auto"))
        if sync_guard not in ("auto", "always"):
            raise ConfigError(f"{path}: roots[{i}] の sync_guard は \"auto\" か \"always\" にしてください: {sync_guard}")
        roots.append(
            RootConfig(
                name=str(item["name"]),
                path=_expand(str(item["path"])),
                exclude=[str(p) for p in item.get("exclude", [])],
                sync_client=sync_client,
                sync_service=str(item.get("sync_service", "onedrive.service")),
                sync_guard=sync_guard,
            )
        )
    names = [r.name for r in roots]
    if len(names) != len(set(names)):
        raise ConfigError(f"{path}: roots の name が重複しています")

    hy = data.get("hygiene", {})
    hygiene = HygieneConfig()
    if "allow" in hy:
        hygiene.allow = [str(p) for p in hy["allow"]]
    if "device_names" in hy:
        hygiene.device_names = [str(n) for n in hy["device_names"]]
    if "app_data_names" in hy:
        hygiene.app_data_names = [str(n) for n in hy["app_data_names"]]
    if "app_data_min_files" in hy:
        hygiene.app_data_min_files = int(hy["app_data_min_files"])

    sf = data.get("safety", {})
    safety = SafetyConfig()
    if "mass_missing_ratio" in sf:
        safety.mass_missing_ratio = float(sf["mass_missing_ratio"])
    if "mass_missing_min" in sf:
        safety.mass_missing_min = int(sf["mass_missing_min"])

    pl = data.get("plan", {})
    plan = PlanConfig()
    if "quarantine_dir" in pl:
        plan.quarantine_dir = _expand(str(pl["quarantine_dir"]))
    if "snapper_config" in pl:
        plan.snapper_config = str(pl["snapper_config"])

    tg = data.get("tags", {})
    tags = TagsConfig()
    if "dir" in tg:
        tags.dir = _expand(str(tg["dir"]))

    og = data.get("organize", {})
    organize = OrganizeConfig()
    if "inbox_patterns" in og:
        organize.inbox_patterns = [str(x) for x in og["inbox_patterns"]]
    for key, conv in (
        ("ollama_url", str),
        ("model", str),
        ("use_images", bool),
        ("max_chars", int),
        ("candidates", int),
        ("temperature", float),
        ("timeout", float),
    ):
        if key in og:
            setattr(organize, key, conv(og[key]))

    return Config(roots=roots, hygiene=hygiene, safety=safety, plan=plan, tags=tags, organize=organize)
