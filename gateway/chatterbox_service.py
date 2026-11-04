"""R9700 speech service behind api.mtgacoach.com/v1/chatterbox/.

The installed Chatterbox models synthesize complete segments. Generate short
segments successively and stream PCM immediately, without collecting a whole
utterance. This is segment streaming, not token-level model inference.
"""

from __future__ import annotations

import hmac
import os
import re
import threading
import time
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


def segments(text, limit=100):
    """Prefer sentence boundaries, then whitespace; preserve every word."""
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        while len(sentence) > limit:
            boundary = sentence.rfind(" ", 0, limit + 1)
            if boundary < 1:
                boundary = limit
            yield sentence[:boundary]
            sentence = sentence[boundary:].lstrip()
        if sentence:
            yield sentence


def create_app(engine, *, auth_url="http://127.0.0.1:8444/v1/models", transport=None, legacy_key=""):
    app = FastAPI(title="MTGA Coach Chatterbox", docs_url=None, redoc_url=None)
    busy = threading.Lock()

    async def authorize(authorization):
        if not authorization.startswith("Bearer ") or len(authorization) > 4103:
            raise HTTPException(401, "A Coach key is required.")
        try:
            async with httpx.AsyncClient(transport=transport, timeout=3, follow_redirects=False) as client:
                result = await client.get(auth_url, headers={"Authorization": authorization})
            if result.status_code in (401, 403):
                raise HTTPException(result.status_code, "Your Coach key is not authorized.")
            if result.status_code != 200:
                raise HTTPException(503, "Coach authentication is unavailable.")
            if not any(model.get("id") == "current" for model in result.json().get("data", [])):
                raise HTTPException(403, "Your key does not include Coach access.")
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, "Coach authentication is unavailable.") from None

    @app.get("/health")
    def health():
        return {"status": "busy" if busy.locked() else "ready", "model": "hybrid", "streaming": "segments"}

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
            for audio, rate in engine._synthesize_pcm(
                segment,
                rate=body.speed,
                voice=body.voice,
                exaggeration=body.energy,
                excitement=body.excitement,
            ):
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
        if not busy.acquire(blocking=False):
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

    import uvicorn
    from arenaonair.tts_server import HybridSpeechEngine

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18767)
    args = parser.parse_args()
    key = os.environ.get("TTS_API_KEY", "")
    if not key and os.environ.get("TTS_API_KEY_FILE"):
        key = Path(os.environ["TTS_API_KEY_FILE"]).expanduser().read_text().strip()
    engine = HybridSpeechEngine(device=os.environ.get("TTS_DEVICE", "cuda"))
    if engine.preload_voices(["atlas", "mira"]) != 2 or not engine.warmup():
        raise RuntimeError("Speech model warmup failed")
    uvicorn.run(create_app(engine, legacy_key=key), host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
