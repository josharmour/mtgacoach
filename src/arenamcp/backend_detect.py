"""Backend detection for the two-mode architecture (online / local).

Used by the TUI/standalone to detect available backends on launch,
validate connectivity, and pick the best default mode.
"""

import json
import logging
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

# Default local model (vLLM serves Gemma 4 E2B-it under this alias)
DEFAULT_LOCAL_MODEL = "gemma4:e2b"


def detect_backends_quick() -> dict[str, bool]:
    """Check which backend modes are available.

    Returns a dict of mode_name -> is_available.
    HTTP checks use a 2-second timeout so this never blocks for long.
    """
    results: dict[str, bool] = {}

    # Online: check if mtgacoach.com API is reachable and we have a license key
    results["online"] = _is_online_available()

    # Local: disabled for now
    results["local"] = False

    return results


def _is_online_available() -> bool:
    """Check if online mode is available (has license key + API reachable)."""
    try:
        from arenamcp.settings import get_settings
        license_key = get_settings().get("license_key", "")
        if not license_key:
            return False
    except Exception:
        return False

    try:
        from arenamcp.backends.proxy import ONLINE_BASE_URL
        req = urllib.request.Request(f"{ONLINE_BASE_URL}/models", method="GET",
                                     headers={"Authorization": f"Bearer {license_key}",
                                              "User-Agent": "mtgacoach-client/1.0"})
        with urllib.request.urlopen(req, timeout=2):
            return True
    except Exception as e:
        logger.debug(f"Online API check failed: {e}")
        # If we have a cached valid subscription, consider online available
        # even if the API is momentarily unreachable
        try:
            from arenamcp.subscription import check_subscription
            status = check_subscription(license_key)
            return status.is_valid
        except Exception:
            return False


def _is_local_available() -> bool:
    """Check if the configured local endpoint responds (disabled)."""
    return False


def validate_backend(mode: str) -> tuple[bool, str]:
    """Validate that a backend mode can actually serve requests.

    Returns:
        (is_working, error_message) — error_message is empty on success.
    """
    mode = mode.lower()

    if mode == "online":
        return _validate_online()
    elif mode == "local":
        return False, "Local mode is disabled."
    else:
        return False, f"Unknown mode: {mode}. Use 'online'."


def _validate_online() -> tuple[bool, str]:
    """Check online mode: license key valid + API reachable."""
    try:
        from arenamcp.settings import get_settings
        license_key = get_settings().get("license_key", "")
    except Exception:
        return False, "Could not load settings"

    if not license_key:
        return False, "No license key configured. Use /subscribe to get one."

    try:
        from arenamcp.subscription import check_subscription
        status = check_subscription(license_key)
        if status.is_valid:
            return True, ""
        return False, status.message or "Subscription not active."
    except Exception as e:
        return False, f"Subscription check failed: {e}"


def _validate_local() -> tuple[bool, str]:
    """Check local mode: endpoint reachable with at least one model."""
    try:
        from arenamcp.settings import get_settings
        local_url = get_settings().get("local_url") or "http://localhost:8000/v1"
    except Exception:
        local_url = "http://localhost:8000/v1"

    try:
        req = urllib.request.Request(f"{local_url}/models", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            models = data.get("data", [])
            if models:
                return True, ""
            return False, f"Endpoint at {local_url} has no models loaded"
    except Exception as e:
        logger.debug(f"Local validation failed: {e}")
        # Check if it's an Ollama endpoint by trying /api/tags
        if "11434" in local_url:
            try:
                req = urllib.request.Request("http://localhost:11434/api/tags", method="GET")
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read())
                    models = data.get("models", [])
                    if models:
                        return True, ""
                    return False, "Ollama is running but has no models. Run: ollama pull llama3.2"
            except Exception:
                pass
        return False, f"Local endpoint not reachable at {local_url}. Is Ollama/LM Studio running?"


def auto_select_mode() -> tuple[str, Optional[str]]:
    """Auto-select the best available mode.

    Returns:
        (mode, model_or_none)
    """
    return "online", None


def is_query_failure_retriable(error_text: str) -> bool:
    """Check whether an error message indicates a billing/quota/auth failure.

    These errors mean the backend won't recover on retry — the user should
    switch modes or check their subscription.
    """
    error_lower = error_text.lower()
    failure_indicators = [
        "insufficient",
        "billing",
        "quota",
        "rate limit",
        "rate_limit",
        "expired",
        "unauthorized",
        "403",
        "401",
        "429",
        "payment required",
        "credit",
        "subscription",
        "not logged in",
        "auth",
        "permission denied",
        "api key",
        "apikey",
        "invalid_api_key",
        "account",
        "base url",
        "invalid url",
        "unknown url type",
        "url type",
    ]
    return any(indicator in error_lower for indicator in failure_indicators)
