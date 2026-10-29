"""Explicit local Turbo synthesis benchmark. No playback; never invoked by Coach.

Run only after matches finish. JSON output records cold/warm requests, both voices,
a longer line, deadlines and worker-reported memory. GPU/frame-time contention
and essential audio latency still require paired replay/device qualification.
"""

import argparse
import json
import sys
import threading
import time
import types
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True)
    parser.add_argument("--aoa-source", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--revision", default="749d1c1a46eb10492095d68fbcf55691ccf137cd")
    parser.add_argument("--device", choices=("cpu", "mps", "cuda", "auto"), default="cpu")
    args = parser.parse_args()
    # Avoid importing Coach's application initializer in the benchmark driver.
    package = types.ModuleType("arenamcp")
    package.__path__ = [str(Path(__file__).resolve().parents[2] / "src/arenamcp")]
    sys.modules["arenamcp"] = package
    from arenamcp.local_chatterbox import LocalChatterboxClient

    client = LocalChatterboxClient()
    config = {
        "python": args.python,
        "aoa_source": args.aoa_source,
        "model_dir": args.model_dir,
        "revision": args.revision,
        "device": args.device,
    }
    results = []
    try:
        for voice, text in [
            ("atlas", "A new game is underway."),
            ("atlas", "Player one is at twelve life."),
            ("mira", "Player two is at sixteen life."),
            (
                "mira",
                "A spell goes onto the stack. Its outcome is still unknown, and both players have creatures on the battlefield.",
            ),
        ]:
            started = time.monotonic()
            try:
                audio = client.render(
                    config=config, text=text, voice=voice, speed=1, cancelled=threading.Event()
                )
                results.append(
                    {
                        "voice": voice,
                        "characters": len(text),
                        "seconds": time.monotonic() - started,
                        "audio_bytes": len(audio),
                        "worker": client.last_metrics,
                        "ok": True,
                    }
                )
            except Exception as error:
                results.append(
                    {
                        "voice": voice,
                        "seconds": time.monotonic() - started,
                        "ok": False,
                        "reason": type(error).__name__,
                    }
                )
                break
    finally:
        client.close()
    print(json.dumps({"device": args.device, "revision": args.revision, "runs": results}, indent=2))
    return 0 if len(results) == 4 and all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
