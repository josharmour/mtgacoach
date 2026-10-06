"""Kokoro v1.0 voices offered by the coach (shared by the desktop and in-process TTS).

Lightweight on purpose: the desktop engine imports it without loading ONNX.
"""

from __future__ import annotations

DEFAULT_VOICE = "am_adam"

# Every American and British English voice in voices-v1.0.bin (28 of its 54);
# the id's prefix encodes accent and gender: af/am US, bf/bm UK.
KOKORO_VOICES: list[tuple[str, str]] = [
    ("af_heart", "Heart (US Female)"),
    ("af_bella", "Bella (US Female)"),
    ("af_nicole", "Nicole (US Female)"),
    ("af_sky", "Sky (US Female)"),
    ("af_aoede", "Aoede (US Female)"),
    ("af_kore", "Kore (US Female)"),
    ("af_sarah", "Sarah (US Female)"),
    ("af_alloy", "Alloy (US Female)"),
    ("af_river", "River (US Female)"),
    ("af_jessica", "Jessica (US Female)"),
    ("af_nova", "Nova (US Female)"),
    ("am_adam", "Adam (US Male)"),
    ("am_echo", "Echo (US Male)"),
    ("am_eric", "Eric (US Male)"),
    ("am_fenrir", "Fenrir (US Male)"),
    ("am_liam", "Liam (US Male)"),
    ("am_michael", "Michael (US Male)"),
    ("am_onyx", "Onyx (US Male)"),
    ("am_puck", "Puck (US Male)"),
    ("am_santa", "Santa (US Male)"),
    ("bf_emma", "Emma (UK Female)"),
    ("bf_isabella", "Isabella (UK Female)"),
    ("bf_alice", "Alice (UK Female)"),
    ("bf_lily", "Lily (UK Female)"),
    ("bm_george", "George (UK Male)"),
    ("bm_fable", "Fable (UK Male)"),
    ("bm_lewis", "Lewis (UK Male)"),
    ("bm_daniel", "Daniel (UK Male)"),
]

VOICE_GROUPS = {"af": "US Female", "am": "US Male", "bf": "UK Female", "bm": "UK Male"}


def voice_index(voice_id: str | None) -> int:
    """Position of a voice id, falling back to the default voice."""
    ids = [voice for voice, _label in KOKORO_VOICES]
    if voice_id in ids:
        return ids.index(voice_id)
    return ids.index(DEFAULT_VOICE)
