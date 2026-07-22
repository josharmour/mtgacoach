"""Text-to-speech synthesis using Kokoro ONNX.

This module provides TTS using the kokoro-onnx library for offline,
low-latency speech synthesis with high-quality neural voices.

NOTE: Model files must be downloaded manually (~300MB total):
- kokoro-v1.0.onnx: https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
- voices-v1.0.bin: https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin

Place files in ~/.cache/kokoro/ or specify paths explicitly.
"""

import io
import os
import struct
import subprocess
import sys
import tempfile
import threading
import ctypes
import wave
from pathlib import Path
from typing import Optional

import logging

# Lazy numpy import — module-level import hangs in subprocess contexts
np = None

def _ensure_numpy():
    global np
    if np is None:
        import numpy
        np = numpy

# Audio backend selection:
# - Windows: use winsound with temp WAV files. SND_MEMORY blocks when the
#   process has no foreground window; file-based playback avoids this.
#   sounddevice/PortAudio hangs during device enumeration on some systems.
# - Other platforms: use sounddevice (PortAudio).
_USE_WINSOUND = sys.platform == "win32"
sd = None
winsound = None
if _USE_WINSOUND:
    import winsound
    import wave as _wave_mod
else:
    try:
        import sounddevice as sd
    except ImportError:
        sd = None

logger = logging.getLogger(__name__)

if _USE_WINSOUND:
    _winmm = ctypes.WinDLL("winmm", use_last_error=True)
    _play_sound_w = _winmm.PlaySoundW
    _play_sound_w.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint]
    _play_sound_w.restype = ctypes.c_bool

    _SND_ASYNC = 0x00000001
    _SND_NODEFAULT = 0x00000002
    _SND_PURGE = 0x00000040
    _SND_FILENAME = 0x00020000
    # Route pipe-mode playback through the system audio session instead of the
    # hidden python.exe session. This avoids the focus/input gating seen when
    # the launcher window has not yet received user input.
    _SND_SYSTEM = 0x00200000


def _native_play_sound(path: Optional[str], flags: int) -> bool:
    if not _USE_WINSOUND:
        return False

    ctypes.set_last_error(0)
    ok = bool(_play_sound_w(path, None, flags))
    err = ctypes.get_last_error()
    target = path if path is not None else "<null>"
    logger.info("PlaySoundW(path=%s, flags=0x%08X) => ok=%s last_error=%s", target, flags, ok, err)
    return ok


def _samples_to_wav_bytes(samples, sample_rate: int) -> bytes:
    """Convert float32 numpy samples to WAV bytes in memory."""
    _ensure_numpy()
    # Kokoro does not guarantee peaks <= 1.0; without normalization hot
    # samples hard-clip at int16 conversion and speech sounds maxed-out /
    # crackly. Only ever scale DOWN, to 0.95 headroom.
    if len(samples):
        peak = float(np.max(np.abs(samples)))
        if peak > 0.95:
            samples = samples * (0.95 / peak)
    int_samples = np.clip(samples * 32767, -32768, 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(int_samples.tobytes())
    return buf.getvalue()


# Temp WAV file for winsound file-based playback (avoids SND_MEMORY focus bug)
_winsound_tmp_path: Optional[str] = None


def _winsound_play_samples(samples, sample_rate: int, blocking: bool = True) -> None:
    """Play samples via winsound using a temp WAV file.

    Always uses SND_ASYNC to avoid blocking when the process has no
    foreground window. Playback is routed through the Windows system audio
    session so the hidden pipe child is not dependent on window focus.
    For blocking mode, sleeps for the audio duration.
    """
    global _winsound_tmp_path
    _ensure_numpy()

    wav_data = _samples_to_wav_bytes(samples, sample_rate)

    # Write to a persistent temp file (reused across calls)
    if _winsound_tmp_path is None:
        fd, _winsound_tmp_path = tempfile.mkstemp(suffix=".wav", prefix="mtgacoach_")
        os.close(fd)

    with open(_winsound_tmp_path, 'wb') as f:
        f.write(wav_data)

    # Always use SND_ASYNC — blocking PlaySound hangs when the process
    # lacks a foreground window (no active audio session).
    flags = _SND_FILENAME | _SND_ASYNC | _SND_NODEFAULT | _SND_SYSTEM
    if not _native_play_sound(_winsound_tmp_path, flags):
        # Fall back to winsound's wrapper without the system-session flag if
        # the native call fails for any reason on the current machine.
        logger.warning("PlaySoundW with SND_SYSTEM failed; falling back to winsound.PlaySound")
        winsound.PlaySound(
            _winsound_tmp_path,
            winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
        )

    if blocking:
        # Approximate playback duration and wait
        duration = len(samples) / sample_rate
        import time as _time
        _time.sleep(duration)


# Default model cache location
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "kokoro"
MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"

# Model download URLs for error messages
MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"

# Language code → Kokoro lang ID mapping
# Kokoro v1.0 supports: en-us, en-gb, ja, zh, ko, fr, de, es, pt-br, hi, it
LANG_MAP = {
    "en": "en-us",
    "en-us": "en-us",
    "en-gb": "en-gb",
    "ja": "ja",
    "zh": "zh",
    "ko": "ko",
    "fr": "fr",
    "de": "de",
    "es": "es",
    "pt": "pt-br",
    "pt-br": "pt-br",
    "hi": "hi",
    "it": "it",
    # Unsupported languages fall back to en-us via .get() default
}


class KokoroTTS:
    """Text-to-speech synthesizer using Kokoro ONNX.

    Uses lazy model loading to avoid startup delay. The model is only
    loaded when the first synthesis is requested.

    Example:
        tts = KokoroTTS()
        # First call loads model (~300MB)
        samples, sample_rate = tts.synthesize("Hello, how can I help?")

    Note:
        Model files must be downloaded manually. See module docstring
        for download URLs. Default location: ~/.cache/kokoro/
    """

    # Kokoro outputs at fixed 24kHz sample rate
    SAMPLE_RATE = 24000

    def __init__(
        self,
        model_path: Optional[str] = None,
        voices_path: Optional[str] = None,
        voice: str = "am_adam",
        speed: float = 1.0,
        lang: str = "en-us",
    ) -> None:
        """Initialize the TTS synthesizer.

        Args:
            model_path: Path to kokoro-v1.0.onnx file. Defaults to
                       ~/.cache/kokoro/kokoro-v1.0.onnx
            voices_path: Path to voices-v1.0.bin file. Defaults to
                        ~/.cache/kokoro/voices-v1.0.bin
            voice: Voice ID to use. Default 'am_adam' (American Male Adam).
            speed: Speech speed multiplier. Default 1.0.
            lang: Language code. Default 'en-us'.
        """
        # Resolve model paths
        if model_path is None:
            self._model_path = DEFAULT_CACHE_DIR / MODEL_FILE
        else:
            self._model_path = Path(model_path)

        if voices_path is None:
            self._voices_path = DEFAULT_CACHE_DIR / VOICES_FILE
        else:
            self._voices_path = Path(voices_path)

        self._voice = voice
        self._speed = speed
        self._lang = lang

        # Lazy-loaded model
        self._kokoro: Optional[object] = None
        self._load_lock = threading.Lock()

    def _ensure_model_loaded(self) -> None:
        """Lazy-load the Kokoro model on first use.

        Raises:
            FileNotFoundError: If model files are not found, with
                             download instructions.
            ImportError: If kokoro-onnx is not installed.
        """
        if self._kokoro is not None:
            return

        with self._load_lock:
            # Double-check after acquiring lock
            if self._kokoro is not None:
                return

            # Check model files exist
            missing_files = []
            if not self._model_path.exists():
                missing_files.append(
                    f"Model file not found: {self._model_path}\n"
                    f"  Download from: {MODEL_URL}"
                )
            if not self._voices_path.exists():
                missing_files.append(
                    f"Voices file not found: {self._voices_path}\n"
                    f"  Download from: {VOICES_URL}"
                )

            if missing_files:
                # Create cache directory hint
                cache_hint = (
                    f"\nCreate directory and download files:\n"
                    f"  mkdir -p {DEFAULT_CACHE_DIR}\n"
                    f"  cd {DEFAULT_CACHE_DIR}\n"
                    f"  curl -LO {MODEL_URL}\n"
                    f"  curl -LO {VOICES_URL}"
                )
                raise FileNotFoundError(
                    "Kokoro TTS model files not found:\n\n"
                    + "\n\n".join(missing_files)
                    + cache_hint
                )

            # Import and load kokoro
            try:
                from kokoro_onnx import Kokoro
            except ImportError as e:
                raise ImportError(
                    "kokoro-onnx not installed. Run: pip install kokoro-onnx"
                ) from e

            self._kokoro = Kokoro(
                str(self._model_path),
                str(self._voices_path),
            )

    def synthesize(self, text: str):
        """Synthesize text to audio samples.

        Returns:
            Tuple of (samples, sample_rate) where samples is a numpy
            array of float32 audio data and sample_rate is always 24000.
        """
        _ensure_numpy()
        self._ensure_model_loaded()

        if not text or not text.strip():
            return np.array([], dtype=np.float32), self.SAMPLE_RATE

        # Generate audio using Kokoro
        samples, sample_rate = self._kokoro.create(
            text,
            voice=self._voice,
            speed=self._speed,
            lang=self._lang,
        )

        return samples, sample_rate


class VoiceOutput:
    """Unified voice output interface for TTS playback.

    Provides speak(), speak_async(), and stop() methods for easy
    text-to-speech with audio playback. Counterpart to VoiceInput.

    Example:
        output = VoiceOutput()
        output.speak("Hello, I'm your coach!")  # Blocks until done

        output.speak_async("This plays in background")
        # ... do other work ...
        output.stop()  # Interrupt if needed

    Note:
        First call loads TTS model (~300MB). Model files must be
        downloaded manually - see KokoroTTS docstring.
    """

    # Available Kokoro voices (name, description)
    VOICES = [
        ("am_adam", "Adam"),
        ("am_michael", "Michael"),
        ("af_heart", "Heart"),
        ("af_bella", "Bella"),
        ("af_nicole", "Nicole"),
        ("af_sarah", "Sarah"),
        ("af_sky", "Sky"),
    ]

    def __init__(
        self,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
    ) -> None:
        """Initialize VoiceOutput.

        Args:
            voice: Kokoro voice ID. If None, loads from settings
                  (default: 'am_adam' - American Male Adam).
            speed: Speech speed multiplier. If None, loads from settings
                  (default: 1.0).
        """
        # Load from settings if not specified
        from arenamcp.settings import get_settings
        settings = get_settings()

        self._voice = voice if voice is not None else settings.get("voice", "am_adam")
        self._speed = speed if speed is not None else settings.get("voice_speed", 1.0)
        self._device_index = settings.get("device_index", None)
        self._voice_index = 0  # Index into VOICES list
        self._muted = settings.get("muted", False)
        self._settings = settings

        # Language: map short codes to Kokoro lang IDs
        lang_code = settings.get("language", "en")
        self._lang = LANG_MAP.get(lang_code, "en-us")

        # Find initial voice index
        for i, (vid, _) in enumerate(self.VOICES):
            if vid == self._voice:
                self._voice_index = i
                break

        # Lazy-initialized TTS
        self._tts_engine = None

        # Playback state
        self._lock = threading.Lock()
        self._speak_lock = threading.Lock()  # Prevents overlapping speech
        self._is_speaking = False
        self._stop_requested = False
        self._stream = None
        self._playback_thread: Optional[threading.Thread] = None
        self._windows_tts_proc: Optional[subprocess.Popen] = None
        self._fallback_tts_error_logged = False

    def warmup(self, delay: float = 0) -> None:
        """Pre-load the TTS model so first speak() has no delay.

        Call this in a background thread during startup. Loads the ONNX
        model (~300MB) and runs a tiny synthesis to warm the inference path.

        Args:
            delay: Seconds to wait before starting warmup. Allows other
                   startup threads (coaching loop) to initialize first.
        """
        try:
            if delay > 0:
                import time as _time
                _time.sleep(delay)
            self._ensure_tts()
            # Run a tiny synthesis to warm the ONNX runtime session.
            # Must hold _speak_lock to serialize with speak()/speak_async(),
            # otherwise concurrent ONNX calls trigger access violations.
            with self._speak_lock:
                self._tts_engine.synthesize(".")
            logger.info("TTS model warmed up")
        except Exception as e:
            logger.warning(f"TTS warmup failed: {e}")

    def _ensure_tts(self) -> None:
        """Initialize TTS on first use."""
        if self._tts_engine is None:
            self._tts_engine = KokoroTTS(voice=self._voice, speed=self._speed, lang=self._lang)

    def _windows_rate(self) -> int:
        """Map Kokoro speed multiplier to Windows SpeechSynthesizer rate."""
        rate = int(round((self._speed - 1.0) * 8))
        return max(-10, min(10, rate))

    def _try_windows_tts_fallback(self, text: str, blocking: bool) -> bool:
        """Fallback to built-in Windows SAPI TTS when Kokoro isn't available."""
        if os.name != "nt":
            return False

        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.Rate = {self._windows_rate()}; "
            "$inputText = [Console]::In.ReadToEnd(); "
            "if (-not [string]::IsNullOrWhiteSpace($inputText)) { $s.Speak($inputText) }"
        )
        cmd = ["powershell.exe", "-NoProfile", "-Command", script]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            if blocking:
                with self._lock:
                    self._is_speaking = True
                    self._stop_requested = False
                subprocess.run(
                    cmd,
                    input=text,
                    text=True,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                )
                with self._lock:
                    self._is_speaking = False
                return True

            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                creationflags=creationflags,
            )
            if proc.stdin is not None:
                proc.stdin.write(text)
                proc.stdin.close()

            with self._lock:
                self._windows_tts_proc = proc
                self._is_speaking = True
                self._stop_requested = False

            def _wait_proc() -> None:
                proc.wait()
                with self._lock:
                    if self._windows_tts_proc is proc:
                        self._windows_tts_proc = None
                    self._is_speaking = False

            threading.Thread(target=_wait_proc, daemon=True).start()
            return True
        except Exception as e:
            if not self._fallback_tts_error_logged:
                logger.error(f"Windows TTS fallback failed: {e}")
                self._fallback_tts_error_logged = True
            with self._lock:
                self._is_speaking = False
            return False

    @property
    def muted(self) -> bool:
        """Check if output is muted."""
        return self._muted

    @property
    def current_voice(self) -> tuple[str, str]:
        """Get current voice (id, description)."""
        return self.VOICES[self._voice_index]

    @property
    def speed(self) -> float:
        """Get the current speed multiplier."""
        return float(self._speed)

    def toggle_mute(self) -> bool:
        """Toggle mute state.

        Returns:
            New mute state (True if now muted).
        """
        self._muted = not self._muted
        if self._muted:
            self.stop()  # Stop any current playback

        # Persist setting
        self._settings.set("muted", self._muted)
        return self._muted

    def next_voice(self, step: int = 1) -> tuple[str, str]:
        """Cycle to the next voice.

        Args:
            step: Number of voices to skip (default 1).

        Returns:
            Tuple of (voice_id, description) for the new voice.
        """
        self._voice_index = (self._voice_index + step) % len(self.VOICES)
        voice_id, description = self.VOICES[self._voice_index]
        self._voice = voice_id

        # Recreate TTS with new voice
        if self._tts_engine is not None:
             # Force re-init with new voice
            self._tts_engine = None
            self._ensure_tts()

        # Persist setting
        self._settings.set("voice", voice_id)
        return (voice_id, description)

    # Speed presets for cycling
    SPEED_PRESETS = [1.0, 1.2, 1.4]

    def cycle_speed(self) -> float:
        """Cycle TTS speed through presets (1.0x → 1.2x → 1.4x → 1.0x).

        Returns:
            The new speed value.
        """
        try:
            idx = self.SPEED_PRESETS.index(self._speed)
            idx = (idx + 1) % len(self.SPEED_PRESETS)
        except ValueError:
            idx = 0  # Reset to 1.0x if current speed isn't a preset

        self._speed = self.SPEED_PRESETS[idx]

        # Recreate TTS engine with new speed
        if self._tts_engine is not None:
            self._tts_engine = None
            self._ensure_tts()

        # Persist setting
        self._settings.set("voice_speed", self._speed)
        return self._speed

    def set_speed(self, speed: float) -> float:
        """Set TTS speed directly.

        Args:
            speed: Speech speed multiplier.

        Returns:
            The applied speed value.
        """
        try:
            new_speed = float(speed)
        except (TypeError, ValueError):
            return self._speed

        if new_speed <= 0:
            return self._speed

        self._speed = new_speed

        self._settings.set("voice_speed", self._speed)
        return self._speed

    def set_voice(self, voice_id: str) -> str:
        """Set active TTS voice directly by voice ID.

        Args:
            voice_id: Identifier of the target voice (e.g. 'af_sky', 'am_adam').

        Returns:
            The applied voice ID.
        """
        voice_id = (voice_id or "").strip()
        if not voice_id:
            return self._voice

        self._voice = voice_id
        for idx, (vid, _) in enumerate(self.VOICES):
            if vid == voice_id:
                self._voice_index = idx
                break

        if self._tts_engine is not None:
            self._tts_engine = None

        self._settings.set("voice", voice_id)
        return self._voice

    def render_to_wav_file(self, text: str) -> tuple[str, float] | None:
        """Render speech to a temp WAV file for parent-process playback."""
        if self._muted:
            return None

        text = self._clean_text(text)
        if not text or not text.strip():
            return None

        self._ensure_tts()

        with self._speak_lock:
            try:
                samples, sample_rate = self._tts_engine.synthesize(text)
            except (ImportError, FileNotFoundError) as e:
                logger.warning(
                    "Kokoro unavailable while rendering pipe audio (%s); "
                    "falling back to in-process playback",
                    e,
                )
                return None

            if len(samples) == 0:
                return None

            # Keep the same lead-in used for direct playback so the first word
            # is not clipped when the parent process starts the stream.
            silence_len = int(sample_rate * 0.5)
            _ensure_numpy()
            silence = np.zeros(silence_len, dtype=np.float32)
            samples = np.concatenate([silence, samples])

            fd, path = tempfile.mkstemp(suffix=".wav", prefix="mtgacoach_pipe_")
            os.close(fd)
            with open(path, "wb") as f:
                f.write(_samples_to_wav_bytes(samples, sample_rate))

            duration = len(samples) / sample_rate
            logger.info(
                "Rendered pipe audio wav=%s duration=%.2fs text=%s...",
                path,
                duration,
                text[:50],
            )
            return path, duration

    def set_voice(self, voice_id: str) -> None:
        """Set the current voice directly by ID.
        
        Args:
            voice_id: The ID of the voice to set (e.g. 'am_adam', 'af_heart')
        """
        # Find index
        found = False
        for i, (vid, _) in enumerate(self.VOICES):
            if vid == voice_id:
                self._voice_index = i
                self._voice = voice_id
                found = True
                break
        
        if not found:
            logger.warning(f"Voice {voice_id} not found, ignoring.")
            return

        # Recreate TTS engine
        if self._tts_engine is not None:
            self._tts_engine = None
            self._ensure_tts()
            
        self._settings.set("voice", voice_id)

    @property
    def is_speaking(self) -> bool:
        """Check if audio is currently playing.

        Returns:
            True if speak() or speak_async() is actively playing audio.
        """
        with self._lock:
            return self._is_speaking

    def _clean_text(self, text: str) -> str:
        """Remove markdown and special characters that TTS shouldn't pronounce."""
        import re
        # Remove asterisks (bold/italic)
        text = text.replace("**", "").replace("*", "")
        # Remove hash (headers)
        text = text.replace("##", "").replace("#", "")
        # Remove backticks (code)
        text = text.replace("```", "").replace("`", "")
        # Remove ellipsis to prevent "dot dot dot" or "d d d"
        text = text.replace("...", " ")
        # Remove game-state bracket annotations like [S,NEED:1], [I,OK], [RM:creat], [T], [FLY]
        text = re.sub(r"\[[A-Z][A-Za-z0-9_,:{}/ ]*\]", "", text)
        # Remove warning emoji that TTS might try to pronounce
        text = text.replace("\u26a0\ufe0f", "Warning:")
        # Normalize short ALL-CAPS decision prefixes ("KEEP:", "MULLIGAN \u2014",
        # "TOP:", "BOTTOM:", "GRAVEYARD:", "LIBRARY:") into a standalone
        # sentence. Kokoro's ONNX model clips stop-consonants (K/M/T) at
        # the head of an utterance on some voices even with leading silence;
        # turning the prefix into its own sentence fixes the "KEEP" being
        # swallowed in mulligan advice.
        _DECISION_PREFIXES = (
            "KEEP", "MULLIGAN", "TOP", "BOTTOM",
            "GRAVEYARD", "LIBRARY", "SKIP", "ATTACK",
            "BLOCK", "PASS", "DRAW", "DISCARD",
        )
        text = re.sub(
            r"^\s*(" + "|".join(_DECISION_PREFIXES) + r")\s*[:\-\u2014]+\s+",
            lambda m: m.group(1).capitalize() + ". ",
            text,
        )
        return text

    def speak(self, text: str, blocking: bool = True) -> None:
        """Synthesize and play text as speech.

        Args:
            text: Text to speak.
            blocking: If True (default), block until audio finishes.
                     If False, same as speak_async().

        Raises:
            FileNotFoundError: If TTS model files not found.
            sd.PortAudioError: If no audio output device available.
        """
        # Skip if muted
        if self._muted:
            return

        # SAPI-only mode (pipe) — use Windows built-in TTS directly
        if getattr(self, '_sapi_only', False):
            if not text or not text.strip():
                return
            text = self._clean_for_tts(text)
            self._try_windows_tts_fallback(text, blocking=blocking)
            return
            
        text = self._clean_text(text)

        if not blocking:
            self.speak_async(text)
            return

        self._ensure_tts()

        if not text or not text.strip():
            return

        # Use speak lock to prevent overlapping speech from multiple threads
        with self._speak_lock:
            # Stop any existing playback
            self.stop()

            # Synthesize
            try:
                samples, sample_rate = self._tts_engine.synthesize(text)
            except (ImportError, FileNotFoundError) as e:
                logger.warning(f"Kokoro unavailable ({e}); trying Windows TTS fallback")
                if self._try_windows_tts_fallback(text, blocking=True):
                    return
                raise
            if len(samples) == 0:
                return

            # Add leading silence (500ms) to prevent cutting off the first word.
            # Audio devices (especially Bluetooth/USB) need time to wake up after
            # sd.stop() or when the stream first opens.  200ms was not enough.
            silence_len = int(sample_rate * 0.5)
            _ensure_numpy()
            silence = np.zeros(silence_len, dtype=np.float32)
            samples = np.concatenate([silence, samples])

            with self._lock:
                self._is_speaking = True
                self._stop_requested = False

            try:
                logger.info(f"Speaking (device={self._device_index}): {text[:50]}...")
                if _USE_WINSOUND:
                    _winsound_play_samples(samples, sample_rate, blocking=True)
                elif sd is not None:
                    sd.play(samples, sample_rate, device=self._device_index)
                    sd.wait()
                else:
                    logger.error("No audio backend available")
            except Exception as e:
                logger.error(f"Audio playback failed: {e}")
            finally:
                with self._lock:
                    self._is_speaking = False

    def speak_async(self, text: str) -> None:
        """Synthesize and play text without blocking.

        Returns immediately. Use is_speaking property to check status
        or stop() to interrupt.

        Args:
            text: Text to speak.

        Raises:
            FileNotFoundError: If TTS model files not found.
        """
        self._ensure_tts()

        if not text or not text.strip():
            return

        # Use speak lock to prevent two threads from both synthesizing
        # and starting playback concurrently (race condition: both call
        # stop() before either starts, then both play simultaneously).
        with self._speak_lock:
            # Stop any existing playback
            self.stop()

            # Synthesize
            try:
                samples, sample_rate = self._tts_engine.synthesize(text)
            except (ImportError, FileNotFoundError) as e:
                logger.warning(f"Kokoro unavailable ({e}); trying Windows TTS fallback")
                if self._try_windows_tts_fallback(text, blocking=False):
                    return
                raise
            if len(samples) == 0:
                return

            # Add leading silence (500ms) to prevent cutting off the first word.
            silence_len = int(sample_rate * 0.5)
            _ensure_numpy()
            silence = np.zeros(silence_len, dtype=np.float32)
            samples = np.concatenate([silence, samples])

            def _playback_worker():
                with self._lock:
                    self._is_speaking = True
                    self._stop_requested = False

                try:
                    logger.info(f"Speaking async (device={self._device_index}): {text[:50]}...")
                    if _USE_WINSOUND:
                        _winsound_play_samples(samples, sample_rate, blocking=True)
                    elif sd is not None:
                        sd.play(samples, sample_rate, device=self._device_index)
                        sd.wait()
                    else:
                        logger.error("No audio backend available")

                except Exception as e:
                    logger.error(f"Audio playback failed: {e}")
                finally:
                    with self._lock:
                        self._is_speaking = False
                        self._playback_thread = None

            # Start playback in background thread
            with self._lock:
                self._playback_thread = threading.Thread(
                    target=_playback_worker,
                    daemon=True,
                )
                self._playback_thread.start()

    def stop(self) -> None:
        """Stop any ongoing playback.

        Safe to call even if nothing is playing.
        """
        with self._lock:
            self._stop_requested = True
            thread = self._playback_thread
            win_proc = self._windows_tts_proc
            self._windows_tts_proc = None

        # Stop audio playback
        if _USE_WINSOUND:
            try:
                if not _native_play_sound(None, _SND_PURGE):
                    winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
        elif sd is not None:
            sd.stop()

        if win_proc is not None and win_proc.poll() is None:
            try:
                win_proc.terminate()
                win_proc.wait(timeout=0.5)
            except Exception:
                pass

        # Wait for async thread to finish
        if thread is not None:
            thread.join(timeout=1.0)

        with self._lock:
            self._is_speaking = False

