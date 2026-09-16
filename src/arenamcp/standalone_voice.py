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

    @property
    def _is_darwin(self) -> bool:
        try:
            from arenamcp import standalone

            return getattr(standalone.sys, "platform", sys.platform) == "darwin"
        except Exception:
            return sys.platform == "darwin"

    @staticmethod
    def _get_popen():
        try:
            from arenamcp import standalone

            return getattr(standalone.subprocess, "Popen", subprocess.Popen)
        except Exception:
            return subprocess.Popen

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._muted = False
        self._speed = 1.0
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
                self._speed = speed
                # Optional passthrough for a native macOS voice name
                # (e.g. "Samantha"); Kokoro voice IDs don't map to `say`.
                voice = settings.get("macos_voice") or settings.get("say_voice")
                if voice:
                    self._say_voice = str(voice)
            except Exception:
                pass
            self._say_rate = self._say_wpm(self._speed)

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
        proc = self._get_popen()(
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
        return self._get_popen()(
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

    SPEED_PRESETS = [0.8, 1.0, 1.2, 1.4, 1.6]

    @property
    def is_speaking(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

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
        if self._is_darwin:
            self._say_rate = self._say_wpm(self._speed)
        return self._speed

    def toggle_mute(self) -> bool:
        self._muted = not self._muted
        return self._muted

    def next_voice(self):
        pass  # SAPI uses system default


class _PipeVoiceOutput:
    """Lightweight voice delegate for pipe mode — avoids duplicate ONNX model loading."""

    _VOICES = [
        ("af_heart", "Heart (Female)"),
        ("af_bella", "Bella (Female)"),
        ("af_nicole", "Nicole (Female)"),
        ("af_aoede", "Aoede (Female)"),
        ("am_fenrir", "Fenrir (Male)"),
        ("am_puck", "Puck (Male)"),
        ("am_michael", "Michael (Male)"),
        ("am_eric", "Eric (US Male)"),
    ]
    SPEED_PRESETS = [0.8, 1.0, 1.2, 1.4, 1.6]

    def __init__(self, ui: Any, inner: Any = None):
        self._ui = ui
        self._inner = inner
        self._voice_index = 7
        self._speed = 1.0
        self._muted = False

    def __getattr__(self, name: str) -> Any:
        if self._inner is not None:
            return getattr(self._inner, name)
        raise AttributeError(f"'_PipeVoiceOutput' object has no attribute '{name}'")

    @property
    def current_voice(self) -> tuple[str, str]:
        if self._inner is not None and hasattr(self._inner, "current_voice"):
            return self._inner.current_voice
        return self._VOICES[self._voice_index]

    @property
    def speed(self) -> float:
        if self._inner is not None and hasattr(self._inner, "speed"):
            return float(self._inner.speed)
        return self._speed

    def cycle_speed(self) -> float:
        if self._inner is not None and hasattr(self._inner, "cycle_speed"):
            return self._inner.cycle_speed()
        try:
            idx = self.SPEED_PRESETS.index(self._speed)
            idx = (idx + 1) % len(self.SPEED_PRESETS)
        except ValueError:
            idx = 1
        self._speed = self.SPEED_PRESETS[idx]
        from arenamcp.settings import get_settings

        with contextlib.suppress(Exception):
            get_settings().set("voice_speed", self._speed)
        return self._speed

    @property
    def muted(self) -> bool:
        if self._inner is not None and hasattr(self._inner, "muted"):
            return bool(self._inner.muted)
        return self._muted

    def next_voice(self) -> tuple[str, str]:
        if self._inner is not None and hasattr(self._inner, "next_voice"):
            return self._inner.next_voice()
        self._voice_index = (self._voice_index + 1) % len(self._VOICES)
        return self.current_voice

    def toggle_mute(self) -> bool:
        if self._inner is not None and hasattr(self._inner, "toggle_mute"):
            return self._inner.toggle_mute()
        self._muted = not self._muted
        return self._muted

    def speak(self, text: str, blocking: bool = False, priority: Any = None, identity: Any = None) -> None:
        if not text or not text.strip() or self.muted:
            return
        if self._inner is not None and hasattr(self._inner, "_clean_text"):
            text = self._inner._clean_text(text)
        else:
            import re

            text = text.replace("**", "").replace("*", "").replace("#", "")
            text = text.replace("```", "").replace("`", "").replace("...", " ")
            text = re.sub(r"\[[A-Z][A-Za-z0-9_,:{}/ ]*\]", "", text).strip()
        if not text or not text.strip():
            return

        emit_request = getattr(self._ui, "emit_speech_request", None)
        if callable(emit_request):
            if self._inner is not None and hasattr(self._inner, "stop"):
                with contextlib.suppress(Exception):
                    self._inner.stop()
            voice_id, voice_name = self.current_voice
            speed = float(getattr(self._inner, "speed", getattr(self, "_speed", 1.0)))
            extra: dict[str, Any] = {}
            if priority is not None:
                extra["priority"] = priority
            if identity is not None:
                extra["identity"] = identity
            emit_request(
                text=text,
                voice_id=voice_id,
                voice_name=voice_name,
                speed=speed,
                **extra,
            )
            return

        if self._inner is not None and hasattr(self._inner, "speak"):
            self._inner.speak(text, blocking=blocking)

    def stop(self) -> None:
        emit_stop = getattr(self._ui, "emit_speech_stop", None)
        if callable(emit_stop):
            emit_stop()
        if self._inner is not None and hasattr(self._inner, "stop"):
            with contextlib.suppress(Exception):
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
