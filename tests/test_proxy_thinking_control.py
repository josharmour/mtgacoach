"""``enable_thinking=False`` must actually reach a vLLM/DeepSeek server.

The gateway's ds4-v9 vLLM server is launched with
``--default-chat-template-kwargs.thinking=true`` and ``.reasoning_effort=high``,
so a request that says nothing about thinking gets extended reasoning. The
real-time coach path constructs ``ProxyBackend`` with ``enable_thinking=False``
(the default) and *intends* no reasoning, but the disable branch only ever spoke
the Ollama (``think``), Gemini and GPT-5 (``reasoning_effort``) dialects — none of
which override a vLLM chat-template default. So the intent was silently a no-op.

Measured on the live gateway with the real 21.5k-char coach prompt captured in
``bug_reports/bug_20260729_225652.json`` (n=5 each):

    OLD  mean 2772ms  median 2235ms  max 5749ms
    NEW  mean  506ms  median  384ms  max 1095ms

and in the field log, 79 calls returned EMPTY visible content because every token
went to reasoning — which is how chain-of-thought reached the TTS.
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


def test_thinking_disabled_is_sent_as_chat_template_kwargs(captured):
    """THE regression test: the disable must be expressed in vLLM's dialect."""
    _run(thinking=False)
    extra = captured.get("extra_body") or {}
    assert "chat_template_kwargs" in extra, (
        "enable_thinking=False must send chat_template_kwargs; without it a vLLM "
        "server launched with --default-chat-template-kwargs.thinking=true keeps "
        "reasoning and the disable is a silent no-op"
    )
    assert extra["chat_template_kwargs"]["thinking"] is False


def test_thinking_enabled_is_sent_explicitly(captured):
    """The win-plan path must not depend on the server default either."""
    _run(thinking=True)
    extra = captured.get("extra_body") or {}
    assert extra.get("chat_template_kwargs", {}).get("thinking") is True


def test_ollama_think_flag_still_sent_when_disabled(captured):
    """Pre-existing Ollama control must not regress."""
    _run(thinking=False)
    extra = captured.get("extra_body") or {}
    assert extra.get("think") is False


@pytest.mark.parametrize("model", ["deepseek-v4-flash", "gemma-4-12b-it", "qwen-uncensored"])
def test_disable_applies_across_gateway_models(captured, model):
    """Every model served by the gateway goes through the same vLLM template."""
    _run(thinking=False, model=model)
    extra = captured.get("extra_body") or {}
    assert extra["chat_template_kwargs"]["thinking"] is False


def test_claude_thinking_config_unaffected(captured):
    """Anthropic's native field must still be set when thinking is enabled."""
    _run(thinking=True, model="claude-sonnet-4")
    extra = captured.get("extra_body") or {}
    assert extra.get("thinking") == {"type": "enabled", "budget_tokens": 8000}
    # ...and the vLLM key rides along harmlessly for gateway-routed Claude.
    assert extra["chat_template_kwargs"]["thinking"] is True
