"""Isolated stdin-only public speech worker. Never download weights or echo text."""

import base64
import json
import os
import sys
from importlib.metadata import version
from pathlib import Path

SPEAKERS = {"play_by_play": "Ryan", "color_analyst": "Serena"}
STYLE = {
    "calm": "Speak quietly and evenly, with a relaxed, conversational cadence.",
    "normal": "Sound engaged and conversational, with natural pauses and clear emphasis.",
    "tense": "Build anticipation with tighter phrasing and focused emphasis on the turning point.",
    "electric": "React to the deciding play with an excited rise in pitch, punch the crucial words, then land the final phrase with energy.",
}


def instruction(role, level, pace):
    character = (
        "Call live play as a sports broadcaster."
        if role == "play_by_play"
        else "Explain the play as a thoughtful color analyst in conversation with the lead."
    )
    return f"{character} {STYLE[level]} Aim for {round(150 * pace)} words per minute."


def serve(snapshot, probe, send):
    import mlx.core as mx
    import numpy as np
    from mlx_audio.tts.utils import load_model

    if (
        version("mlx-audio") != "0.6.5"
        or version("mlx") != "0.32.4"
        or version("mlx-metal") != "0.32.4"
        or version("transformers") != "5.23.0"
        or not mx.metal.is_available()
        or (snapshot / "model.safetensors").stat().st_size != 2393308931
        or (snapshot / "speech_tokenizer/model.safetensors").stat().st_size != 682293092
    ):
        raise ValueError()
    required = (
        "generation_config.json",
        "merges.txt",
        "model.safetensors.index.json",
        "preprocessor_config.json",
        "tokenizer_config.json",
        "vocab.json",
        "speech_tokenizer/config.json",
        "speech_tokenizer/configuration.json",
        "speech_tokenizer/preprocessor_config.json",
    )
    if not all((snapshot / name).is_file() for name in required):
        raise ValueError()
    cfg = json.loads((snapshot / "config.json").read_bytes())
    if cfg["talker_config"]["spk_id"]["ryan"] != 3061 or cfg["talker_config"]["spk_id"]["serena"] != 3066:
        raise ValueError()
    if probe:
        send(event="available")
        return
    model = load_model(str(snapshot))

    def render(text, role, level, pace):
        for part in model.generate_custom_voice(
            text=text,
            speaker=SPEAKERS[role],
            language="English",
            instruct=instruction(role, level, pace),
            stream=True,
            streaming_interval=0.32,
            max_tokens=min(1200, max(128, len(text) * 3)),
            verbose=False,
        ):
            if part.sample_rate != 24000:
                raise ValueError()
            audio = np.asarray(part.audio).reshape(-1)
            if audio.size:
                yield (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()

    # Compile the native graph once while the user is setting up the booth.
    if not any(render("Ready.", "play_by_play", "calm", 1.0)):
        raise ValueError()
    send(event="ready")
    for line in sys.stdin.buffer:
        try:
            request = json.loads(line)
            text, role, level, pace = (request[k] for k in ("text", "role", "excitement", "speed"))
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > 600
                or role not in SPEAKERS
                or level not in STYLE
                or type(pace) not in (float, int)
                or not 0.5 <= pace <= 2.0
            ):
                raise ValueError()
            frames = 0
            for pcm in render(text, role, level, pace):
                frames += len(pcm) // 2
                send(event="pcm", data=base64.b64encode(pcm).decode("ascii"))
            if not frames:
                raise ValueError()
            send(event="done")
        except Exception:
            send(event="failed")


def main():
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
    protocol = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)  # Third-party initialization and native output stay off the protocol pipe.

    def send(**packet):
        protocol.write(json.dumps(packet, separators=(",", ":")) + "\n")

    try:
        serve(Path(sys.argv[1]), sys.argv[2:] == ["--probe"], send)
    except Exception:
        send(event="failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
