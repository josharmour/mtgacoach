from __future__ import annotations

import contextlib
import sys
import threading
import time
import wave
from pathlib import Path

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("This module is not executable")

try:
    import winsound
except ImportError:  # pragma: no cover - non-Windows
    winsound = None  # type: ignore[assignment]

sd = None
sf = None
if winsound is None:
    try:
        import sounddevice as sd
        import soundfile as sf
    except ImportError:
        pass


def _wav_seconds(path: Path) -> float:
    """Clip length for players that cannot report completion (winsound)."""
    try:
        with wave.open(str(path), "rb") as clip:
            rate = clip.getframerate()
            if rate > 0:
                return clip.getnframes() / rate
    except (wave.Error, OSError, EOFError):
        pass
    # Kokoro writes 24 kHz 16-bit mono; size bounds the length if the header is unreadable.
    try:
        return path.stat().st_size / 48000
    except OSError:
        return 0.0


class AudioPlayback:
    _lock = threading.RLock()
    # Monotonic time playback should end for players without a process to poll.
    _expected_end = 0.0

    @classmethod
    def play_file(cls, path: str) -> bool:
        if not path:
            return False

        full_path = Path(path).resolve()
        if not full_path.exists():
            return False

        if winsound is not None:
            with cls._lock:
                cls._stop_unlocked()
                try:
                    winsound.PlaySound(
                        str(full_path),
                        winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                    )
                    cls._expected_end = time.monotonic() + _wav_seconds(full_path) + 0.3
                    return True
                except Exception:
                    return False
        else:
            if sys.platform == "darwin":
                # Always use afplay on macOS, even when sounddevice is
                # available: PortAudio snapshots the device list at init, so
                # audio keeps playing from the built-in speakers after the
                # user switches to (or connects) Bluetooth headphones.
                # afplay follows the live system default output.
                with cls._lock:
                    cls._stop_unlocked()
                    return cls._play_via_cli_unlocked(full_path, ("afplay",))

            if sd is None or sf is None:
                # Fallback: command-line players. Linux desktops typically
                # have one of paplay/aplay/play/pw-play.
                with cls._lock:
                    cls._stop_unlocked()
                    return cls._play_via_cli_unlocked(full_path, ("paplay", "aplay", "play", "pw-play"))

            with cls._lock:
                cls._stop_unlocked()
                try:
                    data, fs = sf.read(str(full_path), dtype="float32")
                    sd.play(data, fs)
                    cls._expected_end = time.monotonic() + (len(data) / fs if fs else 0.0) + 0.3
                    return True
                except Exception:
                    return False

    _cli_process = None

    @classmethod
    def _play_via_cli_unlocked(cls, full_path: Path, players: tuple[str, ...]) -> bool:
        import shutil
        import subprocess

        for player in players:
            if shutil.which(player):
                try:
                    # Run in background to be asynchronous; track the process
                    # so stop() can interrupt playback.
                    cls._cli_process = subprocess.Popen(
                        [player, str(full_path)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    return True
                except Exception:
                    pass
        return False

    @classmethod
    def is_playing(cls) -> bool:
        """True until the current clip ends naturally or is stopped."""
        with cls._lock:
            proc = cls._cli_process
            if proc is not None:
                return proc.poll() is None
            return time.monotonic() < cls._expected_end

    @classmethod
    def stop(cls) -> None:
        with cls._lock:
            cls._stop_unlocked()

    @classmethod
    def _stop_unlocked(cls) -> None:
        cls._expected_end = 0.0
        if winsound is not None:
            with contextlib.suppress(RuntimeError):
                winsound.PlaySound(None, winsound.SND_PURGE)
        else:
            if sd is not None:
                with contextlib.suppress(Exception):
                    sd.stop()
            proc = cls._cli_process
            cls._cli_process = None
            if proc is not None and proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.terminate()
