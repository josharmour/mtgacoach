"""Unified voice input API for PTT and VOX modes.

This module provides a high-level VoiceInput class that ties together
audio capture, trigger detection (PTT or VOX), and speech transcription.

Example:
    # PTT mode - press F4 to record
    voice = VoiceInput(mode='ptt', ptt_key='f4')
    voice.start()
    text = voice.wait_for_speech()  # Blocks until F4 pressed and released
    print(f"You said: {text}")
    voice.stop()

    # VOX mode - voice activation
    voice = VoiceInput(mode='vox', vox_threshold=0.02)
    voice.start()
    text = voice.wait_for_speech()  # Blocks until voice detected and finished
    print(f"You said: {text}")
    voice.stop()
"""

import logging
import threading
from typing import Literal, Optional

import numpy as np
import sounddevice as sd

from arenamcp.audio import AudioRecorder, AudioConfig
from arenamcp.transcription import WhisperTranscriber
from arenamcp.triggers import PTTHandler, VOXDetector

logger = logging.getLogger(__name__)


def play_beep(frequency: float = 880, duration: float = 0.1, volume: float = 0.3) -> None:
    """Play a simple beep tone.

    Args:
        frequency: Tone frequency in Hz. Default 880 (A5).
        duration: Duration in seconds. Default 0.1.
        volume: Volume from 0.0 to 1.0. Default 0.3.
    """
    try:
        sample_rate = 44100
        t = np.linspace(0, duration, int(sample_rate * duration), False)
        tone = np.sin(2 * np.pi * frequency * t) * volume
        # Apply quick fade in/out to avoid clicks
        fade_samples = int(sample_rate * 0.01)
        if fade_samples > 0 and len(tone) > fade_samples * 2:
            tone[:fade_samples] *= np.linspace(0, 1, fade_samples)
            tone[-fade_samples:] *= np.linspace(1, 0, fade_samples)
        sd.play(tone.astype(np.float32), sample_rate, blocking=False)
    except Exception as e:
        logger.debug(f"Could not play beep: {e}")


class VoiceInput:
    """Unified voice input interface for PTT and VOX modes.

    Combines audio capture, trigger detection, and transcription into
    a simple API. Supports both push-to-talk (PTT) and voice activation
    (VOX) modes.

    Attributes:
        mode: Either 'ptt' for push-to-talk or 'vox' for voice activation.
    """

    def __init__(
        self,
        mode: Literal["ptt", "vox"] = "ptt",
        ptt_key: str = "f4",
        vox_threshold: float = 0.02,
        vox_silence: float = 1.0,
        model_size: str = "base",
    ) -> None:
        """Initialize VoiceInput.

        Args:
            mode: Voice input mode. 'ptt' for push-to-talk, 'vox' for
                 voice activation. Default 'ptt'.
            ptt_key: Key for push-to-talk. Only used in PTT mode. Default 'f4'.
            vox_threshold: RMS threshold for voice detection. Only used in
                          VOX mode. Default 0.02.
            vox_silence: Seconds of silence before ending VOX recording.
                        Default 1.0.
            model_size: Whisper model size. Default 'base'.
        """
        self.mode = mode
        self._ptt_key = ptt_key
        self._vox_threshold = vox_threshold
        self._vox_silence = vox_silence
        self._model_size = model_size
        self.transcription_enabled = True  # New flag to control local transcription

        # Components (lazy-initialized)
        self._recorder: Optional[AudioRecorder] = None
        self._transcriber: Optional[WhisperTranscriber] = None
        self._trigger: Optional[PTTHandler | VOXDetector] = None

        # State
        self._active = False
        self._result_ready = threading.Event()
        self._last_result: str = ""
        self._last_audio: Optional[np.ndarray] = None  # Raw audio from last recording
        self._lock = threading.Lock()

        # VOX mode background thread
        self._vox_stream: Optional[sd.InputStream] = None
        self._vox_thread: Optional[threading.Thread] = None
        self._vox_buffer: list[np.ndarray] = []
        self._vox_recording = False

    def _ensure_components(self) -> None:
        """Initialize components on first use."""
        if self._recorder is None:
            self._recorder = AudioRecorder(AudioConfig())

        # Only init transcriber if enabled
        if self._transcriber is None and self.transcription_enabled:
            self._transcriber = WhisperTranscriber(model_size=self._model_size)

        if self._trigger is None:
            if self.mode == "ptt":
                self._trigger = PTTHandler(
                    hotkey=self._ptt_key,
                    on_start=self._on_recording_start,
                    on_stop=self._on_recording_stop,
                )
            else:
                self._trigger = VOXDetector(
                    threshold=self._vox_threshold,
                    on_voice_start=self._on_vox_start,
                    on_voice_stop=self._on_vox_stop,
                    silence_duration=self._vox_silence,
                )

    def _on_recording_start(self) -> None:
        """Called when PTT key is pressed."""
        # Play start beep (higher pitch)
        play_beep(frequency=880, duration=0.08, volume=0.25)
        with self._lock:
            self._result_ready.clear()
            if self._recorder is not None:
                self._recorder.start_recording()

    def _on_recording_stop(self) -> None:
        """Called when PTT key is released."""
        # Play stop beep IMMEIDATELY for responsiveness
        # Use simpler beep parameters for speed
        play_beep(frequency=660, duration=0.06, volume=0.25)
        
        with self._lock:
            if self._recorder is not None:
                audio = self._recorder.stop_recording()
                self._last_audio = audio if len(audio) > 0 else None
                
                # Only transcribe if enabled
                if len(audio) > 0 and self.transcription_enabled:
                    # Ensure transcriber exists (might have been skipped in init)
                    if self._transcriber is None:
                        self._transcriber = WhisperTranscriber(model_size=self._model_size)
                    self._last_result = self._transcriber.transcribe(audio)
                else:
                    self._last_result = ""
            else:
                self._last_audio = None
            self._result_ready.set()

    def _on_vox_start(self) -> None:
        """Called when VOX detects voice activity start."""
        with self._lock:
            self._result_ready.clear()
            self._vox_recording = True
            self._vox_buffer = []

    def _on_vox_stop(self) -> None:
        """Called when VOX detects voice activity end."""
        with self._lock:
            self._vox_recording = False
            if self._vox_buffer:
                audio = np.concatenate(self._vox_buffer)
                if audio.ndim > 1:
                    audio = audio.flatten()
                
                self._last_audio = audio  # Save VOX audio too
                
                # Only transcribe if enabled
                if self.transcription_enabled:
                    if self._transcriber is None:
                        self._transcriber = WhisperTranscriber(model_size=self._model_size)
                    self._last_result = self._transcriber.transcribe(audio)
                else:
                    self._last_result = ""
            else:
                self._last_result = ""
                self._last_audio = None
                
            self._vox_buffer = []
            self._result_ready.set()

    def _vox_audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: dict,
        status: sd.CallbackFlags,
    ) -> None:
        """Callback for VOX mode continuous audio monitoring."""
        if self._trigger is None or not isinstance(self._trigger, VOXDetector):
            return

        # Process audio chunk for voice detection
        audio_chunk = indata.copy().flatten()
        self._trigger.process_audio(audio_chunk)

        # If recording, accumulate audio
        with self._lock:
            if self._vox_recording:
                self._vox_buffer.append(audio_chunk)

    def start(self) -> None:
        """Start listening for voice input.

        In PTT mode, registers the global hotkey.
        In VOX mode, starts continuous audio monitoring.
        """
        if self._active:
            return

        self._ensure_components()

        if self.mode == "ptt":
            if self._trigger is not None and isinstance(self._trigger, PTTHandler):
                self._trigger.start()
        else:
            # VOX mode - start continuous audio monitoring
            config = AudioConfig()
            self._vox_stream = sd.InputStream(
                samplerate=config.sample_rate,
                channels=config.channels,
                dtype=config.dtype,
                callback=self._vox_audio_callback,
            )
            self._vox_stream.start()

        self._active = True

    def stop(self) -> None:
        """Stop listening for voice input.

        Cleans up resources and unregisters hotkeys/streams.
        """
        if not self._active:
            return

        if self.mode == "ptt":
            if self._trigger is not None and isinstance(self._trigger, PTTHandler):
                self._trigger.stop()
        else:
            # VOX mode - stop continuous monitoring
            if self._vox_stream is not None:
                self._vox_stream.stop()
                self._vox_stream.close()
                self._vox_stream = None

            # Reset VOX state
            if self._trigger is not None and isinstance(self._trigger, VOXDetector):
                self._trigger.reset()

        self._active = False

    def wait_for_speech(self, timeout: Optional[float] = None) -> str:
        """Block until speech is captured and transcribed.

        Args:
            timeout: Maximum time to wait in seconds. None for no timeout.

        Returns:
            Transcribed text from the speech. Empty string if no speech
            detected or timeout occurred.
        """
        self._result_ready.clear()
        self._result_ready.wait(timeout=timeout)
        return self._last_result

    def get_last_audio(self) -> Optional[np.ndarray]:
        """Get the raw audio from the last recording.

        Returns:
            Numpy array of audio samples (float32, mono), or None if no recording.
        """
        with self._lock:
            return self._last_audio
