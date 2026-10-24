"""Free-trial key provisioning for the desktop app.

The legacy OpenAI-compatible proxy routes (/v1/models, /v1/chat/completions,
/v1/responses*) were removed on 2026-10-18: customer AI traffic goes to the
LiteLLM gateway at api.mtgacoach.com, the old mc_ keys are dead, and the
website's proxy log showed no authenticated call to them since June.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from abuse_protection import finish_signup, network_identity, request_origin, reserve_signup
from fastapi import APIRouter, HTTPException, Request
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


@router.post("/api/trial")
async def request_trial(request: Request):
    """Auto-provision a 7-day trial key on the desktop app's first run.

    One trial per machine_id, ever. While the trial is active, repeat calls
    return the same key; after it lapses, 403 gates on a Patreon subscription.
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

    now = datetime.now(timezone.utc)

    existing = _get_db().get_trial(machine_id)
    if existing:
        try:
            expires = _parse_trial_iso(existing["expires_at"] or "")
        except ValueError:
            expires = now  # unparseable record â€” treat as lapsed
        if expires > now:
            _record_trial_event(request, "existing", machine_id, app_version)
            return {
                "key": existing["litellm_key"],
                "expires_at": existing["expires_at"],
                "status": "existing",
            }
        _record_trial_event(request, "expired", machine_id, app_version)
        raise HTTPException(403, "trial_expired")

    # Per-machine and per-network issuance limits (429 + Retry-After).
    try:
        reserve_signup(_get_db().get_db, machine_id, network_identity(request))
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
    _get_db().create_trial(machine_id, key, _trial_iso(now), expires_at)
    finish_signup(_get_db().get_db, machine_id, True)
    _record_trial_event(request, "created", machine_id, app_version)
    logger.info(
        f"Trial key issued for machine {machine_id[:12]}... "
        f"(app_version={app_version or 'unknown'}, expires {expires_at})"
    )
    return {"key": key, "expires_at": expires_at, "status": "created"}



# =========================================================================
