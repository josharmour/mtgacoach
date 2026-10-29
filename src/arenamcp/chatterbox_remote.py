"""Arena On Air's remote Chatterbox protocol, with no model/audio dependencies.

Server contract: POST /v1/audio/speech, mono 24 kHz signed little-endian
16-bit PCM. Coach remains the only playback owner. Cancellation discards local
output; the current server cannot abort GPU synthesis.
"""

from __future__ import annotations

import io
import json
import math
import os
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

VOICES = ("atlas", "mira", "default", "server_default")
MAX_AUDIO_BYTES = 20_000_000


class ChatterboxError(RuntimeError):
    """Redacted, safe-to-display provider failure."""


class SpeechCancelled(ChatterboxError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def speech_endpoint(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value.strip().rstrip("/"))
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
        ):
            raise ValueError()
    except ValueError:
        raise ChatterboxError(
            "Use an HTTP(S) speech endpoint without embedded credentials or query parameters."
        ) from None
    path = parsed.path or "/v1"
    if not path.endswith("/audio/speech"):
        path += "/audio/speech"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def read_speech_key(key_file: str) -> str:
    try:
        path = Path(key_file).expanduser()
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode) or (os.name != "nt" and mode & 0o077):
            raise ValueError()
        with path.open(encoding="utf-8") as stream:
            key = stream.read(4097).strip()
        if not key or len(key) > 4096 or "\n" in key or "\r" in key:
            raise ValueError()
        return key
    except (OSError, ValueError, UnicodeError):
        raise ChatterboxError(
            "Choose a readable speech-only key file (permissions 0600 on macOS/Linux)."
        ) from None


def render_remote(
    *,
    endpoint: str,
    key_file: str,
    text: str,
    voice: str = "server_default",
    speed: float = 1.0,
    timeout: float = 4.0,
    cancelled: threading.Event | None = None,
    opener=None,
    clock=time.monotonic,
) -> bytes:
    """Return validated WAV bytes. Never retry busy responses or return stale audio.

    The timeout bounds individual socket operations; the monotonic deadline also
    rejects a slow/trickling response. Cancellation is checked between reads.
    """
    if not text.strip() or len(text) > 600 or voice not in VOICES:
        raise ChatterboxError("Chatterbox requires 1–600 characters and an Atlas, Mira, or Default voice.")
    if (
        not math.isfinite(speed)
        or not 0.5 <= speed <= 2
        or not math.isfinite(timeout)
        or not 0 < timeout <= 6
    ):
        raise ChatterboxError("Invalid Chatterbox speed or deadline.")
    deadline = clock() + timeout

    def check() -> None:
        if cancelled is not None and cancelled.is_set():
            raise SpeechCancelled("Speech cancelled.")
        if clock() >= deadline:
            raise ChatterboxError("Speech server missed its deadline.")

    check()
    url = speech_endpoint(endpoint)
    key = read_speech_key(key_file)
    payload = {"input": text, "speed": speed, "energy": 0.5}
    if voice != "server_default":
        payload["voice"] = voice
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    try:
        check()
        with (opener or urllib.request.build_opener(_NoRedirect)).open(request, timeout=timeout) as response:
            if (
                response.headers.get_content_type() != "audio/pcm"
                or response.headers.get("X-Sample-Rate") != "24000"
            ):
                raise ChatterboxError("Speech server returned an unsupported audio format.")
            chunks = []
            size = 0
            read = getattr(response, "read1", response.read)
            while True:
                check()
                chunk = read(min(65536, MAX_AUDIO_BYTES + 1 - size))
                check()
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_AUDIO_BYTES:
                    raise ChatterboxError("Speech server returned too much audio.")
            raw = b"".join(chunks)
        if not raw or len(raw) % 2:
            raise ChatterboxError("Speech server returned empty or incomplete audio.")
        check()
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(raw)
        check()
        return output.getvalue()
    except ChatterboxError:
        raise
    except urllib.error.HTTPError as error:
        if error.code == 503:
            raise ChatterboxError("Speech server is busy. Try again shortly.") from None
        raise ChatterboxError(
            "Speech server rejected the request; check its endpoint and speech key."
        ) from None
    except Exception:
        raise ChatterboxError("Speech server unavailable or returned invalid audio.") from None


def speech_chunks(text: str, limit: int = 500) -> list[str]:
    """Split Coach advice at whitespace to fit the server's per-request limit."""
    remaining, chunks = text.strip(), []
    while len(remaining) > limit:
        boundary = remaining.rfind(" ", 0, limit + 1)
        if boundary < limit // 2:
            boundary = limit
        chunks.append(remaining[:boundary])
        remaining = remaining[boundary:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def join_speech_audio(parts: list[bytes]) -> bytes:
    """Join validated mono PCM WAV chunks into one desktop-owned playback."""
    if len(parts) == 1:
        return parts[0]
    output = io.BytesIO()
    size = 0
    with wave.open(output, "wb") as destination:
        destination.setnchannels(1)
        destination.setsampwidth(2)
        destination.setframerate(24000)
        for part in parts:
            with wave.open(io.BytesIO(part)) as source:
                if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 24000):
                    raise ChatterboxError("Speech renderer returned an unsupported audio format.")
                frames = source.readframes(source.getnframes())
            size += len(frames)
            if size > MAX_AUDIO_BYTES:
                raise ChatterboxError("Speech renderer returned too much audio.")
            destination.writeframes(frames)
    return output.getvalue()
