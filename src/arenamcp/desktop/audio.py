from __future__ import annotations

import sys
import threading
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


class AudioPlayback:
    _lock = threading.RLock()

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
                    return True
                except Exception:
                    return False
        else:
            if sd is None or sf is None:
                # Fallback: platform-appropriate command-line players.
                # macOS ships afplay (plays wav/mp3 natively); Linux desktops
                # typically have one of paplay/aplay/play/pw-play.
                import shutil
                import subprocess
                if sys.platform == "darwin":
                    players: tuple[str, ...] = ("afplay",)
                else:
                    players = ("paplay", "aplay", "play", "pw-play")
                for player in players:
                    if shutil.which(player):
                        try:
                            # Run in background to be asynchronous
                            subprocess.Popen(
                                [player, str(full_path)],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL
                            )
                            return True
                        except Exception:
                            pass
                return False

            with cls._lock:
                cls._stop_unlocked()
                try:
                    data, fs = sf.read(str(full_path), dtype='float32')
                    sd.play(data, fs)
                    return True
                except Exception:
                    return False

    @classmethod
    def stop(cls) -> None:
        with cls._lock:
            cls._stop_unlocked()

    @classmethod
    def _stop_unlocked(cls) -> None:
        if winsound is not None:
            try:
                winsound.PlaySound(None, winsound.SND_PURGE)
            except RuntimeError:
                pass
        else:
            if sd is not None:
                try:
                    sd.stop()
                except Exception:
                    pass
