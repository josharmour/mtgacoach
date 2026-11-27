"""Client helper for submitting user-reviewed bug reports to mtgacoach.com -> Discord.

No background uploads: callers only invoke :func:`submit_bug_report` after the
user reviews the title, note, Discord username, and screenshot consent in the UI.
Diagnostics are scrubbed with :func:`arenamcp.match_bundle.redact` and gzipped
before upload; the server stores them privately rather than posting raw logs to
public Discord.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import json
import platform
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from arenamcp import __version__
from arenamcp.match_bundle import redact

BUG_REPORT_PATH = "/api/bug-reports"
BUG_REPORT_ME_PATH = "/api/bug-reports/me"
TIMEOUT_S = 20.0
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024
MAX_REPORT_GZ_BYTES = 4 * 1024 * 1024


def _website_base() -> str:
    try:
        from arenamcp.subscription import WEBSITE_BASE

        return str(WEBSITE_BASE).rstrip("/")
    except Exception:
        return "https://mtgacoach.com"


def _auth_headers(license_key: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {license_key.strip()}",
    }
    try:
        from arenamcp.client_metadata import get_client_headers

        headers.update(get_client_headers())
    except Exception:
        headers.setdefault("User-Agent", f"mtgacoach/{__version__}")
    return headers


def _default_opener(
    url: str,
    method: str,
    data: bytes | None,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, str]:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = ""
        with contextlib.suppress(Exception):
            body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), body


def load_local_report(report_path: str | Path | None) -> tuple[dict[str, Any], list[tuple[str, Path]]]:
    """Load a local bug report JSON dict and any valid screenshot paths."""
    if not report_path:
        return {}, []
    p = Path(report_path)
    if not p.is_file():
        return {}, []
    try:
        parsed = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}, []
    if not isinstance(parsed, dict):
        return {}, []
    shots: list[tuple[str, Path]] = []
    raw_shots = parsed.get("screenshots")
    if isinstance(raw_shots, dict):
        for label, spath in raw_shots.items():
            if not spath:
                continue
            sp = Path(str(spath))
            if sp.is_file():
                shots.append((str(label), sp))
    return parsed, shots[:2]


def prepare_report_gz(report_data: dict[str, Any]) -> str:
    """Redact and gzip-compress *report_data* into a base64 string."""
    scrubbed = redact(report_data if isinstance(report_data, dict) else {})
    if not isinstance(scrubbed, dict):
        scrubbed = {}
    encoded = json.dumps(scrubbed, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    gz = gzip.compress(encoded)
    if len(gz) > MAX_REPORT_GZ_BYTES:
        # Drop large log tails if a local report exceeded the upload cap
        trimmed = dict(scrubbed)
        for heavy_key in ("recent_logs", "log_tail", "player_log", "events"):
            trimmed.pop(heavy_key, None)
        trimmed["truncated_for_upload"] = True
        gz = gzip.compress(
            json.dumps(trimmed, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
        )
    return base64.b64encode(gz).decode("ascii")


def encode_screenshots(paths: list[tuple[str, Path]]) -> list[dict[str, str]]:
    """Read and base64-encode up to 2 valid PNG files."""
    out: list[dict[str, str]] = []
    for label, p in paths[:2]:
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        if len(raw) > MAX_SCREENSHOT_BYTES or not raw.startswith(PNG_MAGIC):
            continue
        out.append(
            {
                "name": f"{label or 'screenshot'}.png",
                "png_b64": base64.b64encode(raw).decode("ascii"),
            }
        )
    return out


def fetch_bug_report_profile(
    *,
    license_key: str,
    base_url: str | None = None,
    opener: Callable[..., tuple[int, str]] | None = None,
) -> dict[str, str]:
    """Fetch the user's saved Discord handle and public invite URL, failing silently."""
    key = str(license_key or "").strip()
    if not key:
        return {"discord_handle": "", "invite_url": ""}
    send = opener or _default_opener
    url = (base_url or _website_base()).rstrip("/") + BUG_REPORT_ME_PATH
    try:
        status, raw = send(url, "GET", None, _auth_headers(key), 8.0)
        if status == 200 and raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {
                    "discord_handle": str(data.get("discord_handle") or ""),
                    "invite_url": str(data.get("invite_url") or ""),
                }
    except Exception:
        pass
    return {"discord_handle": "", "invite_url": ""}


def _parse_response_payload(status: int, raw_body: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        obj = json.loads(raw_body) if raw_body else {}
        if isinstance(obj, dict):
            parsed = obj

    detail = parsed.get("detail")
    err_obj = detail if isinstance(detail, dict) else parsed
    error_code = str(err_obj.get("error") or "").strip()
    message = str(err_obj.get("message") or (detail if isinstance(detail, str) else "") or "").strip()
    invite_url = str(err_obj.get("invite_url") or "").strip()

    report_status = str(parsed.get("status") or "").strip()
    if status in (200, 201) and report_status in ("created", "attached", "possible_duplicates"):
        return {
            "ok": True,
            "http_status": status,
            "status": report_status,
            "bug_id": str(parsed.get("bug_id") or ""),
            "discord_username": str(parsed.get("discord_username") or ""),
            "thread_id": str(parsed.get("thread_id") or ""),
            "thread_url": str(parsed.get("thread_url") or ""),
            "matches": list(parsed.get("matches") or []) if isinstance(parsed.get("matches"), list) else [],
            "error_code": "",
            "message": "",
            "invite_url": "",
        }

    if not message:
        if status == 401:
            message = "Your MTGA Coach key wasn't recognized. Set a valid trial or Patron key first."
        elif status == 402:
            message = "Your subscription or trial has expired."
        elif status == 429:
            message = "Too many bug reports sent recently. Please wait a moment before trying again."
        else:
            message = f"Upload failed (HTTP {status or 'network error'})."

    return {
        "ok": False,
        "http_status": status,
        "status": "error",
        "bug_id": "",
        "discord_username": "",
        "thread_id": "",
        "thread_url": "",
        "matches": [],
        "error_code": error_code or f"http_{status}",
        "message": message,
        "invite_url": invite_url,
    }


def submit_bug_report(
    *,
    license_key: str,
    title: str,
    note: str,
    category: str = "desktop",
    discord_handle: str = "",
    report_data: dict[str, Any] | None = None,
    screenshot_paths: list[tuple[str, Path]] | None = None,
    include_screenshots: bool = False,
    existing_thread_id: str = "",
    force_new: bool = False,
    base_url: str | None = None,
    opener: Callable[..., tuple[int, str]] | None = None,
) -> dict[str, Any]:
    """Submit a bug report to ``mtgacoach.com`` for Discord member verification and triage."""
    key = str(license_key or "").strip()
    if not key:
        return {
            "ok": False,
            "http_status": 0,
            "status": "error",
            "bug_id": "",
            "discord_username": "",
            "thread_id": "",
            "thread_url": "",
            "matches": [],
            "error_code": "missing_license_key",
            "message": "Set your MTGA Coach trial or Patron key in the app before sending a bug report to Discord.",
            "invite_url": "",
        }

    payload: dict[str, Any] = {
        "title": " ".join(str(title or "").split()),
        "note": str(note or "").strip(),
        "category": str(category or "desktop").strip().lower() or "desktop",
        "discord_handle": str(discord_handle or "").strip(),
        "app_version": __version__,
        "platform": f"{platform.system()} {platform.release()}".strip(),
        "existing_thread_id": str(existing_thread_id or "").strip(),
        "force_new": bool(force_new),
        "report_gz_b64": prepare_report_gz(report_data or {}),
        "screenshots": encode_screenshots(screenshot_paths or []) if include_screenshots else [],
    }
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = (base_url or _website_base()).rstrip("/") + BUG_REPORT_PATH
    send = opener or _default_opener
    try:
        status, raw_body = send(url, "POST", encoded, _auth_headers(key), TIMEOUT_S)
    except Exception as exc:
        return {
            "ok": False,
            "http_status": 0,
            "status": "error",
            "bug_id": "",
            "discord_username": "",
            "thread_id": "",
            "thread_url": "",
            "matches": [],
            "error_code": "network_error",
            "message": f"Could not reach mtgacoach.com: {exc}",
            "invite_url": "",
        }
    return _parse_response_payload(status, raw_body)
