"""The Decks tab's analyze-all is single-flight and never starts a deck during a match.

2026-10-10: the 95-deck run was started twice and analysed mid-match at high
effort, beside the live decision calls (which then waited ~2x as long for
their first token).
"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from arenamcp import standalone_decks
from arenamcp.standalone_decks import _DeckLibraryMixin


class _Library(_DeckLibraryMixin):
    def __init__(self, records, in_match):
        self._deck_library = {r.deck_id: r for r in records}
        self.in_match = in_match
        self.ui = Mock()
        self.analysed: list[str] = []
        self.release = threading.Event()
        self.release.set()

    def _draft_event_in_match(self):
        return self.in_match()

    def deck_strategy_status(self, record):
        return {"status": "ready" if record.deck_id in self.analysed else "none"}

    def deck_payloads(self):
        return []

    def analyze_deck_record(self, record, *, refresh=False):
        assert self.release.wait(2)
        self.analysed.append(record.deck_id)
        return {"deck_id": record.deck_id, "status": "ready"}


def _records(*ids):
    return [SimpleNamespace(deck_id=i, name=i, complete=True) for i in ids]


def _wait_for(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _finish(lib, in_match=None):
    """Let any run end, so a failed assert never leaves the process-wide lock held."""
    if in_match is not None:
        in_match[0] = False
    lib.release.set()
    assert _wait_for(lambda: not standalone_decks._ANALYZE_ALL_LOCK.locked())


def test_a_second_analyze_all_while_one_runs_is_ignored():
    lib = _Library(_records("a", "b"), in_match=lambda: False)
    lib.release.clear()
    try:
        assert lib.analyze_all_decks_async() is True
        assert lib.analyze_all_decks_async() is False
        lib.release.set()
        assert _wait_for(lambda: lib.analysed == ["a", "b"])
        assert _wait_for(lambda: not standalone_decks._ANALYZE_ALL_LOCK.locked())
        assert lib.analyze_all_decks_async() is True  # free again once the run ended
    finally:
        _finish(lib)


def test_no_deck_starts_during_a_match(monkeypatch):
    monkeypatch.setattr(_Library, "_ANALYZE_ALL_MATCH_POLL_S", 0.001)
    in_match = [True]
    polls = []
    paused = threading.Event()

    def match_running():
        polls.append(in_match[0])
        if len(polls) >= 3:
            paused.set()  # the run has checked the match several times while it waits
        return in_match[0]

    lib = _Library(_records("a", "b"), in_match=match_running)
    try:
        assert lib.analyze_all_decks_async() is True
        assert paused.wait(2)
        assert lib.analysed == []
        in_match[0] = False
        assert _wait_for(lambda: lib.analysed == ["a", "b"])
    finally:
        _finish(lib, in_match)
