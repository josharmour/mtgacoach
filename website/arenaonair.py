"""ArenaOnAir's five-match trial. Gateway credentials never leave this server.

Separate from MTGA Coach's seven-day trial and Patreon key lifecycle.
Match IDs are supplied by the Arena client; bounded sessions also limit replay
of an old ID by modified clients. New trials require email verification.
"""
from __future__ import annotations

import asyncio
import secrets
import hashlib
import hmac
import json
import os
import re
import time

import db
import httpx
import patreon
from abuse_protection import network_identity, reserve_code_request
from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/arenaonair")
MATCH_LIMIT = 5
SESSION_SECONDS = 4 * 3600
REQUEST_LIMIT = 1200
MODEL = os.environ.get("ARENAONAIR_MODEL", "glm-5.3-flash")
SUBSCRIBE_URL = "https://mtgacoach.com/subscribe"
CODE_TTL_SECONDS = 900
MAX_CODE_ATTEMPTS = 5


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
        CREATE TABLE IF NOT EXISTS arenaonair_trial_codes (
            device_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, email TEXT NOT NULL,
            code_hash TEXT NOT NULL, expires REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS arenaonair_trial_ip ON arenaonair_trials(ip_hash, created);
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(arenaonair_trials)")}
        if "email" not in columns:
            conn.execute("ALTER TABLE arenaonair_trials ADD COLUMN email TEXT")
        if "email_verified" not in columns:
            conn.execute("ALTER TABLE arenaonair_trials ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")


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


def _device(body):
    device = body.get("device_id", "")
    if not isinstance(device, str) or not re.fullmatch(r"[0-9a-f]{64}", device):
        raise HTTPException(422, "Invalid device identifier")
    return device


def _email(body):
    email = body.get("email", "")
    if not isinstance(email, str):
        raise HTTPException(422, "invalid_email")
    email = email.strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(422, "invalid_email")
    return email


def _code_hash(request, device, email, code):
    # The high-entropy possession token is never stored, only its hash. Using
    # it as the HMAC key also binds the email challenge to this installation.
    token = request.headers["authorization"].removeprefix("Bearer ")
    return hmac.new(token.encode(), f"{device}:{email}:{code}".encode(), hashlib.sha256).hexdigest()


@router.post("/trial/request-code")
async def request_trial_code(request: Request):
    body = await _body(request)
    device, email = _device(body), _email(body)
    token_hash = _identity(request)
    network = network_identity(request)
    with db.get_db() as conn:
        existing = conn.execute("SELECT token_hash FROM arenaonair_trials WHERE device_id=?", (device,)).fetchone()
        if existing and not hmac.compare_digest(existing[0], token_hash):
            raise HTTPException(409, "This device already has a trial.")
    # Keep the per-device namespace separate from Coach; share network limits.
    reserve_code_request(db.get_db, "arenaonair:" + device, network)
    if not getattr(patreon, "SMTP_HOST", ""):
        raise HTTPException(503, "email_delivery_unavailable")
    code = f"{secrets.randbelow(1_000_000):06d}"
    code_hash = _code_hash(request, device, email, code)
    with db.get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO arenaonair_trial_codes "
                     "(device_id,token_hash,email,code_hash,expires,attempts) VALUES(?,?,?,?,?,0)",
                     (device, token_hash, email, code_hash, time.time() + CODE_TTL_SECONDS))
    try:
        await asyncio.to_thread(patreon.send_trial_code_email, email, code, product="ArenaOnAir")
    except Exception:
        with db.get_db() as conn:
            # A failed old send must not delete a newer request's challenge.
            conn.execute("DELETE FROM arenaonair_trial_codes WHERE device_id=? AND code_hash=?",
                         (device, code_hash))
        raise HTTPException(503, "email_delivery_unavailable") from None
    return {"status": "code_sent", "expires_in": CODE_TTL_SECONDS}


@router.post("/trial")
async def trial(request: Request):
    body = await _body(request)
    device = _device(body)
    token_hash = _identity(request)
    invalid_code = False
    with db.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT token_hash FROM arenaonair_trials WHERE device_id=?", (device,)).fetchone()
        if row:
            if not hmac.compare_digest(row[0], token_hash):
                raise HTTPException(409, "This device already has a trial. Restore its connection file or use a Patreon key.")
            return _status(conn, token_hash)  # grandfather existing possession-authenticated trials
        if not body.get("email") or not body.get("code"):
            raise HTTPException(422, "email_verification_required")
        email = _email(body)
        code = body.get("code")
        pending = conn.execute("SELECT * FROM arenaonair_trial_codes WHERE device_id=?", (device,)).fetchone()
        valid = (isinstance(code, str) and re.fullmatch(r"[0-9]{6}", code) and pending
                 and pending["expires"] > time.time() and pending["attempts"] < MAX_CODE_ATTEMPTS
                 and pending["email"] == email and hmac.compare_digest(pending["token_hash"], token_hash)
                 and hmac.compare_digest(pending["code_hash"], _code_hash(request, device, email, code)))
        if not valid:
            if pending:
                conn.execute("UPDATE arenaonair_trial_codes SET attempts=attempts+1 WHERE device_id=?", (device,))
            invalid_code = True
        else:
            ip_hash = network_identity(request)
            recent = conn.execute("SELECT count(*) FROM arenaonair_trials WHERE ip_hash=? AND created>?",
                                  (ip_hash, time.time() - 86400)).fetchone()[0]
            if recent >= 10:
                raise HTTPException(429, "Trial signup limit reached. Try again tomorrow.")
            conn.execute("INSERT INTO arenaonair_trials "
                         "(device_id,token_hash,created,ip_hash,email,email_verified) VALUES(?,?,?,?,?,1)",
                         (device, token_hash, time.time(), ip_hash, email))
            conn.execute("DELETE FROM arenaonair_trial_codes WHERE device_id=?", (device,))
            result = _status(conn, token_hash)
    # Raise AFTER the transaction commits so failed attempts cannot roll back.
    if invalid_code:
        raise HTTPException(422, "invalid_code")
    return result


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
               "chat_template_kwargs": {"thinking": False}}
    options = body.get("chat_template_kwargs")
    if isinstance(options, dict) and options.get("thinking") is True:
        payload["chat_template_kwargs"] = {"thinking": True, "reasoning_effort": "low"}
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
                                              json=payload, headers={"Authorization": "Bearer " + os.environ["LITELLM_ARENAONAIR_KEY"]})
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
