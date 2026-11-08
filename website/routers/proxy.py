"""Free-trial key provisioning for the desktop app.

The legacy OpenAI-compatible proxy routes (/v1/models, /v1/chat/completions,
/v1/responses*) were removed on 2026-10-18: customer AI traffic goes to the
LiteLLM gateway at api.mtgacoach.com, the old mc_ keys are dead, and the
website's proxy log showed no authenticated call to them since June.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from abuse_protection import (
    finish_signup,
    network_identity,
    request_origin,
    reserve_code_request,
    reserve_signup,
)
from fastapi import APIRouter, HTTPException, Request, Response
from state import _extract_client_metadata, _get_db, _record_trial_event


def _get_patreon():
    import sys
    import patreon
    return sys.modules.get("patreon", patreon)

logger = logging.getLogger("website.proxy")

router = APIRouter()


# =========================================================================
#  Free trial (desktop app first run â€” no Patreon required)
# =========================================================================

# machine_id is a sha256 hex digest computed client-side; nothing else is
# accepted, so the endpoint can't be used to mint keys for arbitrary strings.
_MACHINE_ID_RE = re.compile(r"^[0-9a-f]{64}$")
TRIAL_DAYS = 7

# Email verification for new mints: a 6-digit code is emailed and only its
# salted sha256 hash is stored. The salt is the app's existing admin secret
# (state.admin_password, set from config/env at import) — never a hardcoded
# constant — falling back to the machine_id if the secret is somehow empty.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CODE_TTL_SECONDS = 900
MAX_CODE_ATTEMPTS = 5


def _app_secret() -> str:
    import sys
    return getattr(sys.modules.get("state"), "admin_password", "") or ""


def _hash_trial_code(code: str, machine_id: str) -> str:
    salt = _app_secret() or machine_id
    return hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()


def _clean_email(body: dict) -> str:
    """Lowercased, stripped email from a request body ('' when absent)."""
    return str(body.get("email", "")).strip().lower()


@router.post("/api/trial/request-code")
async def request_trial_code(request: Request):
    """Email a 6-digit verification code for a machine's trial signup.

    Only the salted hash of the code is stored (15-minute TTL, one pending
    row per machine — a new request overwrites it). Rate limited per machine
    and per network; the code itself travels by email only.
    """
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        _record_trial_event(request, "invalid")
        raise HTTPException(400, "Invalid JSON body")

    machine_id = str(body.get("machine_id", "")).strip().lower()
    app_version = str(body.get("app_version", "")).strip()[:64]
    if not _MACHINE_ID_RE.fullmatch(machine_id):
        _record_trial_event(request, "invalid", app_version=app_version)
        raise HTTPException(422, "machine_id must be a 64-character sha256 hex digest")

    email = _clean_email(body)
    if not _EMAIL_RE.fullmatch(email):
        _record_trial_event(request, "invalid", machine_id, app_version)
        raise HTTPException(422, "email must be a valid address")

    # Per-machine (3/hour) and per-network (20/hour) code-request limits.
    try:
        reserve_code_request(_get_db().get_db, machine_id, network_identity(request))
    except HTTPException as e:
        event = "code_rate_limited" if e.status_code == 429 else "unverified_network"
        _record_trial_event(request, event, machine_id, app_version)
        raise

    now = datetime.now(timezone.utc)
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = _trial_iso(now + timedelta(seconds=CODE_TTL_SECONDS))
    _get_db().save_trial_email_code(
        machine_id, email, _hash_trial_code(code, machine_id), _trial_iso(now), expires_at
    )

    patreon = _get_patreon()
    if not getattr(patreon, "SMTP_HOST", ""):
        _record_trial_event(request, "code_send_failed", machine_id, app_version)
        raise HTTPException(503, "email_delivery_unavailable")
    try:
        await asyncio.to_thread(patreon.send_trial_code_email, email, code)
    except Exception as e:
        logger.error(f"Trial code email to {email} failed for machine {machine_id[:12]}...: {e}")
        _record_trial_event(request, "code_send_failed", machine_id, app_version)
        raise HTTPException(503, "email_delivery_unavailable")

    _record_trial_event(request, "code_requested", machine_id, app_version)
    logger.info(f"Trial verification code emailed to {email} (machine {machine_id[:12]}...)")
    return {"status": "code_sent", "expires_in": CODE_TTL_SECONDS}


def _trial_iso(dt: datetime) -> str:
    """ISO8601 UTC with a trailing Z, second precision."""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_trial_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _mint_context(request: Request, app_version: str) -> dict[str, str]:
    """Gateway key metadata that lets spend logs be joined back to signups."""
    origin = request_origin(request)
    return {
        "app_version": app_version,
        "install_id": _extract_client_metadata(request)["install_id"],
        "country": origin["country"],
        "network": origin["network_hash"][:12],
    }


def _verify_trial_code(
    body: dict, machine_id: str, now: datetime, request: Request, app_version: str
) -> None:
    email = _clean_email(body)
    code = str(body.get("code", "")).strip()
    if not email or not code:
        _record_trial_event(request, "email_verification_required", machine_id, app_version)
        raise HTTPException(422, "email_verification_required")
    verified = _get_db().verify_and_consume_trial_email_code(
        machine_id, email, _hash_trial_code(code, machine_id), now, MAX_CODE_ATTEMPTS
    )
    if not verified:
        _record_trial_event(request, "invalid_code", machine_id, app_version)
        raise HTTPException(422, "invalid_code")


@router.post("/api/trial")
async def request_trial(request: Request, response: Response):
    """Auto-provision a 7-day trial key on the desktop app's first run.

    One trial per machine_id, ever. While the trial is active, repeat calls
    return the same key; after it lapses, 403 gates on a Patreon subscription.
    New mints require an email address verified via /api/trial/request-code;
    recovery requires the existing bearer key or verification of the email
    already bound to that trial. A machine identifier is not authentication.
    """
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        _record_trial_event(request, "invalid")
        raise HTTPException(400, "Invalid JSON body")

    machine_id = str(body.get("machine_id", "")).strip().lower()
    app_version = str(body.get("app_version", "")).strip()[:64]
    if not _MACHINE_ID_RE.fullmatch(machine_id):
        _record_trial_event(request, "invalid", app_version=app_version)
        raise HTTPException(422, "machine_id must be a 64-character sha256 hex digest")

    now = datetime.now(timezone.utc)

    existing = _get_db().get_trial(machine_id)
    if existing:
        try:
            expires = _parse_trial_iso(existing["expires_at"] or "")
        except ValueError:
            expires = now  # unparseable record â€” treat as lapsed
        if expires > now:
            # Knowing a machine identifier must never reveal its bearer key.
            authorization = request.headers.get("Authorization", "")
            scheme, _, supplied_key = authorization.partition(" ")
            key_matches = (
                scheme.lower() == "bearer"
                and bool(supplied_key)
                and hmac.compare_digest(supplied_key.encode(), str(existing["litellm_key"]).encode())
            )
            if not key_matches:
                email = _clean_email(body)
                if (
                    not existing.get("recovery_email_verified")
                    or not existing.get("email")
                    or email != str(existing["email"]).strip().lower()
                ):
                    _record_trial_event(request, "recovery_required", machine_id, app_version)
                    raise HTTPException(422, "trial_recovery_required")
                _verify_trial_code(body, machine_id, now, request, app_version)
            # Do not bind a new email here, even when a valid bearer is supplied.
            _record_trial_event(request, "existing", machine_id, app_version)
            return {
                "key": existing["litellm_key"],
                "expires_at": existing["expires_at"],
                "status": "existing",
            }
        await _get_patreon().maybe_email_expired_trial(existing)
        _record_trial_event(request, "expired", machine_id, app_version)
        raise HTTPException(403, "trial_expired")

    # Fail closed on unverifiable origins before anything else.
    try:
        network = network_identity(request)
    except HTTPException:
        _record_trial_event(request, "unverified_network", machine_id, app_version)
        raise

    # New mints require a freshly verified email address.
    email = _clean_email(body)
    _verify_trial_code(body, machine_id, now, request, app_version)

    # Per-machine and per-network issuance limits (429 + Retry-After).
    try:
        reserve_signup(_get_db().get_db, machine_id, network)
    except HTTPException as e:
        event = "rate_limited" if e.status_code == 429 else "unverified_network"
        _record_trial_event(request, event, machine_id, app_version)
        raise

    try:
        key = await _get_patreon().mint_trial_key(machine_id, _mint_context(request, app_version))
    except Exception as e:
        finish_signup(_get_db().get_db, machine_id, False)
        _record_trial_event(request, "mint_failed", machine_id, app_version)
        logger.error(f"Trial mint failed for machine {machine_id[:12]}...: {e}")
        raise HTTPException(503, "Trial provisioning is temporarily unavailable. Try again later.")

    expires_at = _trial_iso(now + timedelta(days=TRIAL_DAYS))
    _get_db().create_trial(
        machine_id, key, _trial_iso(now), expires_at, email=email, recovery_email_verified=True
    )
    finish_signup(_get_db().get_db, machine_id, True)
    _record_trial_event(request, "created", machine_id, app_version)
    logger.info(
        f"Trial key issued for machine {machine_id[:12]}... "
        f"(app_version={app_version or 'unknown'}, expires {expires_at})"
    )
    return {"key": key, "expires_at": expires_at, "status": "created"}



# =========================================================================
