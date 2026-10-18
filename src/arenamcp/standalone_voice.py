"""Voice-output helpers for the standalone coach (extracted from arenamcp.standalone)."""

import contextlib
import logging
import re
import subprocess
import sys
from typing import Any

from arenamcp.kokoro_voices import KOKORO_VOICES, voice_index

logger = logging.getLogger(__name__)


class _PipeVoiceOutput:
    """Voice for pipe mode: the desktop app speaks; this process only sends requests.

    Avoids loading a second copy of the ONNX voice model in the coach process.
    """

    # The full Kokoro list (2026-10-06: a hand-picked 8-voice list here hid
    # Adam, Sky, Onyx and the UK voices from the desktop app).
    _VOICES = KOKORO_VOICES
    SPEED_PRESETS = [0.8, 1.0, 1.2, 1.4, 1.6]

    def __init__(self, ui: Any):
        self._ui = ui
        # Start from the saved voice and speed, not a fixed default.
        saved_voice, saved_speed = None, 1.0
        with contextlib.suppress(Exception):
            from arenamcp.settings import get_settings

            settings = get_settings()
            saved_voice = settings.get("voice")
            saved_speed = float(settings.get("voice_speed", 1.0) or 1.0)
        self._voice_index = voice_index(saved_voice)
        self._speed = saved_speed
        self._muted = False

    @property
    def current_voice(self) -> tuple[str, str]:
        return self._VOICES[self._voice_index]

    @property
    def speed(self) -> float:
        return self._speed

    def cycle_speed(self) -> float:
        try:
            idx = self.SPEED_PRESETS.index(self._speed)
            idx = (idx + 1) % len(self.SPEED_PRESETS)
        except ValueError:
            idx = 1
        self._speed = self.SPEED_PRESETS[idx]
        self._save("voice_speed", self._speed)
        return self._speed

    @property
    def muted(self) -> bool:
        return self._muted

    def next_voice(self) -> tuple[str, str]:
        self._voice_index = (self._voice_index + 1) % len(self._VOICES)
        self._save("voice", self.current_voice[0])
        return self.current_voice

    def set_voice(self, voice_id: str) -> tuple[str, str]:
        """Select a voice by id (desktop voice picker); unknown ids are refused."""
        ids = [voice for voice, _label in self._VOICES]
        if voice_id not in ids:
            raise ValueError(f"Unknown Kokoro voice: {voice_id}")
        self._voice_index = ids.index(voice_id)
        self._save("voice", voice_id)
        return self.current_voice

    def set_speed(self, speed: float) -> float:
        self._speed = max(0.5, min(2.0, float(speed)))
        self._save("voice_speed", self._speed)
        return self._speed

    @staticmethod
    def _save(key: str, value: Any) -> None:
        with contextlib.suppress(Exception):
            from arenamcp.settings import get_settings

            get_settings().set(key, value)

    def toggle_mute(self) -> bool:
        self._muted = not self._muted
        return self._muted

    def speak(self, text: str, blocking: bool = False) -> None:
        if not text or not text.strip() or self.muted:
            return
        text = text.replace("**", "").replace("*", "").replace("#", "")
        text = text.replace("```", "").replace("`", "").replace("...", " ")
        text = re.sub(r"\[[A-Z][A-Za-z0-9_,:{}/ ]*\]", "", text).strip()
        if not text:
            return

        emit_request = getattr(self._ui, "emit_speech_request", None)
        if not callable(emit_request):
            return
        voice_id, voice_name = self.current_voice
        emit_request(text=text, voice_id=voice_id, voice_name=voice_name, speed=self._speed)

    def stop(self) -> None:
        emit_stop = getattr(self._ui, "emit_speech_stop", None)
        if callable(emit_stop):
            emit_stop()


def _probe_sounddevice_import(timeout_seconds: float = 8.0) -> tuple[bool, str]:
    """Probe sounddevice import in a subprocess.

    Importing sounddevice can block inside PortAudio initialization when an audio
    driver is misbehaving. Probing in a subprocess keeps the main process safe.
    stdin=DEVNULL prevents inheriting the parent's pipe when running under a GUI.
    """
    cmd = [sys.executable, "-c", "import sounddevice"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return False, f"timeout after {int(timeout_seconds)}s"
    except Exception as e:
        return False, str(e)

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if detail:
            # Keep only the final line to avoid flooding logs/UI.
            detail = detail.splitlines()[-1]
        else:
            detail = f"exit code {result.returncode}"
        return False, detail

    return True, "ok"
