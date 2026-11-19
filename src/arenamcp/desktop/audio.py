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
if winsound is None and sys.platform != "darwin":
    # Mac playback uses afplay. Importing PortAudio probes input devices too,
    # causing unnecessary microphone prompts and delaying the first window.
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
    _volume = 1.0
    _scaled_tmp_path: Path | None = None

    @classmethod
    def get_volume(cls) -> float:
        with cls._lock:
            return cls._volume

    @classmethod
    def set_volume(cls, volume: float) -> float:
        from arenamcp.settings import clamp_voice_volume

        with cls._lock:
            cls._volume = clamp_voice_volume(volume, default=cls._volume)
            return cls._volume

    @classmethod
    def _cleanup_scaled_tmp_unlocked(cls) -> None:
        tmp = cls._scaled_tmp_path
        cls._scaled_tmp_path = None
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    @classmethod
    def _write_scaled_wav(cls, full_path: Path, volume: float) -> Path | None:
        import array
        import tempfile
        import wave

        try:
            with wave.open(str(full_path), "rb") as src:
                params = src.getparams()
                if params.sampwidth != 2:
                    return None
                raw = src.readframes(params.nframes)
        except Exception:
            return None

        try:
            try:
                import numpy as np

                samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
                scaled_bytes = np.clip(np.rint(samples * volume), -32768, 32767).astype("<i2").tobytes()
            except ImportError:
                arr = array.array("h")
                arr.frombytes(raw)
                if sys.byteorder == "big":
                    arr.byteswap()
                for idx, sample in enumerate(arr):
                    scaled = round(sample * volume)
                    arr[idx] = max(-32768, min(32767, scaled))
                if sys.byteorder == "big":
                    arr.byteswap()
                scaled_bytes = arr.tobytes()

            with tempfile.NamedTemporaryFile(
                suffix=".wav",
                prefix="arenamcp_vol_",
                delete=False,
            ) as tmp_file:
                tmp_path = Path(tmp_file.name)
            with wave.open(str(tmp_path), "wb") as dst:
                dst.setparams(params)
                dst.writeframes(scaled_bytes)
            return tmp_path
        except Exception:
            return None

    @classmethod
    def play_file(cls, path: str, volume: float | None = None) -> bool:
        from arenamcp.settings import clamp_voice_volume

        if not path:
            return False

        full_path = Path(path).resolve()
        if not full_path.exists():
            return False

        with cls._lock:
            base_vol = cls._volume
        vol = base_vol if volume is None else clamp_voice_volume(volume, default=base_vol)

        if winsound is not None:
            with cls._lock:
                cls._stop_unlocked()
                try:
                    play_path = full_path
                    if vol < 0.999:
                        scaled = cls._write_scaled_wav(full_path, vol)
                        if scaled is not None:
                            cls._scaled_tmp_path = scaled
                            play_path = scaled
                    winsound.PlaySound(
                        str(play_path),
                        winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                    )
                    cls._expected_end = time.monotonic() + _wav_seconds(full_path) + 0.3
                    return True
                except Exception:
                    cls._cleanup_scaled_tmp_unlocked()
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
                    return cls._play_via_cli_unlocked(full_path, ("afplay",), volume=vol)

            if sd is None or sf is None:
                # Fallback: command-line players. Linux desktops typically
                # have one of paplay/aplay/play/pw-play.
                with cls._lock:
                    cls._stop_unlocked()
                    return cls._play_via_cli_unlocked(
                        full_path, ("paplay", "aplay", "play", "pw-play"), volume=vol
                    )

            with cls._lock:
                cls._stop_unlocked()
                try:
                    data, fs = sf.read(str(full_path), dtype="float32")
                    if vol < 0.999:
                        data = data * vol
                    sd.play(data, fs)
                    cls._expected_end = time.monotonic() + (len(data) / fs if fs else 0.0) + 0.3
                    return True
                except Exception:
                    return False

    _cli_process = None

    @classmethod
    def _play_via_cli_unlocked(cls, full_path: Path, players: tuple[str, ...], volume: float = 1.0) -> bool:
        import shutil
        import subprocess

        scaled_wav: Path | None = None
        for player in players:
            if shutil.which(player):
                try:
                    if volume >= 0.999:
                        cmd = [player, str(full_path)]
                    elif player in ("afplay", "play"):
                        cmd = [player, "-v", f"{volume:.3f}", str(full_path)]
                    elif player == "paplay":
                        pa_vol = max(0, min(65536, round(volume * 65536)))
                        cmd = [player, f"--volume={pa_vol}", str(full_path)]
                    else:
                        if scaled_wav is None:
                            scaled_wav = cls._write_scaled_wav(full_path, volume)
                            if scaled_wav is not None:
                                cls._scaled_tmp_path = scaled_wav
                        cmd = [player, str(scaled_wav or full_path)]
                    # Run in background to be asynchronous; track the process
                    # so stop() can interrupt playback.
                    cls._cli_process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    return True
                except Exception:
                    pass
        cls._cleanup_scaled_tmp_unlocked()
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
        cls._cleanup_scaled_tmp_unlocked()
