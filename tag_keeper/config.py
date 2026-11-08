"""設定ファイル（TOML）の読み込みと、既定のパス。

設定ファイルの例::

    [[roots]]
    name = "onedrive"
    path = "~/CloudSync/OneDrive"
    exclude = ["*.partial"]          # 走査しないパス（ルートからの相対パスに対する glob）
    sync_client = "onedrive"         # 同期クライアント（大量に隔離するとき、同期を止めずに削除を反映する）。"cloud-sync"（rclone bisync の定期実行）も可
    sync_service = "onedrive.service"  # 常駐の同期の systemd --user ユニット（cloud-sync なら "cloud-sync-onedrive.service" か項目名）
    sync_guard = "auto"              # auto: 大量削除になるときだけ連携する / always: 実行のたびに連携する
    replicas = { "OneDrive" = "onedrive" }  # 中のフォルダが別のルートの複製なら登録する（走査・重複検出・プランから外す）
    allow_cloud = true               # false: 内容を外部（クラウドのモデル）へ送らず、手元のモデル（[organize] local_model）を使う

    [hygiene]
    allow = ["archive/**"]           # 報告しないパス（「残す」と判断したもの）
    device_names = ["zefat", "hermon"]  # 競合コピーの名前に付く端末名
    app_data_names = ["MyGameLauncher"] # 既定に加えて、アプリのデータとみなすフォルダ名
    disabled = ["stale-large"]       # 使わない検出ルール（カテゴリの短い名前）
    stale_min_mb = 500               # 「大きくて長く触れていないもの」の大きさ（MB）
    stale_days = 365                 # …と、触れていない日数
    fanout_max = 200                 # 構造の見直しで「直下のファイルが多すぎる」とする数

    [safety]
    mass_missing_ratio = 0.2         # 一度に消えたとみなす割合がこれを超えたら確定を保留する
    mass_missing_min = 100

    [scan]
    auto_refresh = 60                # Web の画面を動かしている間、この間隔（分）で各ルートを最新にする。0 で止める
    after_sync = true                # cloud-sync の同期が終わるたびに、そのルートを最新にする
    relink_days = 30                 # inode で結び付かない移動を内容ハッシュで探すとき、何日前までに消えたものを相手にするか。0 で探さない

    [plan]
    quarantine_dir = "~/.local/share/tag-keeper/quarantine"  # 隔離先（ルートと同じファイルシステム）
    snapper_config = "home"          # 実行の前後にスナップショットを撮る snapper の設定名（"" で撮らない）

    [tags]
    dir = "~/.local/share/tag-keeper/tags"  # タグのログ（端末ごとの JSONL）の置き場所

    [extract]
    enabled = true                   # Web の画面を動かしている間、裏で本文を抽出して全文検索の索引を作る
    ocr = true                       # tesseract があれば、文字の無いスキャンの PDF を OCR にかける
    ocr_images = false               # 画像（jpg・png など）も OCR にかける
    ocr_lang = "jpn+eng"             # tesseract の言語（縦書きも読むなら "jpn+jpn_vert+eng"）
    ocr_pages = 3                    # OCR にかける PDF のページ数（先頭から）
    max_chars = 50000                # 1ファイルから保存する本文の上限（文字数）
    max_mb = 200                     # これより大きいファイルは抽出しない（MB）

    [organize]
    inbox_patterns = ["tmp", "temp", "*未整理*", "*inbox*"]  # 受け皿とみなすフォルダ名（大文字・小文字を区別しない glob）
    ollama_url = "http://127.0.0.1:11434"
    model = "gemma3:12b"             # 名前と移動先の提案に使うモデル（":cloud" などのクラウドのモデルも可。内容がクラウドに送られる）
    vision_model = ""                # 画像を読むモデル（空なら model）
    model_choices = ["glm-5.3-flash:cloud", "gemma4:31b-cloud"]  # 画面で選べるモデルの候補
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
from typing import Any


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
    # ルートを同期している同期クライアント。"onedrive"（Linux 版 onedrive クライアント）・"cloud-sync"（rclone bisync の定期実行）か ""（連携しない）
    sync_client: str = ""
    # 常駐の同期を動かしている systemd --user のユニット名
    sync_service: str = "onedrive.service"
    # 同期クライアントと連携する条件。"auto": 大量削除とみなされるときだけ / "always": 実行のたびに
    sync_guard: str = "auto"
    # 複製（バックアップ）として登録した中のフォルダ（ルートからの相対パス → 元のルート名）。走査・重複検出・プランから外す（F-SC-6）
    replicas: dict[str, str] = field(default_factory=dict)
    # このルートのファイルの内容を、外部（ollama のクラウドのモデル）へ送ってよいか（F-EX-4・N-2）。
    # false なら、クラウドのモデルを選んでいても手元のモデル（[organize] local_model）に切り替える。業務のルートは false にする
    allow_cloud: bool = True


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
    # 使わない検出ルール（カテゴリの短い名前。例: ["temp", "stale-large"]）
    disabled: list[str] = field(default_factory=list)
    # 「大きくて長く触れていないもの」の大きさ（MB）と、触れていない日数
    stale_min_mb: int = 500
    stale_days: int = 365
    # 構造の見直しで「直下のファイルが多すぎる」とするファイルの数
    fanout_max: int = 200


@dataclass
class SafetyConfig:
    """大量消失の安全弁の設定（要件 F-SC-7）。"""

    # 一度の走査で消えたファイルの割合がこれを超えたら、削除として確定せずに保留する
    mass_missing_ratio: float = 0.2
    # ただし、消えた件数がこの数以下なら保留しない（小さいルートで誤って止まらないように）
    mass_missing_min: int = 100


@dataclass
class ScanConfig:
    """カタログを最新に保つ設定（要件 F-SC-3・F-ID-1、原則 P4）。"""

    # Web の画面（serve）を動かしている間、この間隔（分）で各ルートを最新にする（走査とハッシュ計算）。0 で止める
    auto_refresh: int = 60
    # cloud-sync の同期が終わるたびに、そのルートを最新にする（クラウド側の変更をすぐにカタログへ反映する）
    after_sync: bool = True
    # inode で結び付かない移動（クラウド側での移動が「削除と新規」として届いたものなど）を、
    # 内容ハッシュで探すときに相手にする「消えたもの」の古さの上限（日）。0 で探さない
    relink_days: int = 30


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
class ExtractConfig:
    """全文検索のための本文の抽出の設定（要件 F-EX-1・5・6、F-SE-2）。"""

    # Web の画面（serve）を動かしている間、ハッシュを計算済みのファイルから裏で本文を抽出し、索引に入れる
    enabled: bool = True
    # tesseract があれば、文字を取り出せないスキャンの PDF を OCR にかける
    ocr: bool = True
    # 画像（jpg・png など）も OCR にかけるか（写真やアイコンが多いと時間がかかり、意味の無い文字も拾うので既定は切る）
    ocr_images: bool = False
    # tesseract の言語。入っていないものは除いて使う（縦書きも読むなら "jpn+jpn_vert+eng"）
    ocr_lang: str = "jpn+eng"
    # OCR にかける PDF のページ数（先頭から）
    ocr_pages: int = 3
    # 1ファイルから保存する本文の上限（文字数）。索引の大きさを抑えるため
    max_chars: int = 50_000
    # これより大きいファイルは抽出しない（MB）
    max_mb: int = 200


@dataclass
class TranscribeConfig:
    """音声・動画の書き起こし（要件 F-EX-2）の設定。faster-whisper が要る（pip install 'tag-keeper[transcribe]'）。"""

    # Web の画面（serve）を動かしている間、新しい音声・動画を裏で書き起こすか（重いので既定は止める）
    auto: bool = False
    # faster-whisper のモデル（large-v3-turbo・large-v3・medium・small など、または Hugging Face のモデル名）
    model: str = "large-v3-turbo"
    # auto（GPU で動かせれば GPU）・cuda・cpu
    device: str = "auto"
    device_index: int = 0
    # auto なら GPU は float16、CPU は int8
    compute_type: str = "auto"
    # 空なら言語を自動で判定する（"ja" などで固定もできる）
    language: str = ""
    # 発話の検出のしきい値（下げるほど小さな声も拾う。下げすぎると物音に作文する）
    vad_threshold: float = 0.2
    # これより長いものは書き起こさない（分）
    max_minutes: int = 240
    # 裏の処理の1回で書き起こす件数
    batch: int = 5

    def options(self) -> Any:
        """書き起こしのプロセスに渡す設定。"""
        from tag_keeper.transcribe import Options

        return Options(self.model, self.device, self.device_index, self.compute_type, self.language, self.vad_threshold, self.max_minutes)


@dataclass
class DigestConfig:
    """定期レポート（要件 F-UI-3）の設定。"""

    # Web の画面（serve）を動かしている間、このファイルにレポートを書き直す（空なら書かない）
    path: str = ""
    # 書き直す間隔（時間）
    every_hours: int = 24
    # 「新着」とする日数
    days: int = 7


@dataclass
class AiConfig:
    """書類の構造化抽出と画像の内容推定（要件 F-AI-1〜3・F-EX-3）の設定。モデルは [organize] のものを使う。"""

    # Web の画面（serve）を動かしている間、新しい書類・画像を裏で読み取るか（モデルの問い合わせを伴うので既定は止める）
    auto: bool = False
    # 画像の内容を推定するか（画像を読めるモデルが要る）
    images: bool = True
    # 一度に読み取る件数（裏の処理の1回分）
    batch: int = 10


@dataclass
class OrganizeConfig:
    """受け皿のフォルダの整理の提案（名前と移動先）の設定。"""

    # 受け皿とみなすフォルダ名。大文字・小文字を区別しない glob で、フォルダ名（パスの最後）に照合する
    inbox_patterns: list[str] = field(default_factory=lambda: ["tmp", "temp", "*未整理*", "*inbox*"])
    # ollama の API の URL（外部のサービスには送らない。手元のモデルだけを使う）
    ollama_url: str = "http://127.0.0.1:11434"
    # 名前と移動先の提案に使うモデル（既定は手元のモデル）。
    # ollama のクラウドのモデル（例: "glm-5.3-flash:cloud"）も使えるが、その場合はファイルの内容が ollama のクラウドに送られる
    model: str = "gemma3:12b"
    # 画像を読むモデル。空なら model を使う（model が画像を読めなければ、画像は見せずに名前と場所から判断する）
    vision_model: str = ""
    # 画面でモデルを選ぶときの候補（ollama の手元のモデルに加えて出す。クラウドのモデルは手元の一覧に出ないため）
    model_choices: list[str] = field(default_factory=list)
    # 画像と、文字を取り出せないスキャン PDF を、画像としてモデルに見せるか
    use_images: bool = True
    # モデルに渡す本文の上限（文字数）
    max_chars: int = 4000
    # 似ているフォルダから絞り込み、モデルに選ばせる移動先の候補の数
    candidates: int = 15
    # 思考（thinking）に対応したモデルで考えさせるか。"true" / "false"、空ならモデルに任せる
    think: str = ""
    # think が空のとき、手元のモデル（外へ送らないモデル）に使う思考の設定。手元のモデルは考えさせると
    # 4〜20倍遅くなり、整理の提案の質はほとんど変わらなかった（2026-11 の試験）ので、既定は考えさせない
    local_think: str = "false"
    # 外部へ送らないルート（allow_cloud = false）で、クラウドのモデルの代わりに使う手元のモデル
    local_model: str = "gemma4:12b"
    # OpenAI 互換の API の送り先（名前 → ベースの URL。例: strata = "http://127.0.0.1:18080/v1"）。
    # モデルを「モデル名@名前」と書くと、ollama の代わりにそこへ送る（Strata・Project Maya・llama.cpp・vLLM など）
    endpoints: dict[str, str] = field(default_factory=dict)
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
    scan: ScanConfig = field(default_factory=ScanConfig)
    plan: PlanConfig = field(default_factory=PlanConfig)
    tags: TagsConfig = field(default_factory=TagsConfig)
    extract: ExtractConfig = field(default_factory=ExtractConfig)
    organize: OrganizeConfig = field(default_factory=OrganizeConfig)
    ai: AiConfig = field(default_factory=AiConfig)
    transcribe: TranscribeConfig = field(default_factory=TranscribeConfig)
    digest: DigestConfig = field(default_factory=DigestConfig)

    def find_root(self, name: str) -> RootConfig | None:
        """名前でルートを探す。見つからなければ None を返す。"""
        return next((r for r in self.roots if r.name == name), None)


class ConfigError(ValueError):
    """設定ファイルの内容が不正なときの例外。"""


def _expand(path: str) -> Path:
    """`~` と環境変数を展開した絶対パスを返す。"""
    return Path(os.path.expandvars(os.path.expanduser(path))).resolve()


def _replicas(path: Path, i: int, raw: object) -> dict[str, str]:
    """[[roots]] の replicas（{"中のフォルダ" = "元のルート名"}）を読む。"""
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: roots[{i}] の replicas は {{\"フォルダ\" = \"元のルート名\"}} の形で書いてください")
    out: dict[str, str] = {}
    for k, v in raw.items():
        rel = str(k).strip("/")
        if not rel or any(part in ("", ".", "..") for part in rel.split("/")):
            raise ConfigError(f"{path}: roots[{i}] の replicas のフォルダが不正です: {k!r}")
        out[rel] = str(v)
    return out


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
        if sync_client not in ("", "onedrive", "cloud-sync"):
            raise ConfigError(f"{path}: roots[{i}] の sync_client は \"onedrive\"・\"cloud-sync\" か空にしてください: {sync_client}")
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
                replicas=_replicas(path, i, item.get("replicas", {})),
                allow_cloud=bool(item.get("allow_cloud", True)),
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
    if "disabled" in hy:
        hygiene.disabled = [str(x) for x in hy["disabled"]]
    for key in ("stale_min_mb", "stale_days", "fanout_max"):
        if key in hy:
            setattr(hygiene, key, int(hy[key]))

    sf = data.get("safety", {})
    safety = SafetyConfig()
    if "mass_missing_ratio" in sf:
        safety.mass_missing_ratio = float(sf["mass_missing_ratio"])
    if "mass_missing_min" in sf:
        safety.mass_missing_min = int(sf["mass_missing_min"])

    sc = data.get("scan", {})
    scan = ScanConfig()
    if "auto_refresh" in sc:
        scan.auto_refresh = int(sc["auto_refresh"])
    if "after_sync" in sc:
        scan.after_sync = bool(sc["after_sync"])
    if "relink_days" in sc:
        scan.relink_days = int(sc["relink_days"])
    if scan.auto_refresh < 0 or scan.relink_days < 0:
        raise ConfigError(f"{path}: [scan] の auto_refresh と relink_days は 0 以上にしてください")

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

    ex = data.get("extract", {})
    extract = ExtractConfig()
    for key, conv in (
        ("enabled", bool),
        ("ocr", bool),
        ("ocr_images", bool),
        ("ocr_lang", str),
        ("ocr_pages", int),
        ("max_chars", int),
        ("max_mb", int),
    ):
        if key in ex:
            setattr(extract, key, conv(ex[key]))
    if extract.ocr_pages < 1 or extract.max_chars < 1 or extract.max_mb < 1:
        raise ConfigError(f"{path}: [extract] の ocr_pages・max_chars・max_mb は 1 以上にしてください")

    og = data.get("organize", {})
    organize = OrganizeConfig()
    if "inbox_patterns" in og:
        organize.inbox_patterns = [str(x) for x in og["inbox_patterns"]]
    if "model_choices" in og:
        organize.model_choices = [str(x) for x in og["model_choices"]]
    if "endpoints" in og:
        if not isinstance(og["endpoints"], dict):
            raise ConfigError(f"{path}: [organize.endpoints] は 名前 = \"URL\" の形で書いてください")
        organize.endpoints = {str(k): str(v).rstrip("/") for k, v in og["endpoints"].items()}
    for key, conv in (
        ("ollama_url", str),
        ("model", str),
        ("vision_model", str),
        ("local_model", str),
        ("think", str),
        ("local_think", str),
        ("use_images", bool),
        ("max_chars", int),
        ("candidates", int),
        ("temperature", float),
        ("timeout", float),
    ):
        if key in og:
            setattr(organize, key, conv(og[key]))

    ai_raw = data.get("ai", {})
    ai = AiConfig()
    for key, conv in (("auto", bool), ("images", bool), ("batch", int)):
        if key in ai_raw:
            setattr(ai, key, conv(ai_raw[key]))
    tr = data.get("transcribe", {})
    transcribe = TranscribeConfig()
    for key, conv in (
        ("auto", bool), ("model", str), ("device", str), ("device_index", int), ("compute_type", str),
        ("language", str), ("vad_threshold", float), ("max_minutes", int), ("batch", int),
    ):
        if key in tr:
            setattr(transcribe, key, conv(tr[key]))
    if transcribe.device not in ("auto", "cuda", "cpu"):
        raise ConfigError(f"{path}: [transcribe] の device は auto・cuda・cpu のどれかにしてください: {transcribe.device}")
    dg = data.get("digest", {})
    digest = DigestConfig()
    if "path" in dg:
        digest.path = str(_expand(str(dg["path"]))) if str(dg["path"]) else ""
    for key in ("every_hours", "days"):
        if key in dg:
            setattr(digest, key, int(dg[key]))
    return Config(
        roots=roots, hygiene=hygiene, safety=safety, scan=scan, plan=plan, tags=tags, extract=extract, organize=organize, ai=ai,
        transcribe=transcribe, digest=digest,
    )
