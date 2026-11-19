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

import requests

VOICES = ("atlas", "mira", "default", "server_default")
MAX_AUDIO_BYTES = 20_000_000
HOSTED_BASE = "https://api.mtgacoach.com/v1/chatterbox"
_SESSION_LOCAL = threading.local()


def _http_session() -> requests.Session:
    session = getattr(_SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        _SESSION_LOCAL.session = session
    return session


def hosted_base_url(direct_url: str = "") -> str:
    """Resolve and validate the Chatterbox base URL (optional direct LAN override)."""
    raw = (direct_url or os.environ.get("ARENAMCP_CHATTERBOX_URL", "") or "").strip()
    if not raw:
        return HOSTED_BASE
    try:
        parsed = urllib.parse.urlsplit(raw.rstrip("/"))
        port = parsed.port
    except ValueError:
        raise ChatterboxError(
            "Use an HTTP(S) Chatterbox URL without embedded credentials or query parameters."
        ) from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ChatterboxError(
            "Use an HTTP(S) Chatterbox URL without embedded credentials or query parameters."
        )
    path = parsed.path.rstrip("/")
    if path.endswith("/speech") or path.endswith("/voices"):
        path = path[:-7]
    if not path:
        path = "/v1/chatterbox"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _hosted_headers(api_key: str) -> dict[str, str]:
    if not isinstance(api_key, str) or not api_key.strip() or any(c in api_key for c in "\r\n"):
        raise ChatterboxError("Sign in to Coach to use Chatterbox.")
    return {
        "Authorization": "Bearer " + api_key.strip(),
        "Content-Type": "application/json",
        "User-Agent": "MTGA-Coach",
    }


def _hosted_request(path, api_key, payload=None, *, base_url=""):
    return urllib.request.Request(
        hosted_base_url(base_url) + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=_hosted_headers(api_key),
    )


def hosted_voices(api_key, *, opener=None, base_url=""):
    try:
        headers = _hosted_headers(api_key)
        resolved_base = hosted_base_url(base_url)
        if opener is not None:
            request = urllib.request.Request(resolved_base + "/voices", headers=headers)
            with opener.open(request, timeout=3) as response:
                value = json.loads(response.read(16385))
        else:
            response = _http_session().get(
                resolved_base + "/voices",
                headers=headers,
                timeout=3,
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise ValueError()
            value = json.loads(response.content[:16385])
        voices = value.get("voices")
        if (
            not isinstance(voices, list)
            or not voices
            or any(voice not in VOICES[:3] for voice in voices)
            or value.get("default") not in voices
        ):
            raise ValueError()
        return {"voices": voices, "default": value["default"]}
    except ChatterboxError:
        raise
    except Exception:
        raise ChatterboxError(
            "Can't load Chatterbox voices. Check your connection and Coach sign-in."
        ) from None


def _iter_aligned_pcm(chunks, check):
    total, pending = 0, b""
    for data in chunks:
        check()
        if not data:
            continue
        total += len(data)
        if total > MAX_AUDIO_BYTES:
            raise ChatterboxError("Speech server returned too much audio.")
        pending += data
        aligned = len(pending) // 2 * 2
        if aligned:
            yield pending[:aligned]
            pending = pending[aligned:]
    if not total or pending:
        raise ChatterboxError("Speech server returned empty or incomplete audio.")


def stream_hosted(
    *,
    api_key,
    text,
    voice="server_default",
    speed=1.0,
    cancelled=None,
    opener=None,
    excitement="normal",
    energy=0.5,
    base_url="",
):
    """Yield aligned 24 kHz PCM immediately; optional direct URL override only via base_url/env."""
    if not text.strip() or len(text) > 600 or voice not in VOICES:
        raise ChatterboxError("Chatterbox requires 1–600 characters and an available voice.")
    if not math.isfinite(speed) or not 0.5 <= speed <= 2:
        raise ChatterboxError("Invalid speech speed.")
    if (
        excitement not in {"calm", "normal", "tense", "electric"}
        or not math.isfinite(energy)
        or not 0 <= energy <= 1
    ):
        raise ChatterboxError("Invalid speech delivery.")
    deadline = time.monotonic() + 60

    def check():
        if cancelled is not None and cancelled.is_set():
            raise SpeechCancelled("Speech cancelled.")
        if time.monotonic() >= deadline:
            raise ChatterboxError("Speech server missed its deadline.")

    payload = {"input": text, "speed": speed, "energy": energy, "excitement": excitement}
    if voice != "server_default":
        payload["voice"] = voice
    check()
    headers = _hosted_headers(api_key)
    resolved_base = hosted_base_url(base_url)

    if opener is not None:
        request = urllib.request.Request(
            resolved_base + "/speech",
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        try:
            with opener.open(request, timeout=6) as response:
                if (
                    response.headers.get_content_type() != "audio/pcm"
                    or response.headers.get("X-Sample-Rate") != "24000"
                ):
                    raise ChatterboxError("Speech server returned an unsupported audio format.")
                read = getattr(response, "read1", response.read)

                def _urllib_reads():
                    while True:
                        check()
                        data = read(9600)
                        if not data:
                            break
                        yield data

                yield from _iter_aligned_pcm(_urllib_reads(), check)
        except ChatterboxError:
            raise
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise ChatterboxError("Sign in with an active Coach key to use Chatterbox.") from None
            if error.code in (429, 503):
                raise ChatterboxError("Chatterbox is busy. Try again shortly.") from None
            raise ChatterboxError("Chatterbox request failed.") from None
        except Exception:
            raise ChatterboxError("Chatterbox connection interrupted.") from None
        return

    try:
        with _http_session().post(
            resolved_base + "/speech",
            headers=headers,
            json=payload,
            timeout=(4, 6),
            stream=True,
            allow_redirects=False,
        ) as response:
            if response.status_code in (401, 403):
                raise ChatterboxError("Sign in with an active Coach key to use Chatterbox.")
            if response.status_code in (429, 503):
                raise ChatterboxError("Chatterbox is busy. Try again shortly.")
            if response.status_code >= 300:
                raise ChatterboxError("Chatterbox request failed.")
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type != "audio/pcm" or response.headers.get("X-Sample-Rate") != "24000":
                raise ChatterboxError("Speech server returned an unsupported audio format.")

            def _session_reads():
                for chunk in response.iter_content(chunk_size=9600):
                    check()
                    if chunk:
                        yield chunk

            yield from _iter_aligned_pcm(_session_reads(), check)
    except ChatterboxError:
        raise
    except Exception:
        raise ChatterboxError("Chatterbox connection interrupted.") from None


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
    excitement: str | None = None,
    timeout: float = 4.0,
    cancelled: threading.Event | None = None,
    opener=None,
    clock=time.monotonic,
) -> bytes:
    """Return validated WAV bytes. Never retry busy responses or return stale audio.

    The timeout bounds individual socket operations; the monotonic deadline also
    rejects a slow/trickling response. Cancellation is checked between reads.
    """
    from .commentary.delivery import ENERGY

    if excitement is not None and excitement not in ENERGY:
        raise ChatterboxError("Invalid speech excitement.")
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
    if excitement is not None:
        payload.update(excitement=excitement, energy=ENERGY[excitement])
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


def speech_chunks(text: str, limit: int = 100) -> list[str]:
    """Split advice at whitespace into short requests that fit the speech deadline."""
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
