"""R9700 speech service behind api.mtgacoach.com/v1/chatterbox/.

The installed Chatterbox models synthesize complete segments. Generate short
segments successively and stream PCM immediately, without collecting a whole
utterance. This is segment streaming, not token-level model inference.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import anyio
import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

MAX_AUDIO_BYTES = 20_000_000


class SpeechRequest(BaseModel):
    input: str = Field(min_length=1, max_length=600)
    voice: Literal["atlas", "mira", "default"] = "atlas"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    energy: float = Field(default=0.5, ge=0.0, le=1.0)
    excitement: Literal["calm", "normal", "tense", "electric"] | None = None


_CLAUSE_SEPARATORS = (", ", "; ", ": ", " — ", " - ")
_AUTH_CACHE_MAX = 512


def segments(text, limit=100, first_limit=64):
    """Prefer sentence boundaries, then clause/whitespace boundaries; preserve every word."""
    first = True
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        while sentence:
            active_limit = min(limit, first_limit) if first else limit
            if len(sentence) <= active_limit:
                yield sentence
                first = False
                break
            if first:
                best_split = -1
                for sep in _CLAUSE_SEPARATORS:
                    idx = sentence.rfind(sep, 0, active_limit)
                    token_end = idx + len(sep.rstrip())
                    if idx >= 20 and token_end <= active_limit and token_end > best_split:
                        best_split = token_end
                if best_split > 0:
                    yield sentence[:best_split]
                    sentence = sentence[best_split:].lstrip()
                    first = False
                    continue
            boundary = sentence.rfind(" ", 0, active_limit + 1)
            if boundary < 1:
                boundary = active_limit
            yield sentence[:boundary]
            sentence = sentence[boundary:].lstrip()
            first = False


def _resolve_dtype_name(raw_dtype: str | None, device: str) -> tuple[str | None, str]:
    canonical = (raw_dtype or "").strip().lower()
    if device.strip().lower() == "cpu" or canonical in ("", "none", "auto", "fp32", "float32"):
        return None, "none"
    aliases = {
        "bf16": "bfloat16",
        "bfloat16": "bfloat16",
        "fp16": "float16",
        "float16": "float16",
    }
    dtype_name = aliases.get(canonical)
    if dtype_name is None:
        return None, "none"
    return dtype_name, dtype_name


def build_engine(model_mode=None, device=None, dtype=None):
    """Construct the speech engine, defaulting to fast Turbo + bfloat16 on GPU."""
    mode = (model_mode or os.environ.get("TTS_MODEL") or "turbo").strip().lower()
    if mode not in ("turbo", "hybrid"):
        mode = "turbo"
    resolved_device = (device or os.environ.get("TTS_DEVICE") or "cuda").strip()
    raw_dtype = dtype if dtype is not None else os.environ.get("TTS_DTYPE", "bfloat16")
    dtype_arg, dtype_name = _resolve_dtype_name(raw_dtype, resolved_device)

    from arenaonair.platform.tts_chatterbox import ChatterboxEngine

    if mode == "hybrid":
        from arenaonair.tts_server import HybridSpeechEngine

        if dtype_arg is not None:
            try:
                engine = HybridSpeechEngine(
                    device=resolved_device,
                    factory=lambda **kw: ChatterboxEngine(dtype=dtype_arg, **kw),
                )
            except TypeError:
                engine = HybridSpeechEngine(device=resolved_device)
        else:
            engine = HybridSpeechEngine(device=resolved_device)
        return engine, mode, dtype_name

    for kwargs in (
        {"device": resolved_device, "model_variant": "turbo", "dtype": dtype_arg},
        {"device": resolved_device, "model_variant": "turbo"},
        {"device": resolved_device},
    ):
        try:
            engine = ChatterboxEngine(**kwargs)
            return engine, mode, dtype_name
        except TypeError:
            continue
    return ChatterboxEngine(resolved_device), mode, dtype_name


def _accepts_excitement(engine) -> bool:
    try:
        sig = inspect.signature(engine._synthesize_pcm)
    except (TypeError, ValueError, AttributeError):
        return True
    return "excitement" in sig.parameters or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )


def create_app(
    engine,
    *,
    auth_url="http://127.0.0.1:8444/v1/models",
    transport=None,
    legacy_key="",
    model_name="turbo",
    dtype_name="bfloat16",
    auth_cache_ttl=60.0,
    busy_wait_s=1.5,
):
    busy = threading.Lock()
    auth_client: httpx.AsyncClient | None = None
    auth_cache: dict[str, float] = {}
    accepts_excitement = _accepts_excitement(engine)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            if auth_client is not None:
                await auth_client.aclose()

    app = FastAPI(title="MTGA Coach Chatterbox", docs_url=None, redoc_url=None, lifespan=lifespan)

    async def authorize(authorization):
        nonlocal auth_client
        if not authorization.startswith("Bearer ") or len(authorization) > 4103:
            raise HTTPException(401, "A Coach key is required.")
        cache_key = ""
        now = time.monotonic()
        if auth_cache_ttl and auth_cache_ttl > 0:
            cache_key = hashlib.sha256(authorization.encode("utf-8")).hexdigest()
            expiry = auth_cache.get(cache_key)
            if expiry is not None and expiry > now:
                return
            auth_cache.pop(cache_key, None)
        if auth_client is None:
            auth_client = httpx.AsyncClient(transport=transport, timeout=3, follow_redirects=False)
        try:
            result = await auth_client.get(auth_url, headers={"Authorization": authorization})
            if result.status_code in (401, 403):
                raise HTTPException(result.status_code, "Your Coach key is not authorized.")
            if result.status_code != 200:
                raise HTTPException(503, "Coach authentication is unavailable.")
            if not any(model.get("id") == "current" for model in result.json().get("data", [])):
                raise HTTPException(403, "Your key does not include Coach access.")
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, "Coach authentication is unavailable.") from None
        if cache_key:
            if len(auth_cache) >= _AUTH_CACHE_MAX:
                expired = [k for k, exp in auth_cache.items() if exp <= now]
                for k in expired:
                    auth_cache.pop(k, None)
                if len(auth_cache) >= _AUTH_CACHE_MAX:
                    auth_cache.pop(next(iter(auth_cache)), None)
            auth_cache[cache_key] = now + float(auth_cache_ttl)

    @app.get("/health")
    def health():
        return {
            "status": "busy" if busy.locked() else "ready",
            "model": model_name,
            "dtype": dtype_name,
            "streaming": "segments",
        }

    @app.get("/v1/chatterbox/voices")
    async def voices(authorization: str = Header(default="")):
        await authorize(authorization)
        return {"voices": ["atlas", "mira", "default"], "default": "atlas"}

    def pcm(body):
        import numpy as np

        total, deadline = 0, time.monotonic() + 60
        for segment in segments(body.input):
            if time.monotonic() >= deadline:
                raise RuntimeError("Speech deadline exceeded")
            synth_kwargs = {
                "rate": body.speed,
                "voice": body.voice,
                "exaggeration": body.energy,
            }
            if accepts_excitement:
                synth_kwargs["excitement"] = body.excitement
            for audio, rate in engine._synthesize_pcm(segment, **synth_kwargs):
                if rate != 24000 or not np.isfinite(audio).all():
                    raise ValueError("Invalid audio")
                data = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
                total += len(data)
                if not data or total > MAX_AUDIO_BYTES:
                    raise ValueError("Invalid audio length")
                for offset in range(0, len(data), 9600):
                    yield data[offset : offset + 9600]

    async def respond(body, request):
        if not body.input.strip():
            raise HTTPException(422, "Speech text is empty.")
        acquired = busy.acquire(blocking=False)
        if not acquired and busy_wait_s and busy_wait_s > 0:
            deadline = time.monotonic() + float(busy_wait_s)
            while time.monotonic() < deadline:
                if await request.is_disconnected():
                    raise HTTPException(499, "Client disconnected.")
                await anyio.sleep(0.05)
                acquired = busy.acquire(blocking=False)
                if acquired:
                    break
        if not acquired:
            raise HTTPException(503, "Speech model busy.", headers={"Retry-After": "2"})
        iterator = pcm(body)
        try:
            first = await anyio.to_thread.run_sync(lambda: next(iterator, b""))
            if not first:
                raise ValueError("Empty audio")
        except BaseException:
            iterator.close()
            busy.release()
            raise HTTPException(503, "Speech generation failed.") from None

        async def stream():
            try:
                yield first
                while not await request.is_disconnected():
                    chunk = await anyio.to_thread.run_sync(lambda: next(iterator, b""))
                    if not chunk:
                        break
                    yield chunk
            finally:
                iterator.close()
                busy.release()

        return StreamingResponse(
            stream(),
            media_type="audio/pcm",
            headers={
                "X-Sample-Rate": "24000",
                "X-TTS-Device": "r9700",
                "X-TTS-Streaming": "segments",
                "Cache-Control": "no-store, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/v1/chatterbox/speech")
    async def speech(body: SpeechRequest, request: Request, authorization: str = Header(default="")):
        await authorize(authorization)
        return await respond(body, request)

    # Keep the existing ArenaOnAir LAN protocol working during Coach migration.
    @app.post("/v1/audio/speech")
    async def legacy(body: SpeechRequest, request: Request, authorization: str = Header(default="")):
        if not legacy_key or not hmac.compare_digest(authorization, "Bearer " + legacy_key):
            raise HTTPException(401, "Invalid speech server key.")
        return await respond(body, request)

    return app


def main():
    import argparse

    os.environ.setdefault("TQDM_DISABLE", "1")
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18767)
    args = parser.parse_args()
    key = os.environ.get("TTS_API_KEY", "")
    if not key and os.environ.get("TTS_API_KEY_FILE"):
        key = Path(os.environ["TTS_API_KEY_FILE"]).expanduser().read_text().strip()
    engine, model_name, dtype_name = build_engine()
    ready = engine.preload_voices(["atlas", "mira"]) == 2 and engine.warmup()
    if not ready and dtype_name != "none":
        logging.getLogger(__name__).warning(
            "Speech warmup failed with dtype=%s; retrying in float32", dtype_name
        )
        engine, model_name, dtype_name = build_engine(model_mode=model_name, dtype="none")
        ready = engine.preload_voices(["atlas", "mira"]) == 2 and engine.warmup()
    if not ready:
        raise RuntimeError("Speech model warmup failed")
    uvicorn.run(
        create_app(engine, legacy_key=key, model_name=model_name, dtype_name=dtype_name),
        host=args.host,
        port=args.port,
        workers=1,
    )


if __name__ == "__main__":
    main()
