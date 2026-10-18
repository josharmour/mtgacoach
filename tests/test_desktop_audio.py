from __future__ import annotations

import threading

import pytest

from arenamcp.desktop import audio


def test_play_file_stops_then_plays_without_deadlock(tmp_path, monkeypatch) -> None:
    wav_path = tmp_path / "sample.wav"
    wav_path.write_bytes(b"RIFFtest")

    calls: list[tuple[object, int]] = []

    class FakeWinsound:
        SND_FILENAME = 0x00020000
        SND_ASYNC = 0x0001
        SND_NODEFAULT = 0x0002
        SND_PURGE = 0x0040

        @staticmethod
        def PlaySound(value, flags) -> None:
            calls.append((value, flags))

    monkeypatch.setattr(audio, "winsound", FakeWinsound)
    monkeypatch.setattr(audio.AudioPlayback, "_lock", threading.RLock())

    result: list[bool] = []
    worker = threading.Thread(
        target=lambda: result.append(audio.AudioPlayback.play_file(str(wav_path))),
        name="audio-play-file-test",
    )
    worker.start()
    worker.join(timeout=1.0)

    assert not worker.is_alive(), "play_file deadlocked while trying to stop current audio"
    assert result == [True]
    assert calls == [
        (None, FakeWinsound.SND_PURGE),
        (
            str(wav_path.resolve()),
            FakeWinsound.SND_FILENAME | FakeWinsound.SND_ASYNC | FakeWinsound.SND_NODEFAULT,
        ),
    ]


# ---------------------------------------------------------------------------
# CLI player fallback (no winsound / sounddevice)
# ---------------------------------------------------------------------------


class _FakeProc:
    def poll(self):
        return None


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
        return _FakeProc()

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
