"""Optional local Turbo synthesis worker. No audio playback or model downloads."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import sys
import time
import wave
from pathlib import Path

MODEL_REVISION = "749d1c1a46eb10492095d68fbcf55691ccf137cd"


def validate_model(path, revision):
    folder = Path(path).expanduser().resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", revision or "") or folder.name != revision or not folder.is_dir():
        raise ValueError("Select an immutable model snapshot directory matching its revision")
    return folder


def create_engine(config):
    folder = validate_model(config["model_dir"], config["revision"])
    import torch
    from arenaonair.platform.tts_chatterbox import ChatterboxEngine
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    class PinnedTurbo(ChatterboxEngine):
        def _get_model(self):
            with self._pipeline_lock:
                if self._pipeline is None:
                    device = self.device
                    if device == "auto":
                        device = (
                            "cuda"
                            if torch.cuda.is_available()
                            else "mps"
                            if torch.backends.mps.is_available()
                            else "cpu"
                        )
                    try:
                        self._pipeline = ChatterboxTurboTTS.from_local(folder, device=device)
                    except torch.OutOfMemoryError:
                        if not device.startswith("cuda"):
                            raise
                        device = "cpu"
                        torch.cuda.empty_cache()
                        self._pipeline = ChatterboxTurboTTS.from_local(folder, device=device)
                    self.device = device
                return self._pipeline

        def _resolve_reference(self, voice):
            if voice == "default":
                return None
            if voice not in {"atlas", "mira"}:
                raise ValueError("Unknown packaged voice")
            return str(Path(__file__).resolve().parents[1] / "assets" / "chatterbox" / (voice + ".wav"))

    device = config.get("device", "cpu")
    if device not in {"cpu", "cuda", "mps", "auto"}:
        raise ValueError("Unsupported device")
    torch.set_num_threads(1)
    engine = PinnedTurbo(device=device, model_variant="turbo")
    if engine.preload_voices(["atlas", "mira"]) != 2:
        raise RuntimeError("Local voice initialization failed")
    return engine


def render(engine, payload):
    import numpy as np
    from arenaonair.models import Utterance

    text, speed, voice = payload["text"], float(payload.get("speed", 1)), payload.get("voice", "atlas")
    if (
        not isinstance(text, str)
        or not 0 < len(text) <= 600
        or not 0.5 <= speed <= 2
        or voice not in {"atlas", "mira", "default"}
    ):
        raise ValueError("Invalid render request")
    chunks = engine.prepare_audio(Utterance("render", None, "speech", text, 1, 0, rate=speed, voice=voice))
    output = io.BytesIO()
    total = 0
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        for pcm, sr in chunks:
            if sr != 24000 or not np.isfinite(pcm).all():
                raise ValueError("Invalid PCM")
            raw = (np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes()
            total += len(raw)
            if total > 20_000_000:
                raise ValueError("Audio size limit")
            wav.writeframes(raw)
    if not total:
        raise ValueError("Empty audio")
    return base64.b64encode(output.getvalue()).decode("ascii")


def main():
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    engine = None
    try:
        if hasattr(os, "nice"):
            os.nice(10)
        for line in sys.stdin:
            request = json.loads(line)
            if request.get("cmd") == "shutdown":
                break
            started = time.monotonic()
            with contextlib.redirect_stdout(sys.stderr):
                if request.get("cmd") == "init":
                    engine = create_engine(request)
                    result = {"type": "ready", "device": engine.device}
                else:
                    result = {"type": "rendered", "audio": render(engine, request)}
            metrics = {"seconds": time.monotonic() - started, "device": engine.device}
            try:
                import resource

                # ru_maxrss is bytes on macOS, KiB on Linux; absent on Windows.
                metrics["peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
                    1 if sys.platform == "darwin" else 1024
                )
            except ImportError:
                pass
            if engine.device.startswith("cuda"):
                import torch

                metrics["peak_gpu_bytes"] = torch.cuda.max_memory_allocated()
            result["metrics"] = metrics
            print(json.dumps(result), flush=True)
    except Exception as error:
        print(json.dumps({"type": "error", "reason": type(error).__name__}), flush=True)
        return 1  # release a failed model, including generation-time OOM
    finally:
        if engine:
            engine.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
