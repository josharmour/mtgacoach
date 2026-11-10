"""Outside test models (gateway names starting "or-") are sent OpenRouter's reasoning field."""

from arenamcp.backends.proxy import ProxyBackend


def _params(model, *, thinking_enabled, reasoning_effort):
    backend = ProxyBackend.__new__(ProxyBackend)
    backend.model = model
    return backend._build_params(
        "system",
        "user",
        max_tokens=256,
        temperature=0.0,
        response_format=None,
        thinking_enabled=thinking_enabled,
        reasoning_effort=reasoning_effort,
    )


def test_thinking_off_disables_reasoning_for_a_test_model():
    params = _params("or-gemma-4-26b", thinking_enabled=False, reasoning_effort="none")
    assert params["extra_body"] == {"reasoning": {"enabled": False}}
    assert params["model"] == "or-gemma-4-26b"


def test_effort_is_mapped_for_a_test_model():
    assert _params("or-qwen3.8-27b", thinking_enabled=True, reasoning_effort="low")["extra_body"] == {
        "reasoning": {"effort": "low"}
    }
    assert _params("or-qwen3.8-27b", thinking_enabled=True, reasoning_effort="max")["extra_body"] == {
        "reasoning": {"effort": "high"}
    }


def test_hosted_glm_test_model_is_not_treated_as_the_local_glm():
    params = _params("or-glm-5.3-flash", thinking_enabled=False, reasoning_effort="none")
    assert "chat_template_kwargs" not in params["extra_body"]
    # Its endpoint rejects reasoning switched off; the lowest effort is sent.
    assert params["extra_body"] == {"reasoning": {"effort": "low"}}
    backend = ProxyBackend.__new__(ProxyBackend)
    backend.model = "or-glm-5.3-flash"
    assert backend._priority_supported() is False
    backend.model = "glm-5.3-flash"
    assert backend._priority_supported() is True


def test_served_models_keep_their_parameters():
    assert _params("current", thinking_enabled=False, reasoning_effort="none")["extra_body"] == {
        "chat_template_kwargs": {"thinking": False, "reasoning_effort": "none"}
    }
    assert _params("glm-5.3-flash", thinking_enabled=True, reasoning_effort="low")["extra_body"] == {
        "chat_template_kwargs": {"thinking": True, "reasoning_effort": "low"}
    }


def test_model_switch_is_announced_in_words():
    from arenamcp.pipe_adapter import _spoken_model_name

    assert _spoken_model_name("or-gemma-4-26b") == "Model: OpenRouter gemma 4 26b"
    assert _spoken_model_name("current") == "Model: current"
    assert _spoken_model_name(None) == "Model: default"


def test_source_checkout_launch_counts_as_developer_mode(monkeypatch):
    from arenamcp import settings

    monkeypatch.delenv("MTGACOACH_DEV", raising=False)
    monkeypatch.delenv("MTGACOACH_DEV_CHECKOUT", raising=False)
    monkeypatch.setattr(settings, "get_settings", lambda: {})
    assert settings.is_developer_mode() is False
    monkeypatch.setenv("MTGACOACH_DEV_CHECKOUT", "/some/checkout")
    assert settings.is_developer_mode() is True


def test_switch_reaches_the_autopilot_backend_and_is_not_saved(monkeypatch):
    """2026-11-09 23:17: after a switch, decision.typed calls still went to "current"."""
    from types import SimpleNamespace

    from arenamcp.standalone import StandaloneCoach

    saved = {}
    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach.draft_mode = False
    coach._backend_name = "online"
    coach._model_name = "current"
    coach.settings = SimpleNamespace(set=lambda key, value, save=True: saved.__setitem__(key, value))
    coach.ui = SimpleNamespace(status=lambda *a: None, error=lambda *a: None)
    coach._coach = SimpleNamespace(_backend=SimpleNamespace(model="current", _turns=3))
    coach._autopilot_backend = SimpleNamespace(model="current")
    coach._backend_failed = False
    coach._consecutive_errors = 0
    coach.set_backend("online", "or-gemma-4-26b")
    assert coach._coach._backend.model == "or-gemma-4-26b"
    assert coach._autopilot_backend.model == "or-gemma-4-26b"
    assert coach.model_name == "or-gemma-4-26b"
    assert "model" not in saved
    coach.set_backend("online", "current")
    assert coach._autopilot_backend.model == "current" and saved["model"] == "current"


def test_effort_by_call_class_for_the_alias_and_test_models():
    from arenamcp.backends.proxy import _alias_effort

    assert _alias_effort("current", "decision.typed", "low") == "none"
    assert _alias_effort("current", "background.game_plan", "high") == "low"
    assert _alias_effort("or-gemma-4-26b", "decision.typed", "low") == "none"
    # A test model's plan call ran 17-43 s with thinking on (match eedf1ae3).
    assert _alias_effort("or-gemma-4-26b", "background.game_plan", "high") == "none"
    assert _alias_effort("glm-5.3-flash", "decision.typed", "low") == "low"
    assert _alias_effort("glm-5.3-flash", "background.game_plan", "high") == "high"
