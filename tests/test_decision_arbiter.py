"""Phase A of fable-improvements.md: bridge-authoritative decision arbiter
plus the idempotent autopilot control plane.

Live 2026-06-09: log-derived ghost decisions ('Select Targets' pending in
the snapshot while the connected bridge reported nothing) drove endless
replanning + repeated TTS; and a stateful toggle_autopilot raced a UI
click, flipping autopilot off mid-match.
"""

from arenamcp.decision_arbiter import ArbitratedDecision, arbitrate
from arenamcp.standalone import StandaloneCoach


# ---------------------------------------------------------------------------
# arbitrate()
# ---------------------------------------------------------------------------


def test_connected_idle_bridge_means_no_decision():
    state = {
        "_bridge_connected": True,
        "pending_decision": "Select Targets",          # stale log state
        "legal_actions": ["Select target for X"],
        "decision_context": {"type": "target_selection"},
    }
    assert arbitrate(state) is None


def test_connected_bridge_with_pending_wins():
    state = {
        "_bridge_connected": True,
        "_bridge_request_type": "SelectTargets",
        "pending_decision": "Select Targets",
    }
    arb = arbitrate(state)
    assert arb is not None
    assert arb.source == "bridge"
    assert arb.request_type == "SelectTargets"


def test_bridge_trigger_pending_counts():
    state = {
        "_bridge_connected": True,
        "_bridge_trigger": {"has_pending": True},
        "pending_decision": "Mulligan",
    }
    arb = arbitrate(state)
    assert arb is not None and arb.source == "bridge"


def test_disconnected_falls_back_to_log():
    state = {
        "_bridge_connected": False,
        "pending_decision": "Select Targets",
        "decision_context": {"type": "target_selection"},
    }
    arb = arbitrate(state)
    assert arb is not None
    assert arb.source == "log"
    assert arb.decision_type == "target_selection"


def test_disconnected_with_nothing_is_none():
    assert arbitrate({"_bridge_connected": False}) is None


def test_explicit_connectivity_override_beats_snapshot():
    # Snapshot says disconnected, but the caller holds the live bridge.
    state = {"pending_decision": "Select Targets"}
    assert arbitrate(state, bridge_connected=True) is None
    assert arbitrate(state, bridge_connected=False) is not None


# ---------------------------------------------------------------------------
# set_autopilot (idempotent control plane)
# ---------------------------------------------------------------------------


def _bare_coach(enabled: bool, initialized: bool) -> StandaloneCoach:
    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach._autopilot_enabled = enabled
    coach._autopilot = object() if initialized else None
    return coach


def test_set_autopilot_noop_when_already_in_state(monkeypatch):
    coach = _bare_coach(enabled=True, initialized=True)
    calls = []
    coach.toggle_autopilot = lambda: calls.append(1) or True  # type: ignore[method-assign]
    assert coach.set_autopilot(True) is True
    assert calls == []  # no toggle fired — idempotent

    coach_off = _bare_coach(enabled=False, initialized=False)
    coach_off.toggle_autopilot = lambda: calls.append(1) or True  # type: ignore[method-assign]
    assert coach_off.set_autopilot(False) is False
    assert calls == []


def test_set_autopilot_toggles_when_state_differs():
    coach = _bare_coach(enabled=False, initialized=False)
    calls = []

    def fake_toggle():
        calls.append(1)
        return True

    coach.toggle_autopilot = fake_toggle  # type: ignore[method-assign]
    assert coach.set_autopilot(True) is True
    assert len(calls) == 1
