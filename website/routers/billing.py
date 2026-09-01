"""Subscription verification and billing webhooks."""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import patreon
import state
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from state import (
    LOG_DIR,
    _extract_client_metadata,
    _extract_license_key,
    _get_db,
    _get_stored_response,
    _record_client_telemetry,
    _reload_providers,
    _require_admin,
    _require_license,
    _resolved_default_model,
    _resolved_provider_configs,
    _response_store,
    _store_response,
    templates,
)

logger = logging.getLogger("website.billing")

router = APIRouter()
router.include_router(patreon.router)

# =========================================================================
#  Subscription endpoints (used by mtgacoach client)
# =========================================================================

@router.post("/v1/subscription/check")
async def check_subscription(request: Request):
    """Check subscription status. Returns status + any pending messages."""
    key = _extract_license_key(request)
    if not key:
        # Also try JSON body
        try:
            body = await request.json()
            key = body.get("license_key", "")
        except Exception:
            pass

    if not key:
        return JSONResponse(status_code=401, content={
            "status": "invalid",
            "message": "No license key provided.",
        })

    sub = _get_db().check_license(key)
    if not sub:
        return JSONResponse(status_code=401, content={
            "status": "invalid",
            "message": "Invalid license key.",
        })

    _record_client_telemetry(request, sub["license_key"])

    messages = _get_db().get_messages_after(0)  # Client tracks last_seen_message_id

    result = {
        "status": sub["status"],
        "message": "",
        "expires_at": sub.get("expires_at"),
        "messages": [
            {"id": m["id"], "title": m["title"], "body": m["body"],
             "priority": m["priority"], "created_at": m["created_at"]}
            for m in messages
        ],
    }

    if sub["status"] == "expired":
        result["message"] = "Subscription expired. Renew at mtgacoach.com/subscribe"
    elif sub["status"] == "revoked":
        result["message"] = "Subscription revoked."

    status_code = 200 if sub["status"] in ("active", "trial") else 402
    return JSONResponse(status_code=status_code, content=result)


@router.get("/v1/subscription/messages")
async def get_messages(request: Request):
    """Get service messages for a subscriber."""
    key = _extract_license_key(request)
    if not key:
        raise HTTPException(401, "Missing license key")

    sub = _get_db().check_license(key)
    if not sub:
        raise HTTPException(401, "Invalid license key")

    _record_client_telemetry(request, sub["license_key"])

    messages = _get_db().get_messages_after(0)
    return {
        "messages": [
            {"id": m["id"], "title": m["title"], "body": m["body"],
             "priority": m["priority"], "created_at": m["created_at"]}
            for m in messages
        ]
    }


# =========================================================================
#  Web pages (landing, subscribe, admin)
# =========================================================================

@router.get("/", response_class=HTMLResponse)
async def landing_page(request: Request):
    return templates.TemplateResponse(request=request, name="landing.html")


@router.get("/subscribe", response_class=HTMLResponse)
async def subscribe_page(request: Request):
    return templates.TemplateResponse(request=request, name="subscribe.html")


@router.post("/subscribe/request")
async def subscribe_request(request: Request):
    """Block public self-service key issuance.

    Existing subscribers remain valid, but new keys must be created through
    Patreon or the admin dashboard. This prevents anonymous callers from
    minting or recovering active customer license keys.
    """
    body = await request.json()
    email = body.get("email", "").strip()
    if not email:
        raise HTTPException(400, "Email is required")

    logger.warning("Blocked public self-service signup attempt for %s", email)
    raise HTTPException(
        403,
        "Self-service key issuance is disabled. Subscribe via Patreon or contact support.",
    )


