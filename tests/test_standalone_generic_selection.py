from __future__ import annotations

from arenamcp import standalone


class _DummyMcp:
    def __init__(self, next_state: dict) -> None:
        self._next_state = next_state
        self.polls = 0

    def poll_log(self) -> None:
        self.polls += 1

    def get_game_state(self) -> dict:
        return self._next_state


def _make_coach(next_state: dict) -> tuple[standalone.StandaloneCoach, list[str]]:
    coach = standalone.StandaloneCoach.__new__(standalone.StandaloneCoach)
    coach._mcp = _DummyMcp(next_state)
    coach._normalize_turn_snapshot = lambda state: state
    queued: list[str] = []
    coach._queue_screenshot_analysis = lambda: queued.append("queued")
    return coach, queued


def test_generic_selection_unresolved_queues_vision_and_stops_batch(monkeypatch) -> None:
    coach, queued = _make_coach(
        {
            "pending_decision": "Select Cards",
            "decision_context": {"type": "select_n"},
        }
    )
    monkeypatch.setattr(standalone.time, "sleep", lambda _seconds: None)

    refreshed, should_stop = coach._handle_unresolved_generic_selection(
        {
            "pending_decision": "Select Cards",
            "decision_context": {"type": "selection_generic"},
        }
    )

    assert refreshed["pending_decision"] == "Select Cards"
    assert should_stop is True
    assert queued == ["queued"]
    assert coach._mcp.polls == 1


def test_generic_selection_resolved_to_scry_skips_vision(monkeypatch) -> None:
    coach, queued = _make_coach(
        {
            "pending_decision": "Scry",
            "decision_context": {"type": "scry"},
        }
    )
    monkeypatch.setattr(standalone.time, "sleep", lambda _seconds: None)

    refreshed, should_stop = coach._handle_unresolved_generic_selection(
        {
            "pending_decision": "Select Cards",
            "decision_context": {"type": "selection_generic"},
        }
    )

    assert refreshed["decision_context"]["type"] == "scry"
    assert should_stop is False
    assert queued == []
    assert coach._mcp.polls == 1
