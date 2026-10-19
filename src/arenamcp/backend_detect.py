"""Online-backend validation and error classification.

The app is online-only (local mode was removed 2026-06-11): ``validate_backend``
checks the license key against the subscription service before a manual
reconnect, and ``is_query_failure_retriable`` spots billing/auth failures.
"""

import logging

logger = logging.getLogger(__name__)


def validate_backend(mode: str) -> tuple[bool, str]:
    """Validate that a backend mode can actually serve requests.

    Returns:
        (is_working, error_message) — error_message is empty on success.
    """
    mode = mode.lower()

    if mode == "online":
        return _validate_online()
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
