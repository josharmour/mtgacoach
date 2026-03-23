"""Audio capture infrastructure for voice input.

This module provides non-blocking audio recording using sounddevice for
voice input capture. It uses a callback-based InputStream pattern that
will integrate with PTT (Push-to-Talk) and VOX (Voice Activation) modes.
"""

import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np
import sounddevice as sd


@dataclass
class AudioConfig:
    """Configuration for audio recording.

    Attributes:
        sample_rate: Sample rate in Hz. Default 16000 (Whisper's native rate).
        channels: Number of audio channels. Default 1 (mono for voice).
        dtype: Numpy dtype for audio samples. Default 'float32'.
        device: Audio input device index or name. None uses system default.
    """

    sample_rate: int = 16000
    channels: int = 1
    dtype: str = "float32"
    device: Optional[int | str] = None


class AudioRecorder:
    """Non-blocking audio recorder using sounddevice.

    Uses a callback-based InputStream pattern for PTT/VOX integration.
    Thread-safe buffer management allows concurrent recording and access.

    Example:
        recorder = AudioRecorder()
        recorder.start_recording()
        time.sleep(1.0)
        audio = recorder.stop_recording()
        # audio is numpy array of shape (samples,) at 16kHz float32
    """

    def __init__(self, config: Optional[AudioConfig] = None) -> None:
        """Initialize the audio recorder.

        Args:
            config: Audio configuration. If None, uses default AudioConfig.
        """
        self.config = config or AudioConfig()
        self._stream: Optional[sd.InputStream] = None
        self._buffer: list[np.ndarray] = []
        self._recording: bool = False
        self._lock = threading.Lock()

    @property
    def is_recording(self) -> bool:
        """Check if the recorder is currently recording.

        Returns:
            True if recording is in progress, False otherwise.
        """
        return self._recording

    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: dict,
        status: sd.CallbackFlags,
    ) -> None:
        """Callback for sounddevice InputStream.

        Appends incoming audio data to the buffer. Thread-safe via lock.

        Args:
            indata: Incoming audio data as numpy array.
            frames: Number of frames in the buffer.
            time_info: Time info dict from sounddevice.
            status: Callback status flags.
        """
        if status:
            # Log status issues (overflow, underflow, etc.)
            pass
        with self._lock:
            # Copy to avoid referencing sounddevice's internal buffer
            self._buffer.append(indata.copy())

    def start_recording(self) -> None:
        """Begin capturing audio to internal buffer.

        Creates a non-blocking InputStream with callback that appends
        audio chunks to the buffer. Does nothing if already recording.

        Raises:
            sd.PortAudioError: If audio device is not available.
        """
        if self._recording:
            return

        with self._lock:
            self._buffer = []

        self._stream = sd.InputStream(
            samplerate=self.config.sample_rate,
            channels=self.config.channels,
            dtype=self.config.dtype,
            device=self.config.device,
            callback=self._audio_callback,
        )
        self._stream.start()
        self._recording = True

    def stop_recording(self) -> np.ndarray:
        """Stop capture and return recorded audio.

        Stops the input stream and concatenates all buffered audio
        chunks into a single numpy array.

        Returns:
            Numpy array of recorded audio samples, shape (samples,).
            Returns empty array if not recording or no audio captured.
        """
        if not self._recording:
            return np.array([], dtype=self.config.dtype)

        self._recording = False

        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        with self._lock:
            if not self._buffer:
                return np.array([], dtype=self.config.dtype)
            # Concatenate all chunks and flatten to 1D
            audio = np.concatenate(self._buffer)
            self._buffer = []

        # Flatten to 1D (remove channel dimension for mono)
        if audio.ndim > 1:
            audio = audio.flatten()

        return audio
