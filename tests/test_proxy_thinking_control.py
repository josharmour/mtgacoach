"""The real-time coach path must control GLM-5.3's thinking explicitly.

The engine now behind the dsv4/deepseek-v4-flash aliases is GLM-5.3-Flash
(serve-glm53-blackwell.sh), launched with
``--default-chat-template-kwargs.thinking=true`` and
``.reasoning_effort=high``. GLM ALWAYS deliberates: with ``thinking:false``
the reasoning is NOT separated into ``reasoning_content`` and leaks inline
into the spoken advice (observed live 2026-08-28/29 as the model echoing the
QUICK-mode prompt instructions into the TTS text). With
``thinking=true, reasoning_effort=low`` the glm45 reasoning parser separates
the deliberation and the content is a clean short command.

Interleaved bench through the gateway (2026-08-29, ~25k-char prompts):
high-effort default 3-11s; thinking=false fragmented/junk content;
thinking=true + effort=low ~0.2-0.9s clean (one in four calls returned empty
content, which coach-side salvage covers).

CAVEAT: if a real DeepSeek-dialect dsv4 container returns to :8002 the
low-effort kwargs would slow it (thinking=true was the 2026-07-29 p50 6134ms
bug — see test file history); re-gate on the served engine then.
"""

from __future__ import annotations

import pytest

from arenamcp.backends.proxy import ProxyBackend


@pytest.fixture
def captured(monkeypatch):
    """Capture the request params without touching the network."""
    seen: dict = {}

    def fake_complete_once(self, client, params):
        seen.clear()
        seen.update(params)
        return "Play Forest."

    monkeypatch.setattr(ProxyBackend, "_complete_once", fake_complete_once, raising=True)
    monkeypatch.setattr(ProxyBackend, "_local_warmup", lambda self: None, raising=False)
    return seen


def _run(thinking: bool, model: str = "deepseek-v4-flash") -> None:
    be = ProxyBackend(
        model=model,
        enable_thinking=thinking,
        base_url="http://127.0.0.1:9/v1",
        api_key="sk-test",
    )
    be.complete("sys", "user", max_tokens=256)


def test_disabled_sends_thinking_true_with_low_effort(captured):
    """THE regression test: coach's quick path must run GLM at low effort."""
    _run(thinking=False)
    extra = captured.get("extra_body") or {}
    ctk = extra.get("chat_template_kwargs") or {}
    assert ctk.get("thinking") is True, (
        "GLM deliberates regardless; thinking=true keeps the reasoning in "
        "reasoning_content (separated) instead of leaking into spoken advice"
    )
    assert ctk.get("reasoning_effort") == "low", (
        "the engine default is reasoning_effort=high — 3-11s coach latency"
    )


def test_thinking_enabled_is_sent_explicitly(captured):
    """The win-plan path must not depend on the server default either."""
    _run(thinking=True)
    extra = captured.get("extra_body") or {}
    assert extra.get("chat_template_kwargs", {}).get("thinking") is True


def test_ollama_think_flag_no_longer_sent(captured):
    """No alias routes to Ollama anymore; `think:false` would contradict
    chat_template_kwargs.thinking=true on GLM."""
    _run(thinking=False)
    extra = captured.get("extra_body") or {}
    assert "think" not in extra


@pytest.mark.parametrize("model", ["deepseek-v4-flash", "gemma-4-12b-it", "qwen-uncensored"])
def test_disable_applies_across_gateway_models(captured, model):
    """Every model served by the gateway goes through the same vLLM template."""
    _run(thinking=False, model=model)
    extra = captured.get("extra_body") or {}
    ctk = extra["chat_template_kwargs"]
    assert ctk["thinking"] is True
    assert ctk["reasoning_effort"] == "low"


def test_claude_thinking_config_unaffected(captured):
    """Anthropic's native field must still be set when thinking is enabled."""
    _run(thinking=True, model="claude-sonnet-4")
    extra = captured.get("extra_body") or {}
    assert extra.get("thinking") == {"type": "enabled", "budget_tokens": 8000}
    # ...and the vLLM key rides along harmlessly for gateway-routed Claude.
    assert extra["chat_template_kwargs"]["thinking"] is True
