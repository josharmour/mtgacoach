"""ArenaOnAir's five-match trial. Gateway credentials never leave this server.

Separate from MTGA Coach's seven-day trial and Patreon key lifecycle.
Match IDs are supplied by the Arena client; bounded sessions also limit replay
of an old ID by modified clients. This is a device trial, not verified identity.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time

import db
import httpx
import patreon
from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/arenaonair")
MATCH_LIMIT = 5
SESSION_SECONDS = 4 * 3600
REQUEST_LIMIT = 1200
MODEL = os.environ.get("ARENAONAIR_MODEL", "glm-5.3-flash")
SUBSCRIBE_URL = "https://mtgacoach.com/subscribe"


def init_db():
    with db.get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS arenaonair_trials (
            device_id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
            created REAL NOT NULL, ip_hash TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS arenaonair_matches (
            token_hash TEXT NOT NULL, match_id TEXT NOT NULL,
            started REAL NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
            minute INTEGER NOT NULL DEFAULT 0, minute_requests INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(token_hash, match_id)
        );
        CREATE INDEX IF NOT EXISTS arenaonair_trial_ip ON arenaonair_trials(ip_hash, created);
        """)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _identity(request):
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    if not re.fullmatch(r"aoa_[A-Za-z0-9_-]{43,64}", token):
        raise HTTPException(401, "Connect your free trial in ArenaOnAir.")
    return _digest(token)


def _status(conn, token_hash):
    if not conn.execute("SELECT 1 FROM arenaonair_trials WHERE token_hash=?", (token_hash,)).fetchone():
        raise HTTPException(401, "Trial connection not found. Reconnect in the app.")
    used = conn.execute("SELECT count(*) FROM arenaonair_matches WHERE token_hash=?", (token_hash,)).fetchone()[0]
    return {"matches_used": used, "matches_remaining": max(0, MATCH_LIMIT - used),
            "match_limit": MATCH_LIMIT, "model": MODEL, "subscribe_url": SUBSCRIBE_URL}


async def _body(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 131072:
            raise HTTPException(413, "Request too large")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Invalid JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected a JSON object")
    return body


@router.post("/trial")
async def trial(request: Request):
    body = await _body(request)
    device = body.get("device_id", "")
    if not isinstance(device, str) or not re.fullmatch(r"[0-9a-f]{64}", device):
        raise HTTPException(422, "Invalid device identifier")
    token_hash = _identity(request)
    # Only a digest of the address is retained; daily signup throttle, not identity proof.
    ip_hash = _digest(request.headers.get("cf-connecting-ip") or
                      (request.client.host if request.client else "unknown"))
    with db.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT token_hash FROM arenaonair_trials WHERE device_id=?", (device,)).fetchone()
        if row:
            if not hmac.compare_digest(row[0], token_hash):
                raise HTTPException(409, "This device already has a trial. Restore its connection file or use a Patreon key.")
        else:
            recent = conn.execute("SELECT count(*) FROM arenaonair_trials WHERE ip_hash=? AND created>?",
                                  (ip_hash, time.time() - 86400)).fetchone()[0]
            if recent >= 10:
                raise HTTPException(429, "Trial signup limit reached. Try again tomorrow.")
            conn.execute("INSERT INTO arenaonair_trials VALUES (?,?,?,?)", (device, token_hash, time.time(), ip_hash))
        return _status(conn, token_hash)


@router.get("/status")
async def status(request: Request):
    with db.get_db() as conn:
        return _status(conn, _identity(request))


@router.get("/v1/models")
async def models(request: Request):
    await status(request)
    return {"object": "list", "data": [{"id": MODEL, "object": "model"}]}


@router.post("/v1/chat/completions")
async def completion(request: Request):
    token_hash = _identity(request)
    match = request.headers.get("x-arenaonair-match", "")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", match):
        raise HTTPException(422, "A live Arena match is required.")
    body = await _body(request)
    messages = body.get("messages")
    if (not isinstance(messages, list) or not 1 <= len(messages) <= 8 or
            any(not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant") or
                not isinstance(m.get("content"), str) for m in messages)):
        raise HTTPException(422, "Invalid messages")
    # An allowlist keeps client-controlled routing, callbacks and credentials out.
    payload = {"model": MODEL, "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
               "max_tokens": 1800, "temperature": 0.4,
               "chat_template_kwargs": {"thinking": True, "reasoning_effort": "low"}}
    if isinstance(body.get("response_format"), dict):
        payload["response_format"] = body["response_format"]
    if body.get("temperature") == 0:
        payload["temperature"] = 0
    with db.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = _status(conn, token_hash)
        row = conn.execute("SELECT * FROM arenaonair_matches WHERE token_hash=? AND match_id=?",
                           (token_hash, match)).fetchone()
        now = time.time()
        minute = int(now // 60)
        if row is None:
            if current["matches_remaining"] <= 0:
                raise HTTPException(402, "Your five free matches are used. Subscribe through Patreon or connect your own provider.")
            conn.execute("INSERT INTO arenaonair_matches(token_hash,match_id,started) VALUES(?,?,?)",
                         (token_hash, match, now))
        elif now - row["started"] >= SESSION_SECONDS or row["requests"] >= REQUEST_LIMIT:
            raise HTTPException(402, "This trial match session has ended. Subscribe or connect your own provider.")
        elif row["minute"] == minute and row["minute_requests"] >= 60:
            raise HTTPException(429, "Please wait before requesting more commentary.")
        conn.execute("UPDATE arenaonair_matches SET requests=requests+1, minute=?, "
                     "minute_requests=CASE WHEN minute=? THEN minute_requests+1 ELSE 1 END "
                     "WHERE token_hash=? AND match_id=?", (minute, minute, token_hash, match))
    try:
        response = await patreon._http().post(patreon.LITELLM_URL + "/v1/chat/completions",
                                              json=payload, headers=patreon._admin_headers())
        if response.status_code != 200:
            raise RuntimeError("gateway unavailable")
        result = response.json()
        if not isinstance(result.get("choices"), list):
            raise ValueError("missing choices")
    except (httpx.HTTPError, RuntimeError, ValueError, KeyError):
        # A failed first request must not spend a free match. Concurrent successful
        # requests preserve the row; decrement only this request's reservation.
        with db.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE arenaonair_matches SET requests=requests-1 WHERE token_hash=? AND match_id=?",
                         (token_hash, match))
            conn.execute("DELETE FROM arenaonair_matches WHERE token_hash=? AND match_id=? AND requests=0",
                         (token_hash, match))
        raise HTTPException(503, "Hosted commentary is temporarily unavailable. Please retry.") from None
    return {k: result[k] for k in ("id", "object", "created", "choices", "usage", "model") if k in result}


init_db()
