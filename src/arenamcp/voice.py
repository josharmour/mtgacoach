"""Voice utilities and audio feedback cues."""

from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)


def play_beep(frequency: float = 880, duration: float = 0.1, volume: float = 0.3) -> None:
    """Play a simple beep tone for audio feedback.

    Args:
        frequency: Tone frequency in Hz. Default 880 (A5).
        duration: Duration in seconds. Default 0.1.
        volume: Volume from 0.0 to 1.0. Default 0.3.
    """
    try:
        import sounddevice as sd

        sample_rate = 44100
        t = np.linspace(0, duration, int(sample_rate * duration), False)
        tone = np.sin(2 * np.pi * frequency * t) * volume
        fade_samples = int(sample_rate * 0.01)
        if fade_samples > 0 and len(tone) > fade_samples * 2:
            tone[:fade_samples] *= np.linspace(0, 1, fade_samples)
            tone[-fade_samples:] *= np.linspace(1, 0, fade_samples)
        sd.play(tone.astype(np.float32), sample_rate, blocking=False)
    except Exception as e:
        logger.debug(f"Could not play beep: {e}")
