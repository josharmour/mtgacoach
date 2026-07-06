"""P1-4: an own stack object the bot didn't submit → advise-only cooldown.

2026-07-06 01:02: the user manually cast The Spirit Oasis and Utter
Insignificance; the autopilot kept planning against the user's board for
~50s and activated the user's aura 3x.
"""

import time

import arenamcp.autopilot as autopilot_module
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine


class _DummyBridge:
    connected = False

    def connect(self):
        return False


def _engine(monkeypatch) -> AutopilotEngine:
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: _DummyBridge())
    return AutopilotEngine(
        planner=None,
        mapper=None,
        controller=None,
        get_game_state=lambda: {},
        config=AutopilotConfig(dry_run=True),
    )


def _state(stack_names, local_seat=1):
    return {
        "local_seat_id": local_seat,
        "stack": [
            {"name": n, "controller_seat_id": local_seat} for n in stack_names
        ],
    }


def test_unexplained_own_stack_object_triggers_cooldown(monkeypatch):
    eng = _engine(monkeypatch)
    assert eng._detect_manual_play(_state([])) is False
    assert eng._detect_manual_play(_state(["The Spirit Oasis"])) is True
    assert time.time() < eng._manual_play_cooldown_until


def test_bot_submitted_cast_is_explained(monkeypatch):
    eng = _engine(monkeypatch)
    eng._detect_manual_play(_state([]))
    eng._recent_bot_submissions.append((time.monotonic(), "the spirit oasis"))
    assert eng._detect_manual_play(_state(["The Spirit Oasis"])) is False
    assert eng._manual_play_cooldown_until == 0.0


def test_opponent_stack_objects_ignored(monkeypatch):
    eng = _engine(monkeypatch)
    eng._detect_manual_play(_state([]))
    state = {
        "local_seat_id": 1,
        "stack": [{"name": "Counterspell", "controller_seat_id": 2}],
    }
    assert eng._detect_manual_play(state) is False


def test_stale_bot_submission_does_not_explain(monkeypatch):
    eng = _engine(monkeypatch)
    eng._detect_manual_play(_state([]))
    eng._recent_bot_submissions.append(
        (time.monotonic() - 60.0, "the spirit oasis")
    )
    assert eng._detect_manual_play(_state(["The Spirit Oasis"])) is True
