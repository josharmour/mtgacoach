"""Subscription validation for mtgacoach.com online backend.

Checks license keys against the mtgacoach.com API, caches results
locally, and retrieves service messages for subscribers.
"""

import json
import logging
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# API base URL for subscription validation
API_BASE = "https://api.mtgacoach.com"

# Subscription page URL (opened in browser when user needs to subscribe)
SUBSCRIBE_URL = "https://mtgacoach.com/subscribe"

# Cache TTL: how long a subscription check result is trusted (24 hours)
CACHE_TTL_SECONDS = 86400

# Settings directory (shared with settings.py)
_SETTINGS_DIR = Path.home() / ".arenamcp"
_SUB_CACHE_FILE = _SETTINGS_DIR / "subscription_cache.json"


class SubscriptionStatus:
    """Result of a subscription check."""

    ACTIVE = "active"
    TRIAL = "trial"
    EXPIRED = "expired"
    INVALID = "invalid"
    UNKNOWN = "unknown"  # Could not reach server

    def __init__(
        self,
        status: str = "unknown",
        message: str = "",
        expires_at: Optional[str] = None,
        messages: Optional[list[dict]] = None,
    ):
        self.status = status
        self.message = message
        self.expires_at = expires_at
        self.messages = messages or []

    @property
    def is_valid(self) -> bool:
        return self.status in (self.ACTIVE, self.TRIAL)

    @property
    def needs_subscription(self) -> bool:
        return self.status in (self.EXPIRED, self.INVALID)

    def __repr__(self) -> str:
        return f"SubscriptionStatus(status={self.status!r}, message={self.message!r})"


def check_subscription(license_key: str, force: bool = False) -> SubscriptionStatus:
    """Validate a license key against the mtgacoach.com API.

    Results are cached locally for CACHE_TTL_SECONDS to avoid blocking
    every request. Use force=True to bypass cache.

    Args:
        license_key: The user's license key
        force: If True, bypass the local cache

    Returns:
        SubscriptionStatus with validation result
    """
    if not license_key:
        return SubscriptionStatus(
            status=SubscriptionStatus.INVALID,
            message="No license key configured. Visit mtgacoach.com/subscribe to get one.",
        )

    # Check cache first (unless forced)
    if not force:
        cached = _load_cache(license_key)
        if cached:
            return cached

    # Call the API
    try:
        payload = json.dumps({"license_key": license_key}).encode("utf-8")
        req = urllib.request.Request(
            f"{API_BASE}/v1/subscription/check",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {license_key}",
                "User-Agent": "mtgacoach-client/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        result = SubscriptionStatus(
            status=data.get("status", SubscriptionStatus.UNKNOWN),
            message=data.get("message", ""),
            expires_at=data.get("expires_at"),
            messages=data.get("messages", []),
        )
        _save_cache(license_key, result)
        return result

    except urllib.error.HTTPError as e:
        if e.code == 401:
            result = SubscriptionStatus(
                status=SubscriptionStatus.INVALID,
                message="Invalid license key.",
            )
        elif e.code == 402:
            result = SubscriptionStatus(
                status=SubscriptionStatus.EXPIRED,
                message="Subscription expired. Renew at mtgacoach.com/subscribe",
            )
        else:
            result = SubscriptionStatus(
                status=SubscriptionStatus.UNKNOWN,
                message=f"Server error ({e.code}). Online mode unavailable.",
            )
        _save_cache(license_key, result)
        return result

    except Exception as e:
        logger.warning(f"Subscription check failed: {e}")
        # On network failure, allow cached result even if expired
        cached = _load_cache(license_key, ignore_ttl=True)
        if cached and cached.is_valid:
            logger.info("Using expired subscription cache (server unreachable)")
            return cached
        return SubscriptionStatus(
            status=SubscriptionStatus.UNKNOWN,
            message="Could not reach mtgacoach.com. Check your internet connection.",
        )


def get_service_messages(license_key: str) -> list[dict]:
    """Fetch unread service messages for a subscriber.

    Messages are returned as part of the subscription check response.
    Each message has: id, title, body, created_at, priority.
    """
    if not license_key:
        return []

    try:
        req = urllib.request.Request(
            f"{API_BASE}/v1/subscription/messages",
            headers={
                "Authorization": f"Bearer {license_key}",
                "User-Agent": "mtgacoach-client/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        return data.get("messages", [])
    except Exception as e:
        logger.debug(f"Could not fetch service messages: {e}")
        return []


def open_subscribe_page() -> None:
    """Open the subscription page in the default browser."""
    import webbrowser
    webbrowser.open(SUBSCRIBE_URL)


def _load_cache(license_key: str, ignore_ttl: bool = False) -> Optional[SubscriptionStatus]:
    """Load cached subscription status if still valid."""
    if not _SUB_CACHE_FILE.exists():
        return None
    try:
        with open(_SUB_CACHE_FILE) as f:
            data = json.load(f)
        if data.get("license_key") != license_key:
            return None
        if not ignore_ttl:
            cached_at = data.get("checked_at", 0)
            if time.time() - cached_at > CACHE_TTL_SECONDS:
                return None
        return SubscriptionStatus(
            status=data.get("status", SubscriptionStatus.UNKNOWN),
            message=data.get("message", ""),
            expires_at=data.get("expires_at"),
        )
    except Exception:
        return None


def _save_cache(license_key: str, result: SubscriptionStatus) -> None:
    """Cache subscription check result to disk."""
    try:
        _SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "license_key": license_key,
            "status": result.status,
            "message": result.message,
            "expires_at": result.expires_at,
            "checked_at": time.time(),
        }
        with open(_SUB_CACHE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.debug(f"Could not cache subscription status: {e}")
