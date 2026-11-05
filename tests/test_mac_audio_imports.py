"""Desktop output and render workers must not initialize PortAudio on import."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS uses afplay for file playback")
def test_desktop_and_render_worker_import_without_portaudio():
    source = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc, sys
class RejectPortAudio(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'sounddevice':
            raise AssertionError('Output-only startup imported PortAudio')
sys.meta_path.insert(0, RejectPortAudio())
from arenamcp.desktop import audio
from arenamcp import tts
assert audio.sd is None and tts.sd is None
""",
        ],
        env={**os.environ, "PYTHONPATH": str(source)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
