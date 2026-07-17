"""Unit tests for the macOS voice-output fallbacks.

Covers:
- standalone._SAPIVoice: uses `say` on darwin, PowerShell SAPI on Windows
- desktop.audio.AudioPlayback: CLI fallback chain picks afplay on darwin
"""
from __future__ import annotations

import pytest

from arenamcp import standalone
from arenamcp.desktop import audio


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeSettings:
    def __init__(self, data: dict | None = None) -> None:
        self._data = dict(data or {})

    def get(self, key, default=None):
        return self._data.get(key, default)


class FakeStdin:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, chunk: bytes) -> None:
        self.data += chunk

    def close(self) -> None:
        self.closed = True


class FakeProc:
    def __init__(self) -> None:
        self.stdin = FakeStdin()
        self.wait_timeout: float | None = None
        self.terminated = False
        self._returncode: int | None = None

    def wait(self, timeout=None):
        self.wait_timeout = timeout
        self._returncode = 0
        return 0

    def poll(self):
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        self._returncode = -15


@pytest.fixture
def popen_calls(monkeypatch):
    """Record every subprocess.Popen call made by standalone._SAPIVoice."""
    calls: list[tuple[list, dict, FakeProc]] = []

    def fake_popen(cmd, **kwargs):
        proc = FakeProc()
        calls.append((cmd, kwargs, proc))
        return proc

    monkeypatch.setattr(standalone.subprocess, "Popen", fake_popen)
    return calls


def _make_darwin_voice(monkeypatch, settings_data: dict | None = None):
    monkeypatch.setattr(standalone.sys, "platform", "darwin")
    monkeypatch.setattr(
        standalone, "get_settings", lambda: FakeSettings(settings_data)
    )
    return standalone._SAPIVoice()


# ---------------------------------------------------------------------------
# _SAPIVoice on darwin -> `say`
# ---------------------------------------------------------------------------

def test_darwin_speak_uses_say(monkeypatch, popen_calls) -> None:
    voice = _make_darwin_voice(monkeypatch, {"voice_speed": 1.0})
    voice.speak("Hello there", blocking=True)

    assert len(popen_calls) == 1
    cmd, kwargs, proc = popen_calls[0]
    assert cmd == ["say", "-r", "175"]
    assert "creationflags" not in kwargs
    assert proc.stdin.data == b"Hello there"
    assert proc.stdin.closed
    assert proc.wait_timeout == 30


def test_darwin_current_voice(monkeypatch, popen_calls) -> None:
    voice = _make_darwin_voice(monkeypatch)
    assert voice.current_voice == ("say", "macOS say")


def test_darwin_rate_mapping_from_voice_speed(monkeypatch, popen_calls) -> None:
    voice = _make_darwin_voice(monkeypatch, {"voice_speed": 1.2})
    voice.speak("check rate")
    cmd, _, _ = popen_calls[0]
    assert cmd[:3] == ["say", "-r", "210"]  # 175 * 1.2


def test_say_wpm_clamps_extremes() -> None:
    assert standalone._SAPIVoice._say_wpm(1.0) == 175
    assert standalone._SAPIVoice._say_wpm(0.01) == 90  # floor
    assert standalone._SAPIVoice._say_wpm(10.0) == 450  # ceiling
    assert standalone._SAPIVoice._say_wpm("garbage") == 175  # non-numeric


def test_darwin_voice_name_passthrough(monkeypatch, popen_calls) -> None:
    voice = _make_darwin_voice(monkeypatch, {"macos_voice": "Samantha"})
    assert voice.current_voice == ("say", "macOS say (Samantha)")
    voice.speak("with a named voice")
    cmd, _, _ = popen_calls[0]
    assert cmd == ["say", "-r", "175", "-v", "Samantha"]


def test_darwin_settings_failure_falls_back_to_defaults(
    monkeypatch, popen_calls
) -> None:
    monkeypatch.setattr(standalone.sys, "platform", "darwin")

    def boom():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(standalone, "get_settings", boom)
    voice = standalone._SAPIVoice()
    voice.speak("still speaks")
    cmd, _, _ = popen_calls[0]
    assert cmd == ["say", "-r", "175"]


def test_darwin_speak_strips_markup(monkeypatch, popen_calls) -> None:
    voice = _make_darwin_voice(monkeypatch)
    voice.speak("**Attack** with `Bear` [TAG] now...")
    _, _, proc = popen_calls[0]
    spoken = proc.stdin.data.decode("utf-8")
    for token in ("*", "`", "[TAG]"):
        assert token not in spoken
    assert "Attack" in spoken and "Bear" in spoken


def test_darwin_muted_does_not_spawn(monkeypatch, popen_calls) -> None:
    voice = _make_darwin_voice(monkeypatch)
    assert voice.toggle_mute() is True
    voice.speak("should be silent")
    assert popen_calls == []


def test_darwin_stop_terminates_running_say(monkeypatch, popen_calls) -> None:
    voice = _make_darwin_voice(monkeypatch)
    voice.speak("long sentence", blocking=False)
    _, _, proc = popen_calls[0]
    assert voice.is_speaking
    voice.stop()
    assert proc.terminated
    assert not voice.is_speaking


# ---------------------------------------------------------------------------
# _SAPIVoice on Windows -> PowerShell SAPI (unchanged behavior)
# ---------------------------------------------------------------------------

def test_windows_speak_uses_powershell_sapi(monkeypatch, popen_calls) -> None:
    monkeypatch.setattr(standalone.sys, "platform", "win32")

    def boom():
        raise AssertionError("get_settings must not be called off-darwin")

    monkeypatch.setattr(standalone, "get_settings", boom)

    voice = standalone._SAPIVoice()
    assert voice.current_voice == ("sapi", "Windows SAPI")
    voice.speak("it's your turn", blocking=True)

    assert len(popen_calls) == 1
    cmd, kwargs, proc = popen_calls[0]
    assert cmd[0] == "powershell"
    assert cmd[1:3] == ["-NoProfile", "-Command"]
    script = cmd[3]
    assert "System.Speech" in script
    assert "SpeechSynthesizer" in script
    assert "it''s your turn" in script  # PowerShell apostrophe escaping intact
    assert kwargs["creationflags"] == 0x08000000
    assert proc.wait_timeout == 30


# ---------------------------------------------------------------------------
# desktop.audio CLI fallback chain
# ---------------------------------------------------------------------------

@pytest.fixture
def cli_fallback_env(monkeypatch, tmp_path):
    """Force the CLI-player fallback branch of AudioPlayback.play_file."""
    wav = tmp_path / "sample.wav"
    wav.write_bytes(b"RIFFtest")
    monkeypatch.setattr(audio, "winsound", None)
    monkeypatch.setattr(audio, "sd", None)
    monkeypatch.setattr(audio, "sf", None)

    popen: list[tuple[list, dict]] = []

    def fake_popen(cmd, **kwargs):
        popen.append((cmd, kwargs))
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    return wav, popen


def test_audio_darwin_uses_afplay(monkeypatch, cli_fallback_env) -> None:
    wav, popen = cli_fallback_env
    monkeypatch.setattr(audio.sys, "platform", "darwin")

    which_queries: list[str] = []

    def fake_which(name):
        which_queries.append(name)
        return "/usr/bin/afplay" if name == "afplay" else None

    monkeypatch.setattr("shutil.which", fake_which)

    assert audio.AudioPlayback.play_file(str(wav)) is True
    assert which_queries == ["afplay"]  # linux players never probed on darwin
    assert len(popen) == 1
    cmd, _ = popen[0]
    assert cmd == ["afplay", str(wav.resolve())]


def test_audio_darwin_no_player_returns_false(monkeypatch, cli_fallback_env) -> None:
    wav, popen = cli_fallback_env
    monkeypatch.setattr(audio.sys, "platform", "darwin")
    monkeypatch.setattr("shutil.which", lambda name: None)

    assert audio.AudioPlayback.play_file(str(wav)) is False
    assert popen == []


def test_audio_linux_chain_untouched(monkeypatch, cli_fallback_env) -> None:
    wav, popen = cli_fallback_env
    monkeypatch.setattr(audio.sys, "platform", "linux")

    which_queries: list[str] = []

    def fake_which(name):
        which_queries.append(name)
        return "/usr/bin/paplay" if name == "paplay" else None

    monkeypatch.setattr("shutil.which", fake_which)

    assert audio.AudioPlayback.play_file(str(wav)) is True
    assert "afplay" not in which_queries
    assert which_queries[0] == "paplay"
    cmd, _ = popen[0]
    assert cmd == ["paplay", str(wav.resolve())]
