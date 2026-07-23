"""Voice-output helpers for the standalone coach.

Extracted from arenamcp.standalone (pure move, no behavior change).
Re-exported from arenamcp.standalone for backwards compatibility."""

import contextlib
import logging
import subprocess
import sys
from typing import Any

logger = logging.getLogger(__name__)


class _SAPIVoice:
    """Lightweight OS-native TTS fallback — no numpy, no sounddevice, no PortAudio.

    Windows: PowerShell's System.Speech.Synthesis (SAPI).
    macOS: the built-in ``say`` command.
    Drop-in replacement for VoiceOutput in pipe mode.
    """

    # macOS `say` speaks at ~175 words per minute by default; the app's
    # `voice_speed` setting is a 1.0-centered multiplier, so scale from there.
    _SAY_BASE_WPM = 175
    _SAY_MIN_WPM = 90
    _SAY_MAX_WPM = 450

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._muted = False
        self._is_darwin = sys.platform == "darwin"
        self._say_voice: str | None = None
        self._say_rate: int = self._SAY_BASE_WPM
        if self._is_darwin:
            speed = 1.0
            try:
                # Look up via the standalone module (deferred) so tests that
                # monkeypatch standalone.get_settings keep working after the
                # extraction of this class out of standalone.py.
                from arenamcp import standalone

                settings = standalone.get_settings()
                speed = float(settings.get("voice_speed", 1.0) or 1.0)
                # Optional passthrough for a native macOS voice name
                # (e.g. "Samantha"); Kokoro voice IDs don't map to `say`.
                voice = settings.get("macos_voice") or settings.get("say_voice")
                if voice:
                    self._say_voice = str(voice)
            except Exception:
                pass
            self._say_rate = self._say_wpm(speed)

    @classmethod
    def _say_wpm(cls, speed: float) -> int:
        """Map the 1.0-centered voice_speed multiplier to say's -r wpm."""
        try:
            wpm = int(round(cls._SAY_BASE_WPM * float(speed)))
        except (TypeError, ValueError):
            wpm = cls._SAY_BASE_WPM
        return max(cls._SAY_MIN_WPM, min(cls._SAY_MAX_WPM, wpm))

    @property
    def current_voice(self) -> tuple[str, str]:
        if self._is_darwin:
            if self._say_voice:
                return ("say", f"macOS say ({self._say_voice})")
            return ("say", "macOS say")
        return ("sapi", "Windows SAPI")

    def speak(self, text: str, blocking: bool = True) -> None:
        if self._muted or not text or not text.strip():
            return
        # Clean markup
        import re

        text = text.replace("**", "").replace("*", "").replace("#", "")
        text = text.replace("```", "").replace("`", "").replace("...", " ")
        text = re.sub(r"\[[A-Z][A-Za-z0-9_,:{}/ ]*\]", "", text)
        try:
            self.stop()
            if self._is_darwin:
                self._proc = self._spawn_say(text)
            else:
                self._proc = self._spawn_sapi(text)
            if blocking and self._proc is not None:
                self._proc.wait(timeout=30)
        except Exception:
            pass

    def _spawn_say(self, text: str) -> subprocess.Popen:
        """macOS: speak via the built-in `say` command (text over stdin)."""
        cmd = ["say", "-r", str(self._say_rate)]
        if self._say_voice:
            cmd += ["-v", self._say_voice]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if proc.stdin is not None:
            try:
                proc.stdin.write(text.encode("utf-8"))
            finally:
                proc.stdin.close()
        return proc

    def _spawn_sapi(self, text: str) -> subprocess.Popen:
        """Windows: speak via PowerShell System.Speech (SAPI)."""
        # Escape for PowerShell
        safe = text.replace("'", "''").replace('"', '\\"')
        cmd = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.Speak('{safe}')"
        )
        return subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", cmd],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=0.5)
            except Exception:
                pass
        self._proc = None

    @property
    def is_speaking(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def toggle_mute(self) -> bool:
        self._muted = not self._muted
        return self._muted

    def next_voice(self):
        pass  # SAPI uses system default


class _PipeVoiceOutput:
    """Wrap VoiceOutput so the visible desktop parent owns audio playback."""

    def __init__(self, ui: Any, inner: Any):
        self._ui = ui
        self._inner = inner

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def speak(self, text: str, blocking: bool = True) -> None:
        if not text or not text.strip():
            return

        text = self._inner._clean_text(text)
        if not text or not text.strip():
            return

        if getattr(self._inner, "muted", False):
            return

        emit_request = getattr(self._ui, "emit_speech_request", None)
        if callable(emit_request):
            with contextlib.suppress(Exception):
                self._inner.stop()

            try:
                voice_id, voice_name = self._inner.current_voice
                speed = float(getattr(self._inner, "speed", getattr(self._inner, "_speed", 1.0)))
                emit_request(
                    text=text,
                    voice_id=voice_id,
                    voice_name=voice_name,
                    speed=speed,
                )
                logger.info(
                    "Pipe voice delegated to desktop worker voice=%s speed=%.2f",
                    voice_id,
                    speed,
                )
                return
            except Exception as e:
                logger.error("Pipe voice delegation failed: %s", e)

        # Fallback for non-pipe environments or if the desktop bridge is unavailable.
        self._inner.speak(text, blocking=blocking)

    def stop(self) -> None:
        emit_stop = getattr(self._ui, "emit_speech_stop", None)
        if callable(emit_stop):
            emit_stop()
        self._inner.stop()


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
