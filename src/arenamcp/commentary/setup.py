"""Read existing speech setup without starting services or changing preferences."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path

from arenamcp.chatterbox_remote import HOSTED_BASE, hosted_voices

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
    values = {key: str(settings.get(key) or "") for key in ("commentary_python", "commentary_aoa_source")}
    values["chatterbox_server_url"] = HOSTED_BASE + "/speech"
    values["chatterbox_key_file"] = ""
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
    return SpeechSetup(values, bool(settings.get("license_key")), False, source_available, True)


def server_voices(endpoint=None, *, api_key=None, opener=None):
    """Fetch the hosted catalog; legacy endpoint preferences are ignored."""
    if api_key is None:
        from arenamcp.settings import get_settings

        api_key = str(get_settings().get("license_key") or "")
    return hosted_voices(api_key, opener=opener)
