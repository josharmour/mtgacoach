"""Read existing speech setup without starting services or changing preferences."""

from __future__ import annotations

import importlib.util
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from arenamcp.chatterbox_remote import ChatterboxError, _NoRedirect, read_speech_key, speech_endpoint
from arenamcp.commentary.local_voice_worker import validate_model

VOICE_LABELS = {
    "atlas": "Atlas · US English male",
    "mira": "Mira · US English female",
    "default": "Chatterbox original voice",
}


@dataclass
class SpeechSetup:
    values: dict
    server_configured: bool
    local_configured: bool
    announcers_available: bool
    prefer_server: bool


def discover(settings, *, home=None, repository=None):
    home = Path(home) if home is not None else Path.home()
    repository = Path(repository) if repository is not None else Path(__file__).resolve().parents[3]
    aoa = {}
    try:
        path = home / ".arenaonair/config.toml"
        if path.stat().st_size <= 262144:
            aoa = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    keys = (
        "commentary_python",
        "commentary_aoa_source",
        "chatterbox_server_url",
        "chatterbox_key_file",
        "chatterbox_local_python",
        "chatterbox_model_dir",
        "chatterbox_model_revision",
        "chatterbox_device",
    )
    values = {key: str(settings.get(key) or "") for key in keys}
    for key, old in (
        ("chatterbox_server_url", "tts_server_url"),
        ("chatterbox_key_file", "tts_server_key_file"),
    ):
        if not values[key] and isinstance(aoa.get(old), str):
            values[key] = aoa[old]
    source = (
        Path(values["commentary_aoa_source"]).expanduser()
        if values["commentary_aoa_source"]
        else repository.parent / "arenaonair"
    )
    source_available = any((root / "arenaonair/llm_booth.py").is_file() for root in (source, source / "src"))
    if source_available:
        values["commentary_aoa_source"] = str(source)
    else:
        try:
            source_available = importlib.util.find_spec("arenaonair") is not None
        except (ImportError, ValueError):
            source_available = False
    server = False
    try:
        values["chatterbox_server_url"] = speech_endpoint(values["chatterbox_server_url"])
        read_speech_key(values["chatterbox_key_file"])
        server = True
    except ChatterboxError:
        pass
    if not values["chatterbox_local_python"]:
        for candidate in (
            home / "venvs/coach-chatterbox/bin/python",
            repository / "runtime/chatterbox/.venv/bin/python",
            source / ".venv/bin/python",
        ):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                values["chatterbox_local_python"] = str(candidate)
                break
    revision = values["chatterbox_model_revision"] or "749d1c1a46eb10492095d68fbcf55691ccf137cd"
    values["chatterbox_model_revision"] = revision
    if not values["chatterbox_model_dir"]:
        for candidate in (
            home / "models/coach-chatterbox" / revision,
            home / ".cache/huggingface/hub/models--ResembleAI--chatterbox-turbo/snapshots" / revision,
        ):
            if candidate.is_dir():
                values["chatterbox_model_dir"] = str(candidate)
                break
    local = False
    try:
        executable = Path(values["chatterbox_local_python"]).expanduser()
        validate_model(values["chatterbox_model_dir"], revision)
        local = (
            source_available
            and bool(values["chatterbox_local_python"])
            and executable.is_file()
            and os.access(executable, os.X_OK)
        )
    except ValueError:
        pass
    values["chatterbox_device"] = values["chatterbox_device"] or "cpu"
    return SpeechSetup(
        values,
        server,
        local,
        source_available,
        settings.get("tts_provider") == "chatterbox_remote" or aoa.get("tts_backend") == "remote",
    )


def server_voices(endpoint, *, opener=None):
    """Use the actual speech request schema, including its omitted-voice default."""
    parsed = urllib.parse.urlsplit(speech_endpoint(endpoint))
    url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/openapi.json", "", ""))
    try:
        with (opener or urllib.request.build_opener(_NoRedirect)).open(url, timeout=3) as response:
            raw = response.read(262145)
            if len(raw) > 262144:
                raise ValueError()
            schema = json.loads(raw)
        body = schema["paths"]["/v1/audio/speech"]["post"]["requestBody"]["content"]["application/json"][
            "schema"
        ]
        if "$ref" in body:
            prefix = "#/components/schemas/"
            if not body["$ref"].startswith(prefix):
                raise ValueError()
            body = schema["components"]["schemas"][body["$ref"][len(prefix) :]]
        voice = body["properties"]["voice"]
        choices = voice["enum"]
        if (
            not isinstance(choices, list)
            or not choices
            or any(value not in VOICE_LABELS for value in choices)
        ):
            raise ValueError()
        if "voice" in body.get("required", []):
            raise ValueError()
        return {"voices": choices, "default": voice.get("default")}
    except Exception:
        raise ChatterboxError(
            "Can't load server voices. Check that you're connected to the same network as your speech server, then reopen Speech & voices."
        ) from None
