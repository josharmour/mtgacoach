"""Client metadata headers for mtgacoach network requests."""

from __future__ import annotations

import os
from typing import Optional

from .settings import get_settings


def _current_version() -> str:
    from arenamcp import __version__

    return str(__version__).strip() or "unknown"


def _normalize_frontend(value: Optional[str]) -> str:
    frontend = str(value or "").strip().lower()
    if frontend in {"winui", "pyside", "tui", "standalone"}:
        return frontend
    return "unknown"


def get_client_metadata() -> dict[str, str]:
    """Return normalized client metadata for proxy/API requests."""
    settings = get_settings()
    version = _current_version()
    frontend = _normalize_frontend(os.environ.get("MTGACOACH_FRONTEND"))
    install_id = str(settings.get("install_id", "") or "").strip()

    return {
        "version": version,
        "frontend": frontend,
        "install_id": install_id,
        "user_agent": f"mtgacoach/{version} ({frontend})",
    }


def get_client_headers() -> dict[str, str]:
    """Return HTTP headers describing the current client install."""
    metadata = get_client_metadata()
    headers = {
        "User-Agent": metadata["user_agent"],
        "X-MTGACoach-Version": metadata["version"],
        "X-MTGACoach-Frontend": metadata["frontend"],
    }
    if metadata["install_id"]:
        headers["X-MTGACoach-Install-ID"] = metadata["install_id"]
    return headers
