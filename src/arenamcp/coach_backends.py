"""LLM backend factory and configuration helpers for MTGA Coach."""

from __future__ import annotations

import json
import logging
from typing import Any

from arenamcp.backends import LLMBackend, ProxyBackend

logger = logging.getLogger(__name__)


def get_models_for_mode(mode: str) -> list[tuple[str, str | None]]:
    """Return the gateway models a developer machine can cycle through.

    Returns list of ``(display_name, model_id_or_None)`` tuples.
    ``None`` means "use the server-assigned default model".

    Only "online" exists (local mode was removed 2026-06-11): it queries
    api.mtgacoach.com's /v1/models and uses the stable current alias when discovery is temporarily unavailable.
    """
    if mode.lower() == "online":
        from arenamcp.backends.proxy import resolve_online_base_url
        from arenamcp.client_metadata import get_client_headers
        from arenamcp.gateway_models import current_models
        from arenamcp.settings import get_settings

        return current_models(
            resolve_online_base_url(), get_settings().get("license_key", ""), get_client_headers()
        )
    return [("Default", None)]


THINKING_MODEL_PREFERENCE = ["current"]


def pick_thinking_model() -> str | None:
    """Auto-select the best available thinking model.

    In online mode, queries the mtgacoach.com /v1/models endpoint.
    Returns the first match from THINKING_MODEL_PREFERENCE, or None.
    """
    import urllib.request

    try:
        from arenamcp.backends.proxy import resolve_online_base_url
        from arenamcp.settings import get_settings

        s = get_settings()
        license_key = s.get("license_key", "")
        if not license_key or s.get("mode") != "online":
            return None

        req = urllib.request.Request(
            f"{resolve_online_base_url()}/models",
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
    """Create the LLM backend. The app is online-only: api.mtgacoach.com with the license key.

    Args:
        mode: "online" (local and auto modes were removed)
        model: Optional model override; the gateway pins current
        progress_callback: Unused; kept for call compatibility

    Raises:
        ValueError: If mode is not "online"
    """
    mode = mode.lower()
    if mode != "online":
        raise ValueError(f"Unknown mode: {mode}. The app is online-only; use 'online'.")

    from arenamcp.settings import get_settings

    license_key = get_settings().get("license_key", "")
    return ProxyBackend.create_online(model=model, license_key=license_key)


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
