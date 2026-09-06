from __future__ import annotations

import threading

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
