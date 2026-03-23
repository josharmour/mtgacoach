"""Voice input trigger mechanisms for PTT and VOX modes.

This module provides push-to-talk (PTT) and voice activation (VOX) triggers
for controlling when audio is captured and transcribed.

NOTE: The keyboard library requires admin/root privileges on Linux but
works normally on Windows without elevation.
"""

import time
from typing import Callable, Optional

import keyboard
import numpy as np


class PTTHandler:
    """Push-to-talk handler with global hotkey support.

    Registers a global hotkey that triggers callbacks on press/release,
    working even when the application window doesn't have focus.

    Example:
        ptt = PTTHandler(
            hotkey='f4',
            on_start=lambda: recorder.start_recording(),
            on_stop=lambda: process(recorder.stop_recording())
        )
        ptt.start()
        # F4 now controls recording globally
        ptt.stop()

    Note:
        On Linux, requires root privileges for global hotkey capture.
        On Windows, works without elevation.
    """

    def __init__(
        self,
        hotkey: str = "f4",
        on_start: Optional[Callable[[], None]] = None,
        on_stop: Optional[Callable[[], None]] = None,
    ) -> None:
        """Initialize the PTT handler.

        Args:
            hotkey: Key to use for push-to-talk. Default 'f4'.
            on_start: Callback when key is pressed.
            on_stop: Callback when key is released.
        """
        self.hotkey = hotkey
        self.on_start = on_start
        self.on_stop = on_stop
        self._active = False
        self._hook = None

    def start(self) -> None:
        """Register global hotkey listeners.

        Begins listening for the configured hotkey globally.
        Does nothing if already started.
        """
        if self._active:
            return

        # Use hook_key to capture both press and release in a single hook
        self._hook = keyboard.hook_key(self.hotkey, self._on_event)
        self._active = True

    def stop(self) -> None:
        """Unregister hotkey listeners.

        Stops listening for the hotkey. Safe to call multiple times.
        """
        if not self._active:
            return

        if self._hook is not None:
            try:
                keyboard.unhook(self._hook)
            except (ValueError, KeyError):
                pass  # Hook already removed
            self._hook = None
        self._active = False

    def _on_event(self, event: keyboard.KeyboardEvent) -> None:
        """Handle key event (press or release).

        Args:
            event: Keyboard event from the keyboard library.
        """
        if event.event_type == keyboard.KEY_DOWN:
            if self.on_start is not None:
                self.on_start()
        elif event.event_type == keyboard.KEY_UP:
            if self.on_stop is not None:
                self.on_stop()


class VOXDetector:
    """Voice activity detector using RMS threshold.

    Detects voice activity in audio chunks using root mean square (RMS)
    amplitude. Includes a silence grace period to avoid choppy detection
    during natural speech pauses.

    Example:
        vox = VOXDetector(
            threshold=0.02,
            on_voice_start=lambda: recorder.start_recording(),
            on_voice_stop=lambda: process(recorder.stop_recording())
        )
        # In audio callback:
        vox.process_audio(audio_chunk)
    """

    def __init__(
        self,
        threshold: float = 0.02,
        on_voice_start: Optional[Callable[[], None]] = None,
        on_voice_stop: Optional[Callable[[], None]] = None,
        silence_duration: float = 1.0,
    ) -> None:
        """Initialize the VOX detector.

        Args:
            threshold: RMS threshold for voice detection. Default 0.02.
            on_voice_start: Callback when voice activity starts.
            on_voice_stop: Callback when voice activity ends.
            silence_duration: Seconds of silence before triggering stop.
        """
        self.threshold = threshold
        self.on_voice_start = on_voice_start
        self.on_voice_stop = on_voice_stop
        self.silence_duration = silence_duration

        self._is_speaking = False
        self._silence_start: Optional[float] = None

    def _calculate_rms(self, audio: np.ndarray) -> float:
        """Calculate root mean square of audio samples.

        Args:
            audio: Numpy array of audio samples.

        Returns:
            RMS value of the audio.
        """
        if len(audio) == 0:
            return 0.0
        return float(np.sqrt(np.mean(audio**2)))

    def process_audio(self, audio_chunk: np.ndarray) -> bool:
        """Process audio chunk and detect voice activity.

        Updates internal state and triggers callbacks when voice
        activity starts or stops.

        Args:
            audio_chunk: Numpy array of audio samples.

        Returns:
            True if voice is currently detected, False otherwise.
        """
        rms = self._calculate_rms(audio_chunk)
        current_time = time.time()

        if rms > self.threshold:
            # Voice detected
            if not self._is_speaking:
                # Voice just started
                self._is_speaking = True
                self._silence_start = None
                if self.on_voice_start is not None:
                    self.on_voice_start()
            else:
                # Still speaking, cancel any silence timer
                self._silence_start = None
        else:
            # Silence detected
            if self._is_speaking:
                if self._silence_start is None:
                    # Start silence timer
                    self._silence_start = current_time
                elif current_time - self._silence_start >= self.silence_duration:
                    # Silence exceeded duration threshold
                    self._is_speaking = False
                    self._silence_start = None
                    if self.on_voice_stop is not None:
                        self.on_voice_stop()

        return self._is_speaking

    def reset(self) -> None:
        """Reset internal state.

        Clears speaking state and silence timer without triggering callbacks.
        """
        self._is_speaking = False
        self._silence_start = None
