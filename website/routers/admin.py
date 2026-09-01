"""Admin dashboard and management endpoints."""

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

logger = logging.getLogger("website.admin")

router = APIRouter()

# =========================================================================
#  Admin API (for managing subscribers + messages)
# =========================================================================

@router.get("/admin")
async def admin_page(request: Request):
    # Key/subscription management moved to the LiteLLM gateway UI.
    # The legacy dashboard is still reachable at /admin/legacy.
    return RedirectResponse("https://api.mtgacoach.com/ui", status_code=302)


@router.get("/admin/legacy", response_class=HTMLResponse)
async def admin_legacy_page(request: Request):
    return templates.TemplateResponse(request=request, name="admin.html")


@router.get("/admin/api/subscribers")
async def admin_list_subscribers(request: Request, _=Depends(_require_admin)):
    subs = _get_db().list_subscribers()
    # Add usage info
    usage = {u["license_key"]: u for u in _get_db().get_all_usage_summary(30)}
    clients = _get_db().get_client_summary(30)
    for s in subs:
        u = usage.get(s["license_key"], {})
        c = clients.get(s["license_key"], {})
        s["requests_30d"] = u.get("requests", 0)
        s["tokens_30d"] = u.get("total_prompt", 0) + u.get("total_completion", 0)
        s["installs_30d"] = c.get("installs_30d", 0)
        s["frontends_30d"] = c.get("frontends_30d", "")
        s["latest_frontend"] = c.get("latest_frontend", "")
        s["latest_version"] = c.get("latest_version", "")
        s["latest_install_id"] = c.get("latest_install_id", "")
        s["last_seen_at"] = c.get("last_seen_at", 0)

    # Surface the union of provider models so the UI can render a model
    # picker per subscriber. Plus the cluster default for the "(default)"
    # placeholder hint.
    models: list[str] = []
    seen: set[str] = set()
    for p in state.router.providers:
        for m in p.models:
            if m not in seen:
                seen.add(m)
                models.append(m)
    return {
        "subscribers": subs,
        "available_models": models,
        "default_model": _resolved_default_model(),
    }


@router.post("/admin/api/subscribers")
async def admin_create_subscriber(request: Request, _=Depends(_require_admin)):
    body = await request.json()
    result = _get_db().create_subscriber(
        email=body.get("email", ""),
        name=body.get("name", ""),
        days=body.get("days", 30),
        notes=body.get("notes", ""),
    )
    return result


@router.put("/admin/api/subscribers/{key}")
async def admin_update_subscriber(key: str, request: Request, _=Depends(_require_admin)):
    body = await request.json()
    if body.get("action") == "extend":
        _get_db().extend_subscriber(key, body.get("days", 30))
        return {"ok": True}
    elif body.get("action") == "revoke":
        _get_db().revoke_subscriber(key)
        return {"ok": True}
    elif body.get("action") == "activate":
        _get_db().update_subscriber(key, status="active")
        return {"ok": True}
    else:
        _get_db().update_subscriber(key, **{k: v for k, v in body.items() if k != "action"})
        return {"ok": True}


@router.delete("/admin/api/subscribers/{key}")
async def admin_delete_subscriber(key: str, request: Request, _=Depends(_require_admin)):
    _get_db().delete_subscriber(key)
    return {"ok": True}


@router.get("/admin/api/messages")
async def admin_list_messages(request: Request, _=Depends(_require_admin)):
    return _get_db().list_messages()


@router.post("/admin/api/messages")
async def admin_create_message(request: Request, _=Depends(_require_admin)):
    body = await request.json()
    msg_id = _get_db().create_message(
        title=body["title"],
        body=body["body"],
        priority=body.get("priority", "normal"),
        target=body.get("target", "all"),
    )
    return {"id": msg_id}


@router.delete("/admin/api/messages/{msg_id}")
async def admin_delete_message(msg_id: int, request: Request, _=Depends(_require_admin)):
    _get_db().delete_message(msg_id)
    return {"ok": True}


@router.get("/admin/api/usage")
async def admin_usage(request: Request, _=Depends(_require_admin)):
    return _get_db().get_all_usage_summary(30)


@router.get("/admin/api/eval")
async def admin_eval(request: Request, _=Depends(_require_admin)):
    """Return the latest eval-results payload per target plus recent history."""
    return _get_db().get_latest_eval_results()


@router.get("/admin/api/eval/history")
async def admin_eval_history(request: Request, _=Depends(_require_admin)):
    """Return the last N eval payloads for a single target (oldest first).

    Query params:
      - target: required (e.g. 'general' or '17lands_mulligan')
      - limit:  default 30, max 200
    """
    target = (request.query_params.get("target") or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="missing 'target' query param")
    try:
        limit = max(1, min(200, int(request.query_params.get("limit", "30"))))
    except ValueError:
        limit = 30
    return {"target": target, "history": _get_db().get_eval_results_history(target, limit)}


@router.post("/admin/api/eval/results")
async def admin_eval_upload(request: Request, _=Depends(_require_admin)):
    """Accept an eval-results upload from `tools/eval/upload_results.py`.

    Body must be a JSON object with at least a ``target`` field. Stored
    verbatim under ``eval_results``; the ``ts`` field is honored if present.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    target = (payload.get("target") or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="missing 'target' field")
    if len(target) > 64 or any(c.isspace() for c in target):
        raise HTTPException(status_code=400, detail="invalid 'target' (no whitespace, â‰¤64 chars)")
    row_id = _get_db().insert_eval_result(target, payload)
    return {"ok": True, "id": row_id, "target": target}


@router.get("/admin/api/activity")
async def admin_activity(request: Request, _=Depends(_require_admin)):
    """Time-bucketed activity series for the dashboard chart.

    Query params:
      - days: how far back to look (default 7, max 365)
      - bucket: bucket size in seconds (default auto from days)
    """
    try:
        days = max(1, min(365, int(request.query_params.get("days", "7"))))
    except ValueError:
        days = 7

    bucket_param = request.query_params.get("bucket")
    if bucket_param:
        try:
            bucket_seconds = max(60, int(bucket_param))
        except ValueError:
            bucket_seconds = _auto_bucket_seconds(days)
    else:
        bucket_seconds = _auto_bucket_seconds(days)

    return _get_db().get_activity_series(days=days, bucket_seconds=bucket_seconds)


def _auto_bucket_seconds(days: int) -> int:
    """Choose a bucket size that keeps the chart at ~24-100 points."""
    if days <= 1:
        return 3600           # hourly      -> 24 points
    if days <= 3:
        return 3 * 3600       # 3-hourly    -> 24 points
    if days <= 14:
        return 6 * 3600       # 6-hourly    -> ~28-56 points
    if days <= 90:
        return 86400          # daily       -> up to 90 points
    return 7 * 86400          # weekly      -> up to ~52 points


@router.get("/admin/api/logs")
async def admin_logs(request: Request, _=Depends(_require_admin)):
    """Return the last N lines of the proxy log."""
    lines = int(request.query_params.get("lines", "200"))
    log_file = LOG_DIR / "proxy.log"
    if not log_file.exists():
        return {"lines": []}
    with open(log_file) as f:
        all_lines = f.readlines()
    return {"lines": [l.rstrip() for l in all_lines[-lines:]]}


# --- Provider configuration ---

def _is_env_ref(value: str) -> bool:
    return isinstance(value, str) and value.startswith("${") and value.endswith("}")


def _annotate_api_key(value: str) -> dict:
    """Describe an api_key value without leaking its content.

    Env-ref values report whether the underlying env var is set; literal
    values report only their length and a 4-char prefix so the UI can hint
    at which key is configured without revealing the secret.
    """
    if not value:
        return {"type": "empty", "set": False, "display": ""}
    if _is_env_ref(value):
        env_name = value[2:-1]
        present = bool(os.environ.get(env_name))
        return {
            "type": "env",
            "set": present,
            "env_name": env_name,
            "display": value,
        }
    return {
        "type": "literal",
        "set": True,
        "length": len(value),
        "display": f"{value[:4]}â€¦({len(value)} chars)",
    }


def _build_provider_state() -> dict:
    """Snapshot of the live router + persisted config for the admin UI."""
    cfgs = _resolved_provider_configs()
    overridden = _get_db().get_config_value("providers") is not None
    default_model = _resolved_default_model()
    yaml_default = config.get("default_model", "")

    # Index live providers by name for runtime status.
    live_by_name = {p.name: p for p in state.router.providers}

    items: list[dict] = []
    for cfg in cfgs:
        name = cfg.get("name", "")
        live = live_by_name.get(name)
        items.append({
            "name": name,
            "base_url": cfg.get("base_url", ""),
            "api_key": _annotate_api_key(cfg.get("api_key", "")),
            "api_version": cfg.get("api_version", ""),
            "models": list(cfg.get("models", [])),
            "priority": cfg.get("priority", 10),
            "enabled": bool(cfg.get("enabled", True)),
            "live": {
                "loaded": live is not None,
                "available": live.available if live else False,
                "consecutive_failures": live._consecutive_failures if live else 0,
                "backoff_until": live._backoff_until if live else 0.0,
            },
        })

    all_models: list[str] = []
    seen_models: set[str] = set()
    for cfg in cfgs:
        for m in cfg.get("models") or []:
            if m and m not in seen_models:
                seen_models.add(m)
                all_models.append(m)

    return {
        "providers": items,
        "default_model": default_model,
        "yaml_default_model": yaml_default,
        "available_models": all_models,
        "overridden": overridden,
        "env_vars": _list_relevant_env_vars(items),
    }


def _list_relevant_env_vars(items: list[dict]) -> list[dict]:
    """Return env vars referenced by current providers + their set/unset state."""
    seen: set[str] = set()
    out: list[dict] = []
    for p in items:
        info = p.get("api_key") or {}
        if info.get("type") == "env":
            name = info.get("env_name", "")
            if name and name not in seen:
                seen.add(name)
                out.append({"name": name, "set": bool(os.environ.get(name))})
    return out


def _validate_provider_dict(p: dict) -> tuple[bool, str]:
    """Light validation; returns (ok, error_message)."""
    if not isinstance(p, dict):
        return False, "provider entry must be an object"
    name = (p.get("name") or "").strip()
    base_url = (p.get("base_url") or "").strip()
    if not name:
        return False, "name is required"
    if not base_url:
        return False, f"{name}: base_url is required"
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        return False, f"{name}: base_url must be http:// or https://"
    models = p.get("models") or []
    if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
        return False, f"{name}: models must be a list of strings"
    return True, ""


@router.get("/admin/api/providers")
async def admin_get_providers(request: Request, _=Depends(_require_admin)):
    return _build_provider_state()


@router.put("/admin/api/providers")
async def admin_update_providers(request: Request, _=Depends(_require_admin)):
    """Replace the persisted provider list and (optionally) default_model.

    Body: {"providers": [...], "default_model": "..."}.
    Triggers a router reload on success.
    """
    body = await request.json()
    providers = body.get("providers")
    if not isinstance(providers, list):
        raise HTTPException(400, "providers must be a list")

    cleaned: list[dict] = []
    for p in providers:
        ok, err = _validate_provider_dict(p)
        if not ok:
            raise HTTPException(400, err)
        cleaned.append({
            "name": p["name"].strip(),
            "base_url": p["base_url"].strip(),
            "api_key": (p.get("api_key") or "").strip(),
            "api_version": (p.get("api_version") or "").strip(),
            "models": [m.strip() for m in (p.get("models") or []) if m and m.strip()],
            "priority": int(p.get("priority", 10)),
            "enabled": bool(p.get("enabled", True)),
        })

    _get_db().set_config_value("providers", json.dumps(cleaned))

    if "default_model" in body:
        dm = (body.get("default_model") or "").strip()
        _get_db().set_config_value("default_model", dm)

    _reload_providers()
    return {"ok": True, "state": _build_provider_state()}


@router.post("/admin/api/providers/reload")
async def admin_reload_providers(request: Request, _=Depends(_require_admin)):
    """Re-read overrides + yaml and rebuild the router."""
    _reload_providers()
    return {"ok": True, "state": _build_provider_state()}


@router.post("/admin/api/providers/reset")
async def admin_reset_providers(request: Request, _=Depends(_require_admin)):
    """Drop SQLite overrides; revert to yaml-only config."""
    with _get_db().get_db() as conn:
        conn.execute("DELETE FROM proxy_config WHERE key IN ('providers', 'default_model')")
    _reload_providers()
    return {"ok": True, "state": _build_provider_state()}


@router.post("/admin/api/providers/test")
async def admin_test_provider(request: Request, _=Depends(_require_admin)):
    """Send a tiny chat completion to a named provider to verify connectivity."""
    body = await request.json()
    name = (body.get("name") or "").strip()
    model = (body.get("model") or "").strip()
    provider = next((p for p in state.router.providers if p.name == name), None)
    if not provider:
        raise HTTPException(404, f"provider '{name}' not loaded")

    test_model = model or (provider.models[0] if provider.models else "")
    if not test_model:
        raise HTTPException(400, f"provider '{name}' has no models configured")

    test_body = {
        "model": test_model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 4,
    }
    started = time.time()
    try:
        resp = await provider.forward_chat(test_body, state.http_client)
        elapsed_ms = (time.time() - started) * 1000
        ok = 200 <= resp.status_code < 300
        snippet = ""
        try:
            snippet = (resp.text or "")[:200]
        except Exception:
            snippet = ""
        if ok:
            provider.mark_success()
        else:
            provider.mark_failure()
        return {
            "ok": ok,
            "status_code": resp.status_code,
            "elapsed_ms": int(elapsed_ms),
            "model": test_model,
            "snippet": snippet,
        }
    except Exception as e:
        provider.mark_failure()
        return {
            "ok": False,
            "status_code": 0,
            "elapsed_ms": int((time.time() - started) * 1000),
            "model": test_model,
            "error": str(e),
        }


