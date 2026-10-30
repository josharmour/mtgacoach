"""Local Chatterbox owns tempo; the transport must not rescale it again."""

import base64
import io
import sys
import wave
from types import ModuleType, SimpleNamespace

import numpy as np

from arenamcp.commentary.local_voice_worker import render


def test_local_render_passes_rate_once_and_preserves_native_audio(monkeypatch):
    models = ModuleType("arenaonair.models")
    models.Utterance = lambda *args, **kwargs: SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "arenaonair.models", models)
    seen = []

    def prepare(utterance):
        seen.append(utterance)
        return [(np.zeros(16000, dtype=np.float32), 24000)]

    audio = base64.b64decode(
        render(SimpleNamespace(prepare_audio=prepare), {"text": "A cast.", "speed": 1.5, "voice": "mira"})
    )
    assert len(seen) == 1 and seen[0].rate == 1.5 and seen[0].voice == "mira"
    with wave.open(io.BytesIO(audio)) as wav:
        assert wav.getframerate() == 24000 and wav.getnframes() == 16000
