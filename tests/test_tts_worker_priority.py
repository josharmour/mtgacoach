"""The Kokoro worker runs below the game (2026-10-18 22:40, Asahi Linux).

Each spoken line's ONNX burst took the performance cores Arena's VM runs on and
the game stuttered; the worker now lowers its own priority before loading the
model, and synthesis uses two threads instead of four.
"""

from __future__ import annotations

import sys

from arenamcp.desktop import tts_worker


def test_worker_lowers_its_priority_on_posix(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("os.nice", calls.append)
    tts_worker._lower_priority()
    assert calls == [10]


def test_priority_failure_never_stops_the_worker(monkeypatch):
    def refuse(_increment):
        raise PermissionError("not allowed")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("os.nice", refuse)
    tts_worker._lower_priority()  # no exception
