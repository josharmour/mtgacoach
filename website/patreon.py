"""Patreon signup -> LiteLLM virtual keys.

The LiteLLM gateway (api.mtgacoach.com, see gateway/) is the single source of
truth for customer keys. This module makes Patreon membership events mint and
revoke real LiteLLM ``sk-`` keys; the local subscribers table is kept as
email<->key bookkeeping only (its legacy ``mc_`` keys are dead against the
gateway).

Flows:
- ``POST /patreon/webhook``  — pledge created/updated -> mint key (emailed if
  SMTP is configured); pledge deleted/lapsed -> delete key at the gateway.
- ``GET /patreon/callback``  — OAuth "get your license key" page: verifies the
  patron has an active pledge, then shows (minting if needed) their key.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import smtplib
import time
from email.message import EmailMessage
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

import db

logger = logging.getLogger(__name__)

router = APIRouter()

PATREON_CLIENT_ID = os.environ.get("PATREON_CLIENT_ID", "")
PATREON_CLIENT_SECRET = os.environ.get("PATREON_CLIENT_SECRET", "")
PATREON_WEBHOOK_SECRET = os.environ.get("PATREON_WEBHOOK_SECRET", "")

# LiteLLM gateway admin API. The website container runs on the same NAS as the
# gateway, so the default talks to it directly (no Cloudflare in the path).
LITELLM_URL = os.environ.get("LITELLM_URL", "http://10.0.0.2:8444").rstrip("/")
LITELLM_MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")

# What a patron key is allowed to do — mirrors the manually-migrated keys.
PATRON_KEY_MODELS = ["deepseek-v4-flash", "gemma-4-12b-it"]
PATRON_KEY_BUDGET = float(os.environ.get("MTGACOACH_PATRON_BUDGET", "25"))
PATRON_KEY_BUDGET_DURATION = "30d"

# Optional key-delivery email. Unset SMTP_HOST = skip emailing (the OAuth
# callback page is the primary delivery path either way).
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER or "keys@mtgacoach.com")

_USER_AGENT = "mtgacoach-website/1.0"

_http_client: Optional[httpx.AsyncClient] = None


def _http() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": _USER_AGENT}
        )
    return _http_client


# ---------------------------------------------------------------------------
# LiteLLM key admin
# ---------------------------------------------------------------------------


def _admin_headers() -> dict[str, str]:
    if not LITELLM_MASTER_KEY:
        raise RuntimeError("LITELLM_MASTER_KEY is not configured")
    return {"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}


async def mint_litellm_key(email: str, name: str, patron_id: str) -> str:
    """Create a scoped LiteLLM virtual key for a patron and return it."""
    payload: dict[str, Any] = {
        "models": PATRON_KEY_MODELS,
        "key_alias": f"patreon-{email}",
        "max_budget": PATRON_KEY_BUDGET,
        "budget_duration": PATRON_KEY_BUDGET_DURATION,
        "metadata": {
            "email": email,
            "name": name,
            "patron_id": patron_id,
            "source": "patreon",
        },
    }
    for attempt in (1, 2):
        resp = await _http().post(
            f"{LITELLM_URL}/key/generate", json=payload, headers=_admin_headers()
        )
        if resp.status_code == 200:
            key = resp.json().get("key", "")
            if not key:
                raise RuntimeError(f"LiteLLM /key/generate returned no key: {resp.text[:200]}")
            logger.info(f"LiteLLM key minted for {email} (alias {payload['key_alias']})")
            return key
        # key_alias must be unique gateway-wide; a stale alias from an earlier
        # key (e.g. re-subscribe after cancel) collides — retry once suffixed.
        if attempt == 1 and resp.status_code == 400:
            payload["key_alias"] = f"patreon-{email}-{secrets.token_hex(3)}"
            continue
        raise RuntimeError(
            f"LiteLLM /key/generate failed: {resp.status_code} {resp.text[:300]}"
        )
    raise RuntimeError("unreachable")


async def delete_litellm_key(key: str) -> bool:
    """Best-effort delete of a gateway key. Returns True if it was deleted."""
    try:
        resp = await _http().post(
            f"{LITELLM_URL}/key/delete", json={"keys": [key]}, headers=_admin_headers()
        )
        if resp.status_code == 200:
            return True
        logger.warning(f"LiteLLM /key/delete {key[:12]}...: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"LiteLLM /key/delete {key[:12]}... errored: {e}")
    return False


# ---------------------------------------------------------------------------
# Subscriber bookkeeping (email <-> key)
# ---------------------------------------------------------------------------


def _subscriber_by_email(email: str) -> Optional[dict]:
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM subscribers WHERE email = ?", (email,)
        ).fetchone()
    return dict(row) if row else None


def _record_key(email: str, name: str, key: str, notes: str) -> None:
    """Insert or replace the subscriber row for email with the new key."""
    now = time.time()
    with db.get_db() as conn:
        existing = conn.execute(
            "SELECT license_key FROM subscribers WHERE email = ?", (email,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE subscribers SET license_key = ?, status = 'active', "
                "expires_at = NULL, notes = ? WHERE email = ?",
                (key, notes, email),
            )
        else:
            conn.execute(
                "INSERT INTO subscribers (license_key, email, name, status, created_at, expires_at, notes) "
                "VALUES (?, ?, ?, 'active', ?, NULL, ?)",
                (key, email, name, now, notes),
            )


async def ensure_active_key(email: str, name: str, patron_id: str, source: str) -> str:
    """Return the patron's working sk- key, minting/replacing as needed."""
    sub = _subscriber_by_email(email)
    if sub and sub["status"] == "active" and sub["license_key"].startswith("sk-"):
        return sub["license_key"]

    # Anything else — new patron, legacy mc_ key, or revoked/expired row —
    # gets a fresh gateway key. A revoked row's old sk- key was already
    # deleted at the gateway; delete defensively anyway.
    if sub and sub["license_key"].startswith("sk-"):
        await delete_litellm_key(sub["license_key"])

    key = await mint_litellm_key(email, name, patron_id)
    _record_key(email, name, key, f"Patreon {source} (patron_id={patron_id})")
    logger.info(f"Patreon: active key ensured for {email} via {source}")
    asyncio.get_running_loop().create_task(_email_key(email, name, key))
    return key


async def revoke_access(email: str, reason: str) -> None:
    sub = _subscriber_by_email(email)
    if not sub:
        return
    if sub["license_key"].startswith("sk-"):
        await delete_litellm_key(sub["license_key"])
    db.revoke_subscriber(sub["license_key"])
    logger.info(f"Patreon: revoked {email} ({reason})")


# ---------------------------------------------------------------------------
# Key-delivery email (optional)
# ---------------------------------------------------------------------------


def _send_email_sync(to_addr: str, name: str, key: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = "Your mtgacoach license key"
    msg["From"] = SMTP_FROM
    msg["To"] = to_addr
    msg.set_content(
        f"Welcome{', ' + name if name else ''}!\n\n"
        f"Your mtgacoach license key:\n\n    {key}\n\n"
        "Enter it in the mtgacoach app under Settings -> License Key.\n"
        "You can always retrieve it again at https://mtgacoach.com/subscribe\n"
        'via "Already a patron? Get your license key".\n\n'
        "Thanks for supporting mtgacoach!\n"
    )
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
        smtp.starttls()
        if SMTP_USER:
            smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)


async def _email_key(to_addr: str, name: str, key: str) -> None:
    if not SMTP_HOST:
        logger.info(f"SMTP not configured; not emailing key to {to_addr}")
        return
    try:
        await asyncio.to_thread(_send_email_sync, to_addr, name, key)
        logger.info(f"Emailed license key to {to_addr}")
    except Exception as e:
        logger.error(f"Failed to email key to {to_addr}: {e}")


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


@router.post("/patreon/webhook")
async def patreon_webhook(request: Request):
    """Handle Patreon webhook events for membership changes."""
    body_bytes = await request.body()

    if PATREON_WEBHOOK_SECRET:
        signature = request.headers.get("X-Patreon-Signature", "")
        expected = hmac.new(
            PATREON_WEBHOOK_SECRET.encode(), body_bytes, hashlib.md5
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            logger.warning("Patreon webhook: invalid signature")
            raise HTTPException(403, "Invalid signature")

    event_type = request.headers.get("X-Patreon-Event", "")
    data = json.loads(body_bytes)
    logger.info(f"Patreon webhook: {event_type}")

    patron_email = ""
    patron_name = ""
    patron_id = ""
    pledge_active = False
    try:
        for item in data.get("included", []):
            if item.get("type") == "user":
                attrs = item.get("attributes", {})
                patron_email = attrs.get("email", "")
                patron_name = attrs.get("full_name", "")
                patron_id = item.get("id", "")
        member_attrs = data.get("data", {}).get("attributes", {})
        pledge_active = member_attrs.get("patron_status", "") == "active_patron"
    except Exception as e:
        logger.error(f"Patreon webhook parse error: {e}")
        return {"ok": True}  # Malformed payload — retrying won't help.

    if not patron_email:
        logger.warning(f"Patreon webhook {event_type}: no patron email in payload")
        return {"ok": True}

    # A gateway hiccup below raises out of here -> 500 -> Patreon retries the
    # event, which is exactly what we want for transient LiteLLM outages.
    if event_type in ("members:pledge:create", "members:create"):
        if pledge_active:
            await ensure_active_key(patron_email, patron_name, patron_id, "webhook signup")
        else:
            logger.info(f"Patreon: {patron_email} created but not active yet")

    elif event_type in ("members:pledge:delete", "members:delete"):
        await revoke_access(patron_email, event_type)

    elif event_type == "members:pledge:update":
        if pledge_active:
            await ensure_active_key(patron_email, patron_name, patron_id, "webhook update")
        else:
            await revoke_access(patron_email, "pledge inactive")

    return {"ok": True}


# ---------------------------------------------------------------------------
# OAuth callback ("Already a patron? Get your license key")
# ---------------------------------------------------------------------------


def _membership_allows_key(identity: dict) -> tuple[bool, str]:
    """Decide from the /identity response whether this user gets a key.

    Fail-closed when Patreon positively reports no active pledge; fail-open
    (with a loud log) when membership data is absent/ambiguous, so an API
    shape change can't lock every patron out of key recovery.
    """
    members = [
        item for item in identity.get("included", [])
        if item.get("type") == "member"
    ]
    if not members:
        logger.warning(
            "Patreon OAuth: no membership data in identity response; allowing. "
            f"payload keys: {list(identity.keys())}"
        )
        return True, "unverified"
    for m in members:
        if m.get("attributes", {}).get("patron_status") == "active_patron":
            return True, "active_patron"
    statuses = [m.get("attributes", {}).get("patron_status") for m in members]
    return False, ",".join(str(s) for s in statuses)


_PAGE_STYLE = """
<style>
    body { font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
           background: #16130f; color: #e6ddcc; line-height: 1.65;
           display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
    .card { border-top: 1px solid #353026; border-bottom: 1px solid #353026;
            padding: 48px 32px; text-align: center; max-width: 520px; }
    h1 { font-weight: 500; font-style: italic; color: #d4a13c; margin: 0 0 12px; }
    h1.err { color: #b3543f; }
    p { color: #97907f; margin: 12px 0; }
    a { color: #d4a13c; }
    .key { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
           color: #e6ddcc; background: #221e17; border: 1px solid #353026;
           padding: 16px; word-break: break-all; margin: 20px 0; cursor: pointer; font-size: 0.9rem; }
    .key:hover { border-color: #d4a13c; }
    code { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
           background: #221e17; padding: 1px 6px; font-size: 0.9em; color: #e6ddcc; }
</style>
"""


def _error_page(title: str, message: str, status: int = 400) -> HTMLResponse:
    return HTMLResponse(
        f"<html><head><title>mtgacoach</title>{_PAGE_STYLE}</head><body>"
        f'<div class="card"><h1 class="err">{title}</h1><p>{message}</p></div>'
        "</body></html>",
        status_code=status,
    )


@router.get("/patreon/callback")
async def patreon_callback(request: Request):
    """Patron links their Patreon account and gets their license key."""
    code = request.query_params.get("code", "")
    if not code:
        return _error_page("Error", "No authorization code received.")

    try:
        token_resp = await _http().post(
            "https://www.patreon.com/api/oauth2/token",
            data={
                "code": code,
                "grant_type": "authorization_code",
                "client_id": PATREON_CLIENT_ID,
                "client_secret": PATREON_CLIENT_SECRET,
                "redirect_uri": "https://mtgacoach.com/patreon/callback",
            },
        )
        access_token = token_resp.json().get("access_token", "")
        if not access_token:
            logger.error(f"Patreon OAuth failed: {token_resp.text[:300]}")
            return _error_page("Error", "Could not authenticate with Patreon.")

        identity_resp = await _http().get(
            "https://www.patreon.com/api/oauth2/v2/identity"
            "?fields%5Buser%5D=email,full_name"
            "&include=memberships"
            "&fields%5Bmember%5D=patron_status",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        identity = identity_resp.json()
        user_data = identity.get("data", {}).get("attributes", {})
        patron_email = user_data.get("email", "")
        patron_name = user_data.get("full_name", "")
        patron_id = identity.get("data", {}).get("id", "")

        if not patron_email:
            return _error_page("Error", "Could not get your email from Patreon.")

        allowed, status = _membership_allows_key(identity)
        if not allowed:
            logger.info(f"Patreon OAuth: {patron_email} denied (status: {status})")
            return _error_page(
                "No active pledge",
                "We couldn't find an active mtgacoach pledge on this Patreon "
                'account. If you just pledged, wait a minute and retry; otherwise '
                'pledge at <a href="https://www.patreon.com/mtgacoach">patreon.com/mtgacoach</a>.',
                status=403,
            )

        license_key = await ensure_active_key(
            patron_email, patron_name, patron_id, f"oauth ({status})"
        )

        copy_js = (
            f"navigator.clipboard.writeText('{license_key}')"
            ".then(()=>this.textContent='Copied!')"
        )
        return HTMLResponse(
            f"<html><head><title>mtgacoach - Welcome!</title>{_PAGE_STYLE}</head><body>"
            '<div class="card">'
            f"<h1>Welcome, {patron_name or 'Patron'}!</h1>"
            "<p>Your license key is:</p>"
            f'<div class="key" onclick="{copy_js}" title="Click to copy">{license_key}</div>'
            "<p>Enter it in the mtgacoach app under <code>Settings &rarr; License Key</code>.</p>"
            "</div></body></html>"
        )

    except Exception as e:
        logger.error(f"Patreon OAuth error: {e}")
        return _error_page("Error", "Something went wrong — please try again.", status=500)
