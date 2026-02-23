"""Text-to-speech synthesis using Kokoro ONNX.

This module provides TTS using the kokoro-onnx library for offline,
low-latency speech synthesis with high-quality neural voices.

NOTE: Model files must be downloaded manually (~300MB total):
- kokoro-v1.0.onnx: https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
- voices-v1.0.bin: https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin

Place files in ~/.cache/kokoro/ or specify paths explicitly.
"""

import threading
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd
import logging

logger = logging.getLogger(__name__)


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
        sd.play(samples, sample_rate)
        sd.wait()

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

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        """Synthesize text to audio samples.

        Args:
            text: Text to synthesize. Should be reasonable length
                 (sentences, not paragraphs) for best quality.

        Returns:
            Tuple of (samples, sample_rate) where samples is a numpy
            array of float32 audio data and sample_rate is always 24000.

        Raises:
            FileNotFoundError: If model files are not found.
            ImportError: If kokoro-onnx is not installed.
        """
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
        ("af_heart", "American Female - Heart (Grade A)"),
        ("af_bella", "American Female - Bella"),
        ("af_nicole", "American Female - Nicole"),
        ("af_sarah", "American Female - Sarah"),
        ("af_sky", "American Female - Sky"),
        ("am_adam", "American Male - Adam"),
        ("am_michael", "American Male - Michael"),
        ("bf_emma", "British Female - Emma"),
        ("bf_isabella", "British Female - Isabella"),
        ("bm_george", "British Male - George"),
        ("bm_lewis", "British Male - Lewis"),
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
        self._stream: Optional[sd.OutputStream] = None
        self._playback_thread: Optional[threading.Thread] = None

    def _ensure_tts(self) -> None:
        """Initialize TTS on first use."""
        if self._tts_engine is None:
            self._tts_engine = KokoroTTS(voice=self._voice, speed=self._speed, lang=self._lang)

    @property
    def muted(self) -> bool:
        """Check if output is muted."""
        return self._muted

    @property
    def current_voice(self) -> tuple[str, str]:
        """Get current voice (id, description)."""
        return self.VOICES[self._voice_index]

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
            samples, sample_rate = self._tts_engine.synthesize(text)
            if len(samples) == 0:
                return

            # Add a small amount of leading silence (200ms) to prevent cutting off the first word
            # on some audio devices/Bluetooth that have a wake-up delay.
            silence_len = int(sample_rate * 0.2)
            silence = np.zeros(silence_len, dtype=np.float32)
            samples = np.concatenate([silence, samples])

            with self._lock:
                self._is_speaking = True
                self._stop_requested = False

            try:
                # Play with sounddevice (blocking)
                logger.info(f"Speaking (device={self._device_index}): {text[:50]}...")
                sd.play(samples, sample_rate, device=self._device_index)
                sd.wait()
            except sd.PortAudioError as e:
                # No audio device - fail gracefully
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

        # Stop any existing playback
        self.stop()

        # Synthesize
        samples, sample_rate = self._tts_engine.synthesize(text)
        if len(samples) == 0:
            return

        # Add a small amount of leading silence (200ms) to prevent cutting off the first word
        silence_len = int(sample_rate * 0.2)
        silence = np.zeros(silence_len, dtype=np.float32)
        samples = np.concatenate([silence, samples])

        def _playback_worker():
            with self._lock:
                self._is_speaking = True
                self._stop_requested = False

            try:
                # Use callback-based playback for async
                idx = [0]  # Mutable container for closure
                finished = threading.Event()

                def callback(outdata, frames, time_info, status):
                    with self._lock:
                        if self._stop_requested:
                            outdata.fill(0)
                            raise sd.CallbackStop()

                    start = idx[0]
                    end = min(start + frames, len(samples))
                    out_frames = end - start

                    if out_frames > 0:
                        outdata[:out_frames, 0] = samples[start:end]
                    if out_frames < frames:
                        outdata[out_frames:] = 0
                        raise sd.CallbackStop()

                    idx[0] = end

                def finished_callback():
                    finished.set()

                with sd.OutputStream(
                    samplerate=sample_rate,
                    channels=1,
                    callback=callback,
                    finished_callback=finished_callback,
                    device=self._device_index,
                ):
                    logger.info(f"Speaking async (device={self._device_index}): {text[:50]}...")
                    finished.wait()

            except sd.PortAudioError as e:
                # No audio device - fail gracefully
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

        # Stop sounddevice blocking playback
        sd.stop()

        # Wait for async thread to finish
        if thread is not None:
            thread.join(timeout=1.0)

        with self._lock:
            self._is_speaking = False

