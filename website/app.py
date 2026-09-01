"""mtgacoach.com proxy server — routes AI requests and manages subscriptions."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

import db
import httpx
import patreon
import providers
import state
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from routers.admin import router as admin_router
from routers.billing import router as billing_router
from routers.proxy import router as proxy_router

logger = state.logger

# Re-init state for this app runtime
state.init_app_state()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.http_client = httpx.AsyncClient(timeout=120.0)
    logger.info(f"Proxy server started with {len(state.router.providers)} providers")
    try:
        yield
    finally:
        if state.http_client:
            await state.http_client.aclose()


app = FastAPI(title="mtgacoach.com API", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log every request with timing and subscriber info."""
    start = time.time()
    key = ""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        key = auth[7:19] + "..."

    response = await call_next(request)
    elapsed_ms = (time.time() - start) * 1000

    path = request.url.path
    if path in ("/health", "/favicon.ico") or path.startswith("/static"):
        return response

    logger.info(
        f"{request.method} {path} → {response.status_code} "
        f"({elapsed_ms:.0f}ms) "
        f"key={key or 'none'} "
        f"ip={request.client.host if request.client else 'unknown'}"
    )
    return response


@app.get("/", response_class=HTMLResponse)
async def landing_page(request: Request):
    return state.templates.TemplateResponse(request=request, name="landing.html")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "providers": [
            {"name": p.name, "available": p.available, "models": p.models}
            for p in state.router.providers
        ],
    }


# Include modular routers
app.include_router(proxy_router)
app.include_router(billing_router)
app.include_router(admin_router)

# Compatibility exports for tests and direct module consumers
config = state.config
router = state.router
templates = state.templates
http_client = state.http_client
_response_store = state._response_store
_require_license = state._require_license
_require_admin = state._require_admin
_resolved_provider_configs = state._resolved_provider_configs
_resolved_default_model = state._resolved_default_model
_reload_providers = state._reload_providers
_extract_license_key = state._extract_license_key
_extract_client_metadata = state._extract_client_metadata
_record_client_telemetry = state._record_client_telemetry

if __name__ == "__main__":
    import uvicorn
    host = state.config.get("server", {}).get("host", "0.0.0.0")
    port = state.config.get("server", {}).get("port", 8443)
    uvicorn.run(app, host=host, port=port)
