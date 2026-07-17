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

from .client_metadata import get_client_headers

logger = logging.getLogger(__name__)

# API base URL for subscription validation
API_BASE = "https://api.mtgacoach.com"

# Website base URL (trial provisioning, signup)
WEBSITE_BASE = "https://mtgacoach.com"

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
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {license_key}",
        }
        headers.update(get_client_headers())
        req = urllib.request.Request(
            f"{API_BASE}/v1/subscription/check",
            data=payload,
            headers=headers,
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
        headers = {
            "Authorization": f"Bearer {license_key}",
        }
        headers.update(get_client_headers())
        req = urllib.request.Request(
            f"{API_BASE}/v1/subscription/messages",
            headers=headers,
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


# ── Free trial provisioning ──────────────────────────────────────────────
#
# First-run doctrine: the app must work with NO key. The client asks the
# website for a 7-day trial key tied to a stable machine hash; when the
# trial lapses the gateway rejects the key and the repair surface points
# the user at Patreon. Server side: website/app.py POST /api/trial.


def get_machine_id() -> str:
    """Stable, anonymous machine hash for trial bookkeeping.

    sha256 of the primary MAC + hostname — not reversible, not hardware
    serial, stable across launches. Spoofable, which is acceptable: trial
    keys carry a hard budget cap on the gateway side.
    """
    import hashlib
    import platform
    import uuid

    raw = f"{uuid.getnode()}:{platform.node()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def request_trial_key(timeout: int = 10) -> dict:
    """Ask the website for a trial license key for this machine.

    Returns a dict with:
      status: "created" | "existing" | "trial_expired" | "offline" | "error"
      key, expires_at: present for created/existing
      message: human-readable detail for the repair surface
    """
    from arenamcp import __version__

    body = json.dumps(
        {"machine_id": get_machine_id(), "app_version": __version__}
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    headers.update(get_client_headers())
    req = urllib.request.Request(
        f"{WEBSITE_BASE}/api/trial", data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        key = (data.get("key") or "").strip()
        if not key:
            return {"status": "error", "message": "Trial endpoint returned no key."}
        return {
            "status": data.get("status", "created"),
            "key": key,
            "expires_at": data.get("expires_at", ""),
            "message": "",
        }
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return {
                "status": "trial_expired",
                "message": (
                    "Your free trial has ended. Subscribe at "
                    f"{SUBSCRIBE_URL} to keep coaching."
                ),
            }
        if e.code == 404:
            # Endpoint not deployed yet — behave like offline, not like a bug.
            return {"status": "offline", "message": "Trial service unavailable."}
        return {"status": "error", "message": f"Trial request failed (HTTP {e.code})."}
    except Exception as e:
        return {"status": "offline", "message": f"Could not reach {WEBSITE_BASE} ({e})."}


def ensure_license_key() -> dict:
    """Auto-provision a trial key when no license key is configured.

    Returns the request_trial_key() result dict, plus status "existing_key"
    when a key is already configured (nothing to do). On success the key and
    trial expiry are persisted to settings.
    """
    from arenamcp.settings import get_settings

    settings = get_settings()
    if (settings.get("license_key") or "").strip():
        return {"status": "existing_key", "message": ""}

    result = request_trial_key()
    if result.get("key"):
        settings.set("license_key", result["key"], save=False)
        settings.set("trial_expires_at", result.get("expires_at", ""), save=True)
        logger.info(
            "Provisioned %s trial key (expires %s)",
            result.get("status"), result.get("expires_at"),
        )
    return result
