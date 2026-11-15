"""Local, explicitly selected public commentary on Apple Silicon."""

import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

PROVIDER = "qwen_public"
PAIR = ("Ryan", "Serena")
REVISION = "41d3337e8b7f2843a75841595fc14e4b9a7a4b96"


def selected(settings):
    return (
        settings.get("commentary_public_voice") == PROVIDER
        and settings.get("tts_provider", "kokoro") == "kokoro"
        and settings.get("autoplay_speech") == "commentary"
    )


def command():
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return None
    python = Path.home() / ".arenamcp/voices/qwen3/bin/python"
    snapshot = (
        Path.home()
        / ".cache/huggingface/hub/models--mlx-community--Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit"
        / "snapshots"
        / REVISION
    )
    sandbox = Path("/usr/bin/sandbox-exec")
    if not (sandbox.is_file() and os.access(python, os.X_OK) and (snapshot / "config.json").is_file()):
        return None
    return [
        str(sandbox),
        "-p",
        "(version 1)(allow default)(deny network*)",
        str(python),
        "-I",
        "-B",
        "-u",
        str(Path(__file__).with_name("qwen_public_worker.py")),
        str(snapshot),
    ]


_probe_lock = threading.Lock()
_last_probe = (0.0, False)


def available():
    global _last_probe
    with _probe_lock:
        if time.monotonic() - _last_probe[0] > 60 or not _last_probe[0]:
            _last_probe = time.monotonic(), _probe()
        return _last_probe[1]


def _probe():
    args = command()
    if args is None:
        return False
    try:
        result = subprocess.run(
            [*args, "--probe"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == b'{"event":"available"}'
