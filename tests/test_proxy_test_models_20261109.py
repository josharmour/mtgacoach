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
