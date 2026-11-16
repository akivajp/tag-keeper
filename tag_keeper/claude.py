"""Claude API（Anthropic）をモデルの送り先として使う。

モデルを「モデル名@anthropic」と書くと、ollama の代わりに Claude API に送る（公式の SDK `anthropic` が要る。
`pip install 'tag-keeper[claude]'`）。エフォート（考える深さ）は「モデル名:エフォート@anthropic」で指定する::

    claude-haiku-5-5@anthropic          # エフォートは [organize] claude_effort（空ならモデルの既定）
    claude-opus-5-5:low@anthropic       # low / medium / high / xhigh / max

- API キーは、[organize] anthropic_key_file のファイル（1行）、環境変数 TAG_KEEPER_ANTHROPIC_KEY、ANTHROPIC_API_KEY の順に探す
- 送り先は [organize] anthropic_url（既定は https://api.anthropic.com）に明示する。環境変数 ANTHROPIC_BASE_URL は読まない
  （Claude Code 用のプロキシを指していることがあり、そこへ送ると API のクレジットではなくサブスクの枠を使ってしまう）
- 応答ごとのトークン数を、データのフォルダの usage/claude.jsonl に追記する（料金の見積もりに使う。`tag-keeper usage`）
"""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
import os
import socket
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tag_keeper.config import OrganizeConfig, default_data_dir

log = logging.getLogger(__name__)

# 「モデル名@anthropic」の @ の後ろ
ENDPOINT = "anthropic"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# 100万トークンあたりの料金（米ドル。入力, 出力）。2026-11 時点の公表値。新しいモデルが出たら足す
# （ここに無いモデルも使えるが、金額は出せない）
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-2": (8.00, 40.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
# 応じられない内容と判定されたときに、API の側で別のモデルに回す仕組み（fallbacks）に対応したモデル
_SERVER_FALLBACK = ("claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# 答えの形の指示（ollama は API の側で JSON を強制できるが、こちらは指示で守らせる。無いと、まれに文章で答える）
SYSTEM = "指示された JSON のオブジェクトを1つだけ出力してください。前置き・説明・見出し・コードの囲み（```）は付けません。"
# 1回の答えの上限（トークン。考えた分も含む）。ストリーミングなしで時間切れにならない範囲
MAX_TOKENS = 16000


class ClaudeError(RuntimeError):
    """Claude API に問い合わせられない・答えを使えないときの例外（organize が OllamaError に直す）。

    unavailable が真なら、一時的に応答しない（利用上限・混雑・接続できない・クレジット切れ）。手元のモデルに切り替える合図。
    """

    def __init__(self, message: str, *, unavailable: bool = False) -> None:
        super().__init__(message)
        self.unavailable = unavailable


@dataclass(frozen=True)
class Usage:
    """1回の問い合わせで使った量。"""

    at: str  # UTC の ISO 8601
    model: str  # 頼んだモデル（Claude API のモデル ID）
    effort: str  # 指定したエフォート（空ならモデルの既定）
    served_by: str  # 実際に答えたモデル（別のモデルに回されたときは model と違う）
    input_tokens: int
    output_tokens: int  # 考えた分を含む
    cache_read_tokens: int
    cache_write_tokens: int
    seconds: float
    purpose: str = ""  # 何のための問い合わせか（suggest / analyze など。呼び出し側が渡す）

    @property
    def cost(self) -> float | None:
        """料金の見積もり（米ドル）。料金表に無いモデルなら None。"""
        return cost_usd(self.served_by or self.model, self.input_tokens, self.output_tokens, self.cache_read_tokens, self.cache_write_tokens)


def split(model: str) -> tuple[str, str] | None:
    """「モデル名[:エフォート]@anthropic」なら（Claude API のモデル ID, エフォート）を返す。それ以外は None。"""
    name, sep, key = model.rpartition("@")
    if not sep or key != ENDPOINT or not name:
        return None
    model_id, _, effort = name.partition(":")
    return model_id, effort.lower()


def available() -> bool:
    """公式の SDK（anthropic）が入っているか。"""
    return importlib.util.find_spec("anthropic") is not None


def api_key(config: OrganizeConfig) -> str:
    """API キーを探す。無ければ空文字列。"""
    if config.anthropic_key_file:
        try:
            key = Path(os.path.expanduser(config.anthropic_key_file)).read_text(encoding="utf-8").strip()
        except OSError as e:
            raise ClaudeError(f"API キーのファイルを読めません: {config.anthropic_key_file}（{e}）") from e
        if key:
            return key
    return os.environ.get("TAG_KEEPER_ANTHROPIC_KEY", "").strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip()


def check(config: OrganizeConfig, model: str) -> None:
    """そのモデルに問い合わせる用意（SDK・API キー・エフォートの書き方）があるかを確かめる。無ければ ClaudeError。"""
    parts = split(model)
    if parts is None:
        raise ClaudeError(f"Claude API のモデルではありません: {model}")
    if parts[1] and parts[1] not in EFFORTS:
        raise ClaudeError(f"エフォートは {'・'.join(EFFORTS)} のどれかにしてください: {model}")
    if not available():
        raise ClaudeError("Claude API を使うには anthropic の SDK が要ります（pip install 'tag-keeper[claude]'）")
    if not api_key(config):
        raise ClaudeError(
            "Claude API のキーがありません（[organize] anthropic_key_file にキーを書いたファイルを指定するか、"
            "環境変数 TAG_KEEPER_ANTHROPIC_KEY か ANTHROPIC_API_KEY に設定してください）"
        )


def cost_usd(model_id: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float | None:
    """料金の見積もり（米ドル）。キャッシュからの読み出しは入力の 0.1 倍、書き込みは 1.25 倍で数える。料金表に無ければ None。"""
    price = PRICES.get(model_id)
    if price is None:
        return None
    per_in, per_out = price[0] / 1_000_000, price[1] / 1_000_000
    return input_tokens * per_in + cache_read * per_in * 0.1 + cache_write * per_in * 1.25 + output_tokens * per_out


def usage_path() -> Path:
    """利用の記録（JSONL）の置き場所。"""
    return default_data_dir() / "usage" / "claude.jsonl"


def record(usage: Usage) -> None:
    """利用の記録に1行足す（書けなくても、問い合わせの結果は捨てない）。"""
    path = usage_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({**asdict(usage), "host": socket.gethostname()}, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("Claude API の利用の記録を書けません: %s", e)


def read_usage(days: int | None = None) -> list[Usage]:
    """利用の記録を読む（days を渡すと、その日数以内のものだけ）。読めない行は飛ばす。"""
    path = usage_path()
    if not path.exists():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds") if days else ""
    fields = set(Usage.__dataclass_fields__)
    out: list[Usage] = []
    # 1行ずつ読む（長く使うと大きくなるので、全体を一度に読み込まない）
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
                u = Usage(**{k: v for k, v in d.items() if k in fields})
            except (ValueError, TypeError):
                continue
            if u.at >= cutoff:
                out.append(u)
    return out


def summarize(usages: list[Usage]) -> list[dict[str, Any]]:
    """利用の記録を（モデル, エフォート, 用途）ごとにまとめる（件数・トークン数・料金・1件あたりの秒数）。"""
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for u in usages:
        g = groups.setdefault((u.model, u.effort, u.purpose), {
            "model": u.model, "effort": u.effort, "purpose": u.purpose, "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "seconds": 0.0, "cost": 0.0, "unpriced": 0,
        })
        g["calls"] += 1
        g["input_tokens"] += u.input_tokens + u.cache_read_tokens + u.cache_write_tokens
        g["output_tokens"] += u.output_tokens
        g["seconds"] += u.seconds
        if u.cost is None:
            g["unpriced"] += 1
        else:
            g["cost"] += u.cost
    return [groups[k] for k in sorted(groups)]


def _client(config: OrganizeConfig) -> Any:
    """Claude API のクライアント（送り先は設定のものに明示する。環境変数 ANTHROPIC_BASE_URL は使わない）。"""
    import anthropic

    return anthropic.Anthropic(api_key=api_key(config), base_url=config.anthropic_url, timeout=config.timeout, max_retries=2)


def generate(config: OrganizeConfig, model: str, prompt: str, image: bytes | None = None, *, mime: str = "image/png", purpose: str = "") -> str:
    """Claude API に問い合わせ、答えの文字（JSON を期待する指示文への答え）を返す。使った量を記録する。

    Raises:
        ClaudeError: 用意が無い・応じられないと判定された・答えが途中で切れた・API のエラー。
    """
    check(config, model)
    import anthropic

    model_id, effort = split(model)  # type: ignore[misc]  # check で確かめてある
    effort = effort or config.claude_effort
    content: list[dict[str, Any]] = []
    if image is not None:
        # 画像は文より前に置く
        content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.standard_b64encode(image).decode("ascii")}})
    content.append({"type": "text", "text": prompt})
    kwargs: dict[str, Any] = {"model": model_id, "max_tokens": MAX_TOKENS, "system": SYSTEM, "messages": [{"role": "user", "content": content}]}
    # 考える深さはエフォートで決める（今のモデルは考えることを止められず、temperature も受け付けない）
    if effort:
        kwargs["output_config"] = {"effort": effort}
    client = _client(config)
    began = time.monotonic()
    try:
        if model_id in _SERVER_FALLBACK:
            # 応じられない内容と誤って判定されたときは、API の側で別のモデルが続きを答える
            res = client.beta.messages.create(betas=[_FALLBACK_BETA], fallbacks="default", **kwargs)
        else:
            res = client.messages.create(**kwargs)
    except anthropic.RateLimitError as e:
        raise ClaudeError(f"Claude API の利用上限に達しています（{model_id}）: {e.message}", unavailable=True) from e
    except anthropic.AuthenticationError as e:
        raise ClaudeError(f"Claude API のキーが正しくありません: {e.message}") from e
    except anthropic.PermissionDeniedError as e:
        raise ClaudeError(f"Claude API のキーに、このモデルを使う権限がありません（{model_id}）: {e.message}") from e
    except anthropic.NotFoundError as e:
        raise ClaudeError(f"Claude API にモデル {model_id} がありません: {e.message}") from e
    except anthropic.APIStatusError as e:
        # 混雑（5xx・529）とクレジット切れは、手元のモデルに切り替える合図にする。それ以外（400 など）はそのファイルの失敗
        down = e.status_code >= 500 or "credit balance" in str(e.message).lower()
        raise ClaudeError(f"Claude API が HTTP {e.status_code} を返しました（{model_id}）: {e.message}", unavailable=down) from e
    except anthropic.APIConnectionError as e:
        raise ClaudeError(f"Claude API に接続できません（{config.anthropic_url}）: {e}", unavailable=True) from e
    u = res.usage
    record(Usage(
        at=datetime.now(timezone.utc).isoformat(timespec="seconds"), model=model_id, effort=effort, served_by=str(res.model or model_id),
        input_tokens=int(u.input_tokens or 0), output_tokens=int(u.output_tokens or 0),
        cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0), cache_write_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        seconds=round(time.monotonic() - began, 2), purpose=purpose,
    ))
    if res.stop_reason == "refusal":
        raise ClaudeError(f"Claude が、この内容には応じられないと判定しました（{model_id}）")
    text = "".join(block.text for block in res.content if block.type == "text")
    if res.stop_reason == "max_tokens" and not text.strip():
        raise ClaudeError(f"答えが上限（{MAX_TOKENS} トークン）で途中で切れました（{model_id}・エフォート {effort or '既定'}）")
    return text
