"""LLM backend factory and configuration helpers for MTGA Coach."""

from __future__ import annotations

import json
import logging
from typing import Any

from arenamcp.backends import LLMBackend, ProxyBackend

logger = logging.getLogger(__name__)


def _is_local_backend(be: Any) -> bool:
    """True when `be` is a ProxyBackend pointed at a local LLM server.

    Detects vLLM (port 8000), Ollama (11434), LM Studio (1234), and the
    legacy api_key markers we wrote out before the vLLM migration. Used to
    pick the longer LLM timeouts that local inference needs.
    """
    if not isinstance(be, ProxyBackend):
        return False
    url = (getattr(be, "_base_url", "") or "").lower()
    if any(host in url for host in ("localhost", "127.0.0.1", "0.0.0.0")):
        return True
    key = (getattr(be, "_api_key", "") or "").lower()
    return key in ("vllm", "ollama", "lm-studio")


def get_available_modes() -> list[tuple[str, str]]:
    """Return available backend modes.

    Returns list of ``(display_name, mode_id)`` tuples.
    Only online mode is available.
    """
    return [
        ("Online", "online"),
    ]


def get_models_for_mode(mode: str) -> list[tuple[str, str | None]]:
    """Return models available for the given mode.

    Returns list of ``(display_name, model_id_or_None)`` tuples.
    ``None`` means "use the mode's default model".

    Queries the endpoint's /v1/models dynamically and falls back to
    a sensible default.
    """
    import urllib.request as _urlreq

    mode = mode.lower()

    if mode == "online":
        try:
            from arenamcp.backends.proxy import ONLINE_BASE_URL
            from arenamcp.settings import get_settings

            license_key = get_settings().get("license_key", "")
            headers = {"User-Agent": "mtgacoach-client/1.0"}
            if license_key:
                headers["Authorization"] = f"Bearer {license_key}"
            req = _urlreq.Request(f"{ONLINE_BASE_URL}/models", headers=headers)
            with _urlreq.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
            models: list[tuple[str, str | None]] = []
            for m in data.get("data", []):
                mid = m["id"]
                models.append((mid, mid))
            if models:
                return models
        except Exception:
            pass
        return [("Default", None)]

    if mode == "local":
        try:
            from arenamcp.settings import get_settings

            local_url = get_settings().get("local_url") or "http://localhost:8000/v1"
        except Exception:
            local_url = "http://localhost:8000/v1"
        # Try OpenAI-compatible /v1/models
        try:
            req = _urlreq.Request(f"{local_url}/models")
            with _urlreq.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
            models = [(m["id"], m["id"]) for m in data.get("data", []) if m.get("id")]
            if models:
                return models
        except Exception:
            pass
        # Try Ollama-specific /api/tags
        if "11434" in local_url:
            try:
                req = _urlreq.Request("http://localhost:11434/api/tags")
                with _urlreq.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read())
                models = [(m["name"], m["name"]) for m in data.get("models", []) if m.get("name")]
                if models:
                    return models
            except Exception:
                pass
        return [("llama3.2", "llama3.2")]

    return [("Default", None)]


THINKING_MODEL_PREFERENCE = [
    "deepseek-v4-flash",
    "claude-opus-4-6",
    "claude-sonnet-4-5-20250929",
    "gemini-2.5-pro",
    "gpt-5.3-codex",
]


def pick_thinking_model() -> str | None:
    """Auto-select the best available thinking model.

    In online mode, queries the mtgacoach.com /v1/models endpoint.
    Returns the first match from THINKING_MODEL_PREFERENCE, or None.
    """
    import urllib.request

    try:
        from arenamcp.backends.proxy import ONLINE_BASE_URL
        from arenamcp.settings import get_settings

        s = get_settings()
        license_key = s.get("license_key", "")
        if not license_key or s.get("mode") != "online":
            return None

        req = urllib.request.Request(
            f"{ONLINE_BASE_URL}/models",
            headers={
                "Authorization": f"Bearer {license_key}",
                "User-Agent": "mtgacoach-client/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())

        available_ids = {m["id"] for m in data.get("data", [])}
        for model_id in THINKING_MODEL_PREFERENCE:
            if model_id in available_ids:
                logger.info(f"Thinking model selected: {model_id}")
                return model_id

        logger.info(f"No preferred thinking model found among {len(available_ids)} models")
        return None
    except Exception as e:
        logger.warning(f"Could not pick thinking model: {e}")
        return None


def create_backend(
    mode: str,
    model: str | None = None,
    progress_callback: Any | None = None,
) -> LLMBackend:
    """Factory function to create LLM backends by mode.

    Args:
        mode: "online" or "local" (or "auto" for auto-detection)
        model: Optional model override (uses mode default if not specified)
        progress_callback: Optional callback(status: str) for real-time subtask updates

    Returns:
        Configured LLMBackend instance

    Raises:
        ValueError: If mode is not recognized
    """
    mode = mode.lower()

    if mode == "auto":
        from arenamcp.backend_detect import auto_select_mode

        auto_mode, auto_model = auto_select_mode()
        logger.info(f"Auto-selected mode: {auto_mode} (model={auto_model})")
        return create_backend(
            auto_mode,
            model=model or auto_model,
            progress_callback=progress_callback,
        )

    if mode == "online":
        from arenamcp.settings import get_settings

        license_key = get_settings().get("license_key", "")
        return ProxyBackend.create_online(model=model, license_key=license_key)

    if mode == "local":
        from arenamcp.settings import get_settings

        s = get_settings()
        local_url = s.get("local_url") or "http://localhost:8000/v1"
        local_api_key = s.get("local_api_key") or "vllm"
        local_model = model or s.get("local_model")

        # If no model specified, try to auto-detect from the endpoint
        if not local_model:
            try:
                import urllib.request as _urlreq

                req = _urlreq.Request(f"{local_url}/models")
                with _urlreq.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read())
                models_list = [m["id"] for m in data.get("data", []) if m.get("id")]
                if models_list:
                    local_model = models_list[0]
            except Exception:
                pass

        return ProxyBackend.create_local(
            model=local_model,
            url=local_url,
            api_key=local_api_key,
        )

    raise ValueError(f"Unknown mode: {mode}. Use 'auto', 'online', or 'local'.")


def create_local_fallback(
    model: str | None = None,
    progress_callback: Any | None = None,
) -> ProxyBackend:
    """Create a local backend as a fallback when online mode fails."""
    from arenamcp.backend_detect import DEFAULT_LOCAL_MODEL

    try:
        from arenamcp.settings import get_settings

        s = get_settings()
        local_url = s.get("local_url") or "http://localhost:8000/v1"
        local_api_key = s.get("local_api_key") or "vllm"
    except Exception:
        local_url = "http://localhost:8000/v1"
        local_api_key = "vllm"
    return ProxyBackend.create_local(
        model=model or DEFAULT_LOCAL_MODEL,
        url=local_url,
        api_key=local_api_key,
    )


# Words that tend to be overused by LLMs in coaching contexts
OVERUSE_CANDIDATES = {
    "consider",
    "considering",
    "important",
    "crucial",
    "critical",
    "definitely",
    "absolutely",
    "certainly",
    "essentially",
    "basically",
    "potentially",
    "priority",
    "prioritize",
    "focus",
    "key",
}

# Threshold for blacklisting (uses in window)
OVERUSE_THRESHOLD = 3
OVERUSE_WINDOW_SECONDS = 120
