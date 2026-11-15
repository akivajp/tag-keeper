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
    model = "gemma4:12b"             # 名前と移動先の提案に使うモデル（":cloud" などのクラウドのモデルも可。内容がクラウドに送られる）
    vision_model = ""                # 画像を読むモデル（空なら model）
    model_choices = ["glm-5.3-flash:cloud", "gemma4:31b-cloud"]  # 画面で選べるモデルの候補
    use_images = true                # 画像と、文字の無いスキャン PDF をモデルに見せる
    max_chars = 4000                 # モデルに渡す本文の上限（文字数）
    candidates = 15                  # モデルに選ばせる移動先の候補の数
"""

from __future__ import annotations

import difflib
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
    # ollama の API の URL。手元（ループバック・LAN・Tailscale）でなければ、ollama のどのモデルも「外部へ送る」扱いになる
    ollama_url: str = "http://127.0.0.1:11434"
    # 名前と移動先の提案に使うモデル（既定は手元のモデル）。
    # ollama のクラウドのモデル（例: "glm-5.3-flash:cloud"）も使えるが、その場合はファイルの内容が ollama のクラウドに送られる
    model: str = "gemma4:12b"
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
    # 主のモデルが応答しないとき（クラウドの利用上限・推論サーバーが止まっている・時間切れ）、local_model で代わりに処理するか
    fallback: bool = True
    # OpenAI 互換の API の送り先（名前 → ベースの URL。例: strata = "http://127.0.0.1:18080/v1"）。
    # モデルを「モデル名@名前」と書くと、ollama の代わりにそこへ送る（Strata・Project Maya・llama.cpp・vLLM など）
    endpoints: dict[str, str] = field(default_factory=dict)
    # 生成の温度（低いほど毎回同じ提案になる）
    temperature: float = 0.2
    # 1件の問い合わせの待ち時間の上限（秒）
    timeout: float = 300.0
    # Claude API（「モデル名@anthropic」のモデル）の API キーを書いたファイル（1行）。空なら環境変数
    # TAG_KEEPER_ANTHROPIC_KEY・ANTHROPIC_API_KEY から読む
    anthropic_key_file: str = ""
    # Claude API の送り先（環境変数 ANTHROPIC_BASE_URL は読まない。Claude Code 用のプロキシを指していることがあるため）
    anthropic_url: str = "https://api.anthropic.com"
    # Claude API のモデルのエフォート（low・medium・high・xhigh・max。空ならモデルの既定）。
    # モデル名に付けたもの（claude-opus-5-5:low@anthropic）が優先
    claude_effort: str = ""


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


class _Table:
    """設定ファイルの1つの表。値を型を確かめながら取り出し、最後に知らないキーが残っていないかを確かめる。

    綴りの違うキー（allow-cloud など）や型の違う値（allow_cloud = "false"）を黙って受け入れると、
    「外部へ送らない」のような守りの設定が効かないまま動いてしまうので、どちらも ConfigError にする。
    """

    def __init__(self, path: Path, where: str, raw: object) -> None:
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: {where} は表（キー = 値 の並び）で書いてください")
        self.path = path
        self.where = where
        self.raw: dict[str, Any] = raw
        # 取り出そうとしたキー（書かれていなくても記録する。知らないキーの判定と、綴りの候補に使う）
        self.known: set[str] = set()

    def has(self, key: str) -> bool:
        """そのキーが書かれているか。"""
        self.known.add(key)
        return key in self.raw

    def _bad(self, key: str, want: str) -> ConfigError:
        return ConfigError(f"{self.path}: {self.where} の {key} は{want}で書いてください: {self.raw[key]!r}")

    def get_bool(self, key: str, default: bool) -> bool:
        """真偽値（true / false）。文字列の "false" などは受け付けない。"""
        if not self.has(key):
            return default
        v = self.raw[key]
        if not isinstance(v, bool):
            raise self._bad(key, " true か false（引用符なし）")
        return v

    def get_int(self, key: str, default: int) -> int:
        """整数。"""
        if not self.has(key):
            return default
        v = self.raw[key]
        if isinstance(v, bool) or not isinstance(v, int):
            raise self._bad(key, "整数")
        return v

    def get_float(self, key: str, default: float) -> float:
        """数（整数か小数）。"""
        if not self.has(key):
            return default
        v = self.raw[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise self._bad(key, "数")
        return float(v)

    def get_str(self, key: str, default: str) -> str:
        """文字列。"""
        if not self.has(key):
            return default
        v = self.raw[key]
        if not isinstance(v, str):
            raise self._bad(key, "文字列（引用符つき）")
        return v

    def get_str_list(self, key: str, default: list[str]) -> list[str]:
        """文字列の配列。文字列を1つだけ書いたもの（1文字ずつの配列として読めてしまう）は受け付けない。"""
        if not self.has(key):
            return default
        v = self.raw[key]
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise self._bad(key, "文字列の配列（[\"…\", \"…\"]）")
        return list(v)

    def get_switch(self, key: str, default: str) -> str:
        """"true" / "false" / 空 の3通りの文字列（think など）。TOML の真偽値で書いたものも受け付ける。"""
        if not self.has(key):
            return default
        v = self.raw[key]
        if isinstance(v, bool):
            return "true" if v else "false"
        if not isinstance(v, str) or v.strip().lower() not in ("", "true", "false"):
            raise self._bad(key, " \"true\"・\"false\" か空")
        return v.strip().lower()

    def finish(self) -> None:
        """知らないキーが書かれていれば ConfigError（似た名前のキーがあれば添える）。"""
        for key in self.raw:
            if key in self.known:
                continue
            near = difflib.get_close_matches(str(key), sorted(self.known), n=1)
            hint = f"（{near[0]} の間違いではありませんか）" if near else ""
            raise ConfigError(f"{self.path}: {self.where} に知らない設定があります: {key}{hint}")


# 設定ファイルに書ける節（これ以外は、綴りの違いとみなして知らせる）
_SECTIONS = ("roots", "hygiene", "safety", "scan", "plan", "tags", "extract", "organize", "ai", "transcribe", "digest")


def load_config(path: Path | None = None) -> Config:
    """設定ファイルを読み込む。ファイルが無ければ既定値の設定を返す。

    知らない節・キーと、型の合わない値は ConfigError にする（黙って既定値のまま動かさない）。

    Args:
        path: 設定ファイルのパス。None なら既定のパスを使う。
    """
    path = path or default_config_path()
    if not path.exists():
        return Config()
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: TOML として読めません: {e}") from e
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path}: 読めません: {e}") from e
    for key in data:
        if key not in _SECTIONS:
            near = difflib.get_close_matches(str(key), _SECTIONS, n=1)
            hint = f"（{near[0]} の間違いではありませんか）" if near else ""
            raise ConfigError(f"{path}: 知らない節があります: [{key}]{hint}")

    def table(name: str) -> _Table:
        return _Table(path, f"[{name}]", data.get(name, {}))

    raw_roots = data.get("roots", [])
    if not isinstance(raw_roots, list):
        raise ConfigError(f"{path}: roots は [[roots]] の形で書いてください")
    roots: list[RootConfig] = []
    for i, item in enumerate(raw_roots):
        t = _Table(path, f"roots[{i}]", item)
        if not t.has("name") or not t.has("path"):
            raise ConfigError(f"{path}: roots[{i}] には name と path が必要です")
        sync_client = t.get_str("sync_client", "")
        if sync_client not in ("", "onedrive", "cloud-sync"):
            raise ConfigError(f"{path}: roots[{i}] の sync_client は \"onedrive\"・\"cloud-sync\" か空にしてください: {sync_client}")
        sync_guard = t.get_str("sync_guard", "auto")
        if sync_guard not in ("auto", "always"):
            raise ConfigError(f"{path}: roots[{i}] の sync_guard は \"auto\" か \"always\" にしてください: {sync_guard}")
        roots.append(
            RootConfig(
                name=t.get_str("name", ""),
                path=_expand(t.get_str("path", "")),
                exclude=t.get_str_list("exclude", []),
                sync_client=sync_client,
                sync_service=t.get_str("sync_service", "onedrive.service"),
                sync_guard=sync_guard,
                replicas=_replicas(path, i, item["replicas"] if t.has("replicas") else {}),
                allow_cloud=t.get_bool("allow_cloud", True),
            )
        )
        t.finish()
    names = [r.name for r in roots]
    if len(names) != len(set(names)):
        raise ConfigError(f"{path}: roots の name が重複しています")

    hy = table("hygiene")
    hygiene = HygieneConfig()
    hygiene.allow = hy.get_str_list("allow", hygiene.allow)
    hygiene.device_names = hy.get_str_list("device_names", hygiene.device_names)
    hygiene.app_data_names = hy.get_str_list("app_data_names", hygiene.app_data_names)
    hygiene.app_data_min_files = hy.get_int("app_data_min_files", hygiene.app_data_min_files)
    hygiene.disabled = hy.get_str_list("disabled", hygiene.disabled)
    hygiene.stale_min_mb = hy.get_int("stale_min_mb", hygiene.stale_min_mb)
    hygiene.stale_days = hy.get_int("stale_days", hygiene.stale_days)
    hygiene.fanout_max = hy.get_int("fanout_max", hygiene.fanout_max)
    hy.finish()

    sf = table("safety")
    safety = SafetyConfig()
    safety.mass_missing_ratio = sf.get_float("mass_missing_ratio", safety.mass_missing_ratio)
    safety.mass_missing_min = sf.get_int("mass_missing_min", safety.mass_missing_min)
    sf.finish()

    sc = table("scan")
    scan = ScanConfig()
    scan.auto_refresh = sc.get_int("auto_refresh", scan.auto_refresh)
    scan.after_sync = sc.get_bool("after_sync", scan.after_sync)
    scan.relink_days = sc.get_int("relink_days", scan.relink_days)
    sc.finish()
    if scan.auto_refresh < 0 or scan.relink_days < 0:
        raise ConfigError(f"{path}: [scan] の auto_refresh と relink_days は 0 以上にしてください")

    pl = table("plan")
    plan = PlanConfig()
    if pl.has("quarantine_dir"):
        plan.quarantine_dir = _expand(pl.get_str("quarantine_dir", ""))
    plan.snapper_config = pl.get_str("snapper_config", plan.snapper_config)
    pl.finish()

    tg = table("tags")
    tags = TagsConfig()
    if tg.has("dir"):
        tags.dir = _expand(tg.get_str("dir", ""))
    tg.finish()

    ex = table("extract")
    extract = ExtractConfig()
    extract.enabled = ex.get_bool("enabled", extract.enabled)
    extract.ocr = ex.get_bool("ocr", extract.ocr)
    extract.ocr_images = ex.get_bool("ocr_images", extract.ocr_images)
    extract.ocr_lang = ex.get_str("ocr_lang", extract.ocr_lang)
    extract.ocr_pages = ex.get_int("ocr_pages", extract.ocr_pages)
    extract.max_chars = ex.get_int("max_chars", extract.max_chars)
    extract.max_mb = ex.get_int("max_mb", extract.max_mb)
    ex.finish()
    if extract.ocr_pages < 1 or extract.max_chars < 1 or extract.max_mb < 1:
        raise ConfigError(f"{path}: [extract] の ocr_pages・max_chars・max_mb は 1 以上にしてください")

    og = table("organize")
    organize = OrganizeConfig()
    organize.inbox_patterns = og.get_str_list("inbox_patterns", organize.inbox_patterns)
    organize.model_choices = og.get_str_list("model_choices", organize.model_choices)
    if og.has("endpoints"):
        raw_eps = og.raw["endpoints"]
        if not isinstance(raw_eps, dict) or not all(isinstance(v, str) for v in raw_eps.values()):
            raise ConfigError(f"{path}: [organize.endpoints] は 名前 = \"URL\" の形で書いてください")
        organize.endpoints = {str(k): v.rstrip("/") for k, v in raw_eps.items()}
    organize.ollama_url = og.get_str("ollama_url", organize.ollama_url)
    organize.model = og.get_str("model", organize.model)
    organize.vision_model = og.get_str("vision_model", organize.vision_model)
    organize.local_model = og.get_str("local_model", organize.local_model)
    organize.think = og.get_switch("think", organize.think)
    organize.local_think = og.get_switch("local_think", organize.local_think)
    organize.fallback = og.get_bool("fallback", organize.fallback)
    organize.use_images = og.get_bool("use_images", organize.use_images)
    organize.max_chars = og.get_int("max_chars", organize.max_chars)
    organize.candidates = og.get_int("candidates", organize.candidates)
    organize.temperature = og.get_float("temperature", organize.temperature)
    organize.timeout = og.get_float("timeout", organize.timeout)
    organize.anthropic_key_file = og.get_str("anthropic_key_file", organize.anthropic_key_file)
    organize.anthropic_url = og.get_str("anthropic_url", organize.anthropic_url).rstrip("/")
    organize.claude_effort = og.get_str("claude_effort", organize.claude_effort).strip().lower()
    og.finish()
    if organize.claude_effort not in ("", "low", "medium", "high", "xhigh", "max"):
        raise ConfigError(f"{path}: [organize] の claude_effort は low・medium・high・xhigh・max か空にしてください: {organize.claude_effort}")
    if "anthropic" in organize.endpoints:
        raise ConfigError(f"{path}: [organize.endpoints] の名前 anthropic は Claude API 用に予約されています（別の名前にしてください）")
    if organize.max_chars < 1 or organize.candidates < 1 or organize.timeout <= 0:
        raise ConfigError(f"{path}: [organize] の max_chars・candidates は 1 以上、timeout は 0 より大きくしてください")

    ai_raw = table("ai")
    ai = AiConfig()
    ai.auto = ai_raw.get_bool("auto", ai.auto)
    ai.images = ai_raw.get_bool("images", ai.images)
    ai.batch = ai_raw.get_int("batch", ai.batch)
    ai_raw.finish()

    tr = table("transcribe")
    transcribe = TranscribeConfig()
    transcribe.auto = tr.get_bool("auto", transcribe.auto)
    transcribe.model = tr.get_str("model", transcribe.model)
    transcribe.device = tr.get_str("device", transcribe.device)
    transcribe.device_index = tr.get_int("device_index", transcribe.device_index)
    transcribe.compute_type = tr.get_str("compute_type", transcribe.compute_type)
    transcribe.language = tr.get_str("language", transcribe.language)
    transcribe.vad_threshold = tr.get_float("vad_threshold", transcribe.vad_threshold)
    transcribe.max_minutes = tr.get_int("max_minutes", transcribe.max_minutes)
    transcribe.batch = tr.get_int("batch", transcribe.batch)
    tr.finish()
    if transcribe.device not in ("auto", "cuda", "cpu"):
        raise ConfigError(f"{path}: [transcribe] の device は auto・cuda・cpu のどれかにしてください: {transcribe.device}")

    dg = table("digest")
    digest = DigestConfig()
    if dg.has("path"):
        raw_path = dg.get_str("path", "")
        digest.path = str(_expand(raw_path)) if raw_path else ""
    digest.every_hours = dg.get_int("every_hours", digest.every_hours)
    digest.days = dg.get_int("days", digest.days)
    dg.finish()
    if ai.batch < 0 or transcribe.batch < 0 or transcribe.max_minutes < 1 or digest.every_hours < 1 or digest.days < 1:
        raise ConfigError(
            f"{path}: [ai] batch・[transcribe] batch は 0 以上、[transcribe] max_minutes・[digest] every_hours・days は 1 以上にしてください"
        )
    return Config(
        roots=roots, hygiene=hygiene, safety=safety, scan=scan, plan=plan, tags=tags, extract=extract, organize=organize, ai=ai,
        transcribe=transcribe, digest=digest,
    )
