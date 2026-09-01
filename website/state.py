"""Shared state and context for mtgacoach website/proxy server."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import secrets
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml
from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates
from providers import ProviderRouter

# Logging: console + file
LOG_DIR = Path("./data/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            LOG_DIR / "proxy.log",
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
        ),
    ],
)
logger = logging.getLogger(__name__)

config: dict[str, Any] = {}
admin_password: str = ""
router: ProviderRouter = ProviderRouter()
templates = Jinja2Templates(directory="templates")
http_client: Optional[httpx.AsyncClient] = None
_RESPONSE_STORE_MAX = 512
_response_store: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


def _get_db():
    import db
    return db


def _resolved_provider_configs() -> list[dict]:
    """Layer SQLite overrides on top of yaml provider list."""
    _db = _get_db()
    raw = _db.get_config_value("providers")
    if raw:
        try:
            return json.loads(raw) or []
        except (json.JSONDecodeError, TypeError):
            logger.warning("proxy_config.providers is not valid JSON; ignoring override")
    return list(config.get("providers", []))


def _resolved_default_model() -> str:
    """Default model from SQLite override or yaml fallback."""
    _db = _get_db()
    return (
        _db.get_config_value("default_model")
        or config.get("default_model", "")
        or ""
    )


def _reload_providers() -> None:
    """Re-init the global ProviderRouter from current yaml + SQLite state."""
    router.load_from_config(_resolved_provider_configs())
    logger.info(f"Provider router reloaded ({len(router.providers)} active)")


def init_app_state() -> None:
    """Initialize or reset state when loading the proxy app."""
    global config, admin_password, router
    CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config.yaml"))
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    admin_password = config.get("admin", {}).get("password", "")
    if admin_password.startswith("${") and admin_password.endswith("}"):
        admin_password = os.environ.get(admin_password[2:-1], "")
    if not admin_password:
        raise RuntimeError(
            "ADMIN_PASSWORD not configured. "
            "Set 'admin.password' in config.yaml or export the referenced env var."
        )

    _db = _get_db()
    _db.init_db()
    router = ProviderRouter()
    router.load_from_config(_resolved_provider_configs())
    _response_store.clear()


# Initial load
init_app_state()


def _store_response(
    response_id: str,
    response: dict[str, Any],
    conversation_messages: list[dict[str, Any]],
) -> None:
    _response_store[response_id] = {
        "response": response,
        "conversation_messages": conversation_messages,
        "stored_at": time.time(),
    }
    _response_store.move_to_end(response_id)
    while len(_response_store) > _RESPONSE_STORE_MAX:
        _response_store.popitem(last=False)


def _get_stored_response(response_id: str) -> Optional[dict[str, Any]]:
    item = _response_store.get(response_id)
    if item:
        _response_store.move_to_end(response_id)
    return item


# --- Auth helpers ---

def _extract_license_key(request: Request) -> str:
    """Extract license key from Authorization header."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return ""


def _extract_client_metadata(request: Request) -> dict[str, str]:
    """Extract optional client telemetry headers from a request."""
    install_id = (request.headers.get("X-MTGACoach-Install-ID") or "").strip()[:128]
    version = (request.headers.get("X-MTGACoach-Version") or "").strip()[:64]
    frontend = (request.headers.get("X-MTGACoach-Frontend") or "").strip().lower()[:32]
    user_agent = (request.headers.get("User-Agent") or "").strip()[:256]
    if frontend not in {"winui", "pyside", "tui", "standalone", "unknown", ""}:
        frontend = "unknown"
    return {
        "install_id": install_id,
        "client_version": version,
        "frontend": frontend,
        "user_agent": user_agent,
    }


def _record_client_telemetry(request: Request, license_key: str) -> None:
    """Persist client telemetry for a validated subscriber request."""
    metadata = _extract_client_metadata(request)
    install_id = metadata.get("install_id", "")
    if not install_id:
        return
    _db = _get_db()
    _db.upsert_client_install(
        license_key=license_key,
        install_id=install_id,
        client_version=metadata.get("client_version", ""),
        frontend=metadata.get("frontend", ""),
        user_agent=metadata.get("user_agent", ""),
        last_ip=request.client.host if request.client else "",
    )


def _require_license(request: Request) -> dict:
    """Validate license key and return subscriber info."""
    key = _extract_license_key(request)
    if not key:
        raise HTTPException(401, "Missing license key")

    _db = _get_db()
    sub = _db.check_license(key)
    if not sub:
        raise HTTPException(401, "Invalid license key")

    if sub["status"] not in ("active", "trial"):
        raise HTTPException(402, f"Subscription {sub['status']}. Renew at mtgacoach.com/subscribe")

    _record_client_telemetry(request, sub["license_key"])
    return sub


def _require_admin(request: Request):
    """Check admin credentials via Basic auth or X-Admin-Key header."""
    admin_key = request.headers.get("X-Admin-Key", "")
    if admin_key and admin_key == admin_password:
        return True

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        import base64
        decoded = base64.b64decode(auth[6:]).decode()
        username, _, password = decoded.partition(":")
        admin_user = config.get("admin", {}).get("username", "admin")
        if username == admin_user and password == admin_password:
            return True

    raise HTTPException(403, "Admin access required")
