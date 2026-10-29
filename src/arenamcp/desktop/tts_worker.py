from __future__ import annotations

import contextlib
import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _emit(event: dict[str, Any]) -> None:
    try:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        raise SystemExit(0)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[tts-worker] %(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )


def _lower_priority() -> None:
    """Run speech synthesis below the game so a spoken line never costs Arena frames.

    2026-10-18 22:40 (Asahi Linux, Arena in a VM on the 4 performance cores):
    each line's Kokoro burst took those cores and Arena stuttered. At below-normal
    priority the scheduler still uses idle cores, so speech stays real-time.
    """
    try:
        if sys.platform == "win32":
            import ctypes

            below_normal = 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS
            kernel32 = ctypes.windll.kernel32
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), below_normal)
        else:
            import os

            os.nice(10)
    except Exception:  # noqa: BLE001 - priority is an optimisation, never fatal
        logger.debug("could not lower TTS worker priority", exc_info=True)


def main() -> int:
    _configure_logging()
    _lower_priority()

    try:
        from arenamcp.tts import VoiceOutput

        # The desktop owns mute and playback. A muted launch must still be
        # able to render after unmuting, without rewriting saved preferences.
        voice = VoiceOutput(render_only=True)
        logger.info("warming Kokoro worker")
        voice.warmup()
        probe = voice.render_to_wav_file("Ready")
        if probe is None:
            raise RuntimeError("Kokoro warmup probe produced no audio")
        probe_path, _ = probe
        with contextlib.suppress(OSError):
            Path(probe_path).unlink(missing_ok=True)
        voice_id, voice_name = voice.current_voice
        _emit(
            {
                "type": "ready",
                "voice_id": voice_id,
                "voice_name": voice_name,
                "speed": float(getattr(voice, "speed", getattr(voice, "_speed", 1.0))),
            }
        )
    except Exception as exc:
        logger.exception("failed to initialize Kokoro worker")
        _emit({"type": "error", "message": f"Kokoro worker init failed: {exc}"})
        return 1

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue

        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning("bad worker command: %s", exc)
            _emit({"type": "error", "message": f"Bad worker command: {exc}"})
            continue

        command = str(payload.get("cmd", "")).strip().lower()
        if command == "shutdown":
            return 0

        if command == "stop":
            continue

        if command != "render":
            _emit({"type": "error", "message": f"Unknown worker command: {command}"})
            continue

        generation = int(payload.get("generation", 0))
        text = str(payload.get("text", ""))
        voice_id = str(payload.get("voice_id", "")).strip()
        speed = payload.get("speed")

        try:
            if voice_id:
                voice.set_voice(voice_id)
            if speed is not None:
                voice.set_speed(float(speed))

            rendered = voice.render_to_wav_file(text)
            active_voice_id, active_voice_name = voice.current_voice
            if rendered is None:
                _emit(
                    {
                        "type": "error",
                        "generation": generation,
                        "message": "Kokoro render produced no audio.",
                    }
                )
                continue

            path, duration = rendered
            _emit(
                {
                    "type": "rendered",
                    "generation": generation,
                    "text": text,
                    "path": path,
                    "duration": round(duration, 3),
                    "voice_id": active_voice_id,
                    "voice_name": active_voice_name,
                    "speed": float(getattr(voice, "speed", getattr(voice, "_speed", 1.0))),
                }
            )
        except Exception as exc:
            logger.exception("render failed")
            _emit(
                {
                    "type": "error",
                    "generation": generation,
                    "message": f"Kokoro render failed: {exc}",
                    "traceback": traceback.format_exc(limit=6),
                }
            )

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
