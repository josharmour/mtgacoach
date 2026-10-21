"""Subscription validation for mtgacoach.com online backend.

Checks license keys against the mtgacoach.com API, caches results
locally, and retrieves service messages for subscribers.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path

from .client_metadata import get_client_headers

logger = logging.getLogger(__name__)

# API base URL for subscription validation
API_BASE = "https://api.mtgacoach.com"

# Website base URL (trial provisioning, signup)
WEBSITE_BASE = "https://mtgacoach.com"

# Subscription check + service messages are served by the website app. api.mtgacoach.com is the
# LiteLLM gateway itself and 404s these routes, which left every status "unknown".
SUBSCRIPTION_BASE = WEBSITE_BASE

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
        expires_at: str | float | None = None,
        messages: list[dict] | None = None,
        kind: str = "",
        name: str = "",
    ):
        self.status = status
        self.message = message
        self.expires_at = expires_at
        self.messages = messages or []
        self.kind = kind  # "subscriber" | "trial" | "" (older servers)
        self.name = name

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
            f"{SUBSCRIPTION_BASE}/v1/subscription/check",
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
            kind=data.get("kind", ""),
            name=data.get("name", ""),
        )
        _save_cache(license_key, result)
        return result

    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read() or b"{}")
        except Exception:
            data = {}
        if e.code == 401:
            result = SubscriptionStatus(
                status=SubscriptionStatus.INVALID,
                message="Invalid license key.",
            )
        elif e.code == 402:
            result = SubscriptionStatus(
                status=SubscriptionStatus.EXPIRED,
                message=data.get("message") or "Subscription expired. Renew at mtgacoach.com/subscribe",
                expires_at=data.get("expires_at"),
                kind=data.get("kind", ""),
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
            f"{SUBSCRIPTION_BASE}/v1/subscription/messages",
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


def _to_epoch(value) -> float | None:
    """expires_at arrives as epoch seconds (subscription check) or ISO8601 (trial provisioning)."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


def _remaining(seconds: float) -> str:
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return f"{days}d {hours}h left"
    if hours:
        return f"{hours}h {rest // 60}m left"
    return f"{max(rest // 60, 1)}m left"


def describe_access(
    license_key: str,
    status: SubscriptionStatus | None,
    trial_expires_at=None,
    now: float | None = None,
) -> tuple[str, str, str, bool]:
    """Badge for the user's access level: (text, tone, tooltip, needs_subscription).

    tone is a theme tone name ("good", "warn", "bad", "muted"). Trial countdowns come from the
    server's expires_at when known, else the expiry saved when the trial key was provisioned.
    """
    now = time.time() if now is None else now
    if not license_key:
        return "No key", "warn", "No license key yet — open Setup & Repair to start a free trial.", True
    st = status.status if status else SubscriptionStatus.UNKNOWN
    kind = status.kind if status else ""
    is_trial = kind == "trial" or st == SubscriptionStatus.TRIAL or (not kind and bool(trial_expires_at))
    expires = _to_epoch(status.expires_at if status and status.expires_at else None) or _to_epoch(
        trial_expires_at
    )

    if is_trial:
        if st in (SubscriptionStatus.EXPIRED, SubscriptionStatus.INVALID) or (expires and expires <= now):
            return (
                "Trial ended — Subscribe",
                "bad",
                f"Your 7-day free trial has ended. Subscribe at {SUBSCRIBE_URL} and paste your key in Setup & Repair.",
                True,
            )
        if expires:
            tone = "warn" if expires - now < 86400 else "good"
            return (
                f"Trial · {_remaining(expires - now)}",
                tone,
                f"Free trial — ends {time.strftime('%b %d %H:%M', time.localtime(expires))}. "
                f"Click to subscribe on Patreon.",
                False,
            )
        return "Trial", "good", "Free trial. Click to subscribe on Patreon.", False
    if st == SubscriptionStatus.ACTIVE:
        who = f" — {status.name}" if status and status.name else ""
        return "★ Subscriber", "good", f"mtgacoach subscriber{who}. Thank you!", False
    if st == SubscriptionStatus.EXPIRED:
        return "Subscription expired", "bad", status.message or f"Renew at {SUBSCRIBE_URL}", True
    if st == SubscriptionStatus.INVALID:
        return "Key invalid", "bad", "This license key isn't recognised. Fix it in Setup & Repair.", True
    return (
        "Status unknown",
        "muted",
        (
            status.message
            if status and status.message
            else "Couldn't reach mtgacoach.com to check your subscription."
        ),
        False,
    )


def _load_cache(license_key: str, ignore_ttl: bool = False) -> SubscriptionStatus | None:
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
            kind=data.get("kind", ""),
            name=data.get("name", ""),
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
            "kind": result.kind,
            "name": result.name,
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

    body = json.dumps({"machine_id": get_machine_id(), "app_version": __version__}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    headers.update(get_client_headers())
    req = urllib.request.Request(f"{WEBSITE_BASE}/api/trial", data=body, headers=headers, method="POST")
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
                "message": (f"Your free trial has ended. Subscribe at {SUBSCRIBE_URL} to keep coaching."),
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
            result.get("status"),
            result.get("expires_at"),
        )
    return result
