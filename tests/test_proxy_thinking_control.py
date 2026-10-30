"""Current/DeepSeek respects disabled thinking; explicit GLM retains its parser workaround."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.backends.proxy import ProxyBackend


@pytest.fixture
def captured(monkeypatch):
    """Capture the request params without touching the network."""
    seen: dict = {}

    def fake_complete_once(self, client, params, **kwargs):
        seen.clear()
        seen.update(params)
        return "Play Forest."

    monkeypatch.setattr(ProxyBackend, "_complete_once", fake_complete_once, raising=True)
    return seen


def _run(thinking: bool, model: str = "glm-5.3-flash") -> None:
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


@pytest.mark.parametrize("structured", [False, True])
def test_text_json_format_is_opt_in_and_keeps_the_requested_output_budget(captured, structured):
    backend = ProxyBackend(base_url="http://127.0.0.1:9/v1", api_key="test")
    kwargs = {"response_format": {"type": "json_object"}} if structured else {}
    backend.complete("Analyze this deck as JSON.", "Card rules", max_tokens=12288, **kwargs)
    assert captured["max_tokens"] == 12288
    if structured:
        assert captured["response_format"] == {"type": "json_object"}
    else:
        assert "response_format" not in captured


@pytest.mark.parametrize("thinking", [False, True])
def test_thinking_is_always_sent_explicitly_and_only_as_a_template_kwarg(captured, thinking):
    """Neither path may depend on the server default. The old Ollama ``think``
    flag is never sent: ``think:false`` would contradict thinking=true on GLM."""
    _run(thinking=thinking)
    extra = captured.get("extra_body") or {}
    assert extra["chat_template_kwargs"]["thinking"] is True
    assert set(extra) == {"chat_template_kwargs"}


@pytest.mark.parametrize("json_mode", [False, True])
def test_glm_vision_uses_low_effort_separated_reasoning(json_mode):
    backend = ProxyBackend(model="glm-5.3-flash", base_url="http://127.0.0.1:9/v1", api_key="test")
    client = Mock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"kind":"wait"}'))]
    )
    backend._client = client
    assert backend.complete_with_image("system", "user", b"image", json_mode=json_mode) == '{"kind":"wait"}'
    params = client.chat.completions.create.call_args.kwargs
    assert params["extra_body"]["chat_template_kwargs"] == {"thinking": True, "reasoning_effort": "low"}
    if json_mode:
        assert params["response_format"] == {"type": "json_object"}
    else:
        assert "response_format" not in params


@pytest.mark.parametrize(
    "saved_model", [None, "dsv4", "deepseek-v4-flash", "gemma-4-12b-it", "glm-5.3-flash"]
)
def test_hosted_model_is_current_regardless_of_saved_alias(saved_model):
    backend = ProxyBackend.create_online(model=saved_model, license_key="test-key")
    assert backend.model == "current"


def test_per_request_reasoning_override_does_not_change_the_backend_default(captured):
    backend = ProxyBackend(base_url="http://127.0.0.1:9/v1", api_key="test", enable_thinking=False)
    backend.complete("Analyze", "Deck", enable_thinking=True)
    assert "reasoning_effort" not in captured["extra_body"]["chat_template_kwargs"]
    assert backend.enable_thinking is False
    backend.complete("Choose", "Board")
    assert captured["extra_body"]["chat_template_kwargs"] == {"thinking": False, "reasoning_effort": "none"}


def test_per_request_reasoning_effort_overrides_the_default(captured):
    """The background game plan asks for a bounded effort (unrestricted ran past 45 s)."""
    be = ProxyBackend(
        model="glm-5.3-flash", enable_thinking=True, base_url="http://127.0.0.1:9/v1", api_key="sk-test"
    )
    be.complete("sys", "user", max_tokens=256, reasoning_effort="low")
    ctk = (captured.get("extra_body") or {})["chat_template_kwargs"]
    assert ctk == {"thinking": True, "reasoning_effort": "low"}


@pytest.mark.parametrize("model", ["current", "deepseek-v4.1-flash-local"])
@pytest.mark.parametrize("call_class", ["decision.typed", "decision.fast", "background.commentary", "probe"])
def test_current_deadline_sensitive_calls_disable_reasoning(captured, model, call_class):
    backend = ProxyBackend(model=model, base_url="http://127.0.0.1:9/v1", api_key="test")
    backend.complete("Choose", "Board", call_class=call_class, reasoning_effort="low")
    assert captured["extra_body"]["chat_template_kwargs"] == {"thinking": False, "reasoning_effort": "none"}
    assert "priority" not in captured["extra_body"]


def test_current_game_plan_bounds_reasoning_without_changing_defaults(captured):
    backend = ProxyBackend(base_url="http://127.0.0.1:9/v1", api_key="test")
    backend.complete("Plan", "Board", call_class="background.game_plan", reasoning_effort="high")
    assert captured["extra_body"]["chat_template_kwargs"] == {"thinking": True, "reasoning_effort": "low"}
    assert backend.enable_thinking is False


@pytest.mark.parametrize(
    "model,capability,expected",
    [("current", None, False), ("current", True, True), ("glm-5.3-flash", False, False)],
)
def test_priority_requires_endpoint_capability_for_moving_alias(model, capability, expected):
    assert ProxyBackend(model=model, supports_priority=capability)._priority_supported() is expected
