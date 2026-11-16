"""Claude API を送り先にする経路（振り分け・エフォート・利用の記録と料金・失敗の扱い）のテスト。API には送らず、クライアントを差し替える。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import write
from tag_keeper import claude, organize
from tag_keeper.cli import main
from tag_keeper.config import ConfigError, OrganizeConfig, load_config

pytest.importorskip("anthropic")


class FakeMessages:
    """messages.create の代わり。受け取った引数を覚え、決めた答えを返す。"""

    def __init__(self, calls: list[dict[str, Any]], answer: Any) -> None:
        self.calls, self.answer = calls, answer

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def response(text: str = '{"doc_type": "請求書"}', *, model: str = "", stop: str = "end_turn", tokens: tuple[int, int] = (1200, 300)) -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)], stop_reason=stop, model=model,
        usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1], cache_read_input_tokens=0, cache_creation_input_tokens=0),
    )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """API キーを用意し、クライアントを偽物にする。state["answer"] を変えると、返す答えが変わる。"""
    state: dict[str, Any] = {"calls": [], "beta_calls": [], "answer": response()}
    monkeypatch.setenv("TAG_KEEPER_ANTHROPIC_KEY", "sk-test")
    # Claude Code 用のプロキシを指す環境変数があっても、送り先は設定のもの
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8787")

    def client(config: OrganizeConfig) -> Any:
        state["url"] = config.anthropic_url
        return SimpleNamespace(
            messages=FakeMessages(state["calls"], state["answer"]),
            beta=SimpleNamespace(messages=FakeMessages(state["beta_calls"], state["answer"])),
        )

    monkeypatch.setattr(claude, "_client", client)
    return state


def test_model_names() -> None:
    assert claude.split("claude-haiku-5-5@anthropic") == ("claude-haiku-5-5", "")
    assert claude.split("claude-opus-5-5:LOW@anthropic") == ("claude-opus-5-5", "low")
    assert claude.split("gemma4:12b") is None and claude.split("qwen@strata") is None
    oc = OrganizeConfig()
    # Claude API は常に「外部へ送る」。外部へ送らないルートでは手元のモデルに切り替える
    assert organize.sends_outside(oc, "claude-haiku-5-5@anthropic")
    local, note = organize.config_for_root(OrganizeConfig(model="claude-haiku-5-5@anthropic"), False)
    assert local.model == oc.local_model and note


def test_generate_sends_effort_and_records_usage(fake: dict[str, Any]) -> None:
    oc = OrganizeConfig(model="claude-haiku-5-5:low@anthropic")
    with organize.purpose("suggest"):
        assert organize.ollama_generate(oc, "読んで", b"\xff\xd8\xff fake jpeg") == {"doc_type": "請求書"}
    call = fake["calls"][0]
    assert call["model"] == "claude-haiku-5-5" and call["output_config"] == {"effort": "low"}
    assert "temperature" not in call and "thinking" not in call  # 今のモデルはどちらも受け付けない
    assert "JSON" in call["system"]  # 文章で答えないよう、答えの形を指示する
    image, text = call["messages"][0]["content"]
    assert image["source"]["media_type"] == "image/jpeg" and text == {"type": "text", "text": "読んで"}
    assert fake["url"] == "https://api.anthropic.com"
    # 利用の記録と料金（Haiku 5.5: 入力 $0.10・出力 $0.50 / 100万トークン）
    (u,) = claude.read_usage()
    assert (u.model, u.effort, u.purpose, u.input_tokens, u.output_tokens) == ("claude-haiku-5-5", "low", "suggest", 1200, 300)
    assert u.cost == pytest.approx(1200 * 0.10e-6 + 300 * 0.50e-6)
    assert claude.cost_usd("claude-opus-5-5", 1_000_000, 1_000_000) == pytest.approx(24.0)
    assert claude.cost_usd("unknown-model", 1, 1) is None


def test_default_effort_and_server_side_fallback(fake: dict[str, Any]) -> None:
    """エフォートは、モデル名に無ければ設定のもの、それも無ければ送らない（モデルの既定）。Opus・Sonnet・Fable は、
    応じられないと誤って判定されたときに API の側で別のモデルが答える仕組みを使う。"""
    organize.ollama_generate(OrganizeConfig(model="claude-haiku-5-5@anthropic"), "読んで")
    assert "output_config" not in fake["calls"][0]
    fake["answer"] = response(model="claude-opus-4-8")
    oc = OrganizeConfig(model="claude-opus-5-5@anthropic", claude_effort="medium")
    organize.ollama_generate(oc, "読んで")
    call = fake["beta_calls"][0]
    assert call["output_config"] == {"effort": "medium"} and call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    # 実際に答えたモデルを記録し、料金もそのモデルで見積もる（料金表に無ければ出さない）
    assert claude.read_usage()[-1].served_by == "claude-opus-4-8" and claude.read_usage()[-1].cost is None


def test_failures(fake: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import anthropic
    import httpx2

    def error(cls: type, status: int, message: str) -> Exception:
        req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        return cls(message, response=httpx2.Response(status, request=req), body={"error": {"message": message}})

    oc = OrganizeConfig(model="claude-haiku-5-5@anthropic")
    # 利用上限・混雑・クレジット切れは、手元のモデルに切り替える合図
    for exc in (error(anthropic.RateLimitError, 429, "rate limited"), error(anthropic.InternalServerError, 529, "overloaded"),
                error(anthropic.BadRequestError, 400, "Your credit balance is too low")):
        fake["answer"] = exc
        with pytest.raises(organize.ModelUnavailable):
            organize.ollama_generate(oc, "読んで")
    # 入力が悪い・キーが違うのは、切り替えずに知らせる
    for exc in (error(anthropic.BadRequestError, 400, "image too large"), error(anthropic.AuthenticationError, 401, "invalid x-api-key")):
        fake["answer"] = exc
        with pytest.raises(organize.OllamaError) as e:
            organize.ollama_generate(oc, "読んで")
        assert not isinstance(e.value, organize.ModelUnavailable)
    fake["answer"] = response(stop="refusal", text="")
    with pytest.raises(organize.OllamaError, match="応じられない"):
        organize.ollama_generate(oc, "読んで")
    # 用意が無いときは、何が足りないかを知らせる
    with pytest.raises(organize.OllamaError, match="エフォート"):
        organize.model_capabilities(oc, "claude-haiku-5-5:turbo@anthropic")
    assert organize.model_capabilities(oc) == ["completion", "vision"]
    monkeypatch.delenv("TAG_KEEPER_ANTHROPIC_KEY")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(organize.OllamaError, match="キーがありません"):
        organize.model_capabilities(oc)


def test_config_keys_and_usage_command(tmp_path: Path, fake: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    key = write(tmp_path / "key", "sk-from-file\n")
    cfg = write(tmp_path / "c.toml", f'[organize]\nmodel = "claude-haiku-5-5@anthropic"\nanthropic_key_file = "{key}"\nclaude_effort = "low"\n')
    oc = load_config(cfg).organize
    assert claude.api_key(oc) == "sk-from-file" and oc.claude_effort == "low"
    for bad in ('[organize]\nclaude_effort = "turbo"\n', '[organize.endpoints]\nanthropic = "http://x/v1"\n'):
        with pytest.raises(ConfigError):
            load_config(write(tmp_path / "bad.toml", bad))
    organize.ollama_generate(oc, "読んで")
    organize.ollama_generate(oc, "読んで")
    base = ["--config", str(cfg), "--db", str(tmp_path / "c.db")]
    assert main([*base, "usage", "--json"]) == 0
    assert '"calls": 2' in capsys.readouterr().out
    assert main([*base, "usage", "--days", "1"]) == 0
    assert "合計: 0.0005 USD" in capsys.readouterr().out
