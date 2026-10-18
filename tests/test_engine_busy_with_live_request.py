"""The 1.5-2.0 s game_engine_busy timer does not hold back a request the bridge already shows."""

from unittest.mock import MagicMock

from tests.fakes import make_engine

from arenamcp.autopilot_bridge import bridge_shows_pending


def _engine():
    return make_engine(MagicMock(connected=True), MagicMock(), dry_run=True)


BUSY = {"game_engine_busy": True, "turn": {"turn_number": 3}}


def test_bridge_shows_pending_needs_a_connected_bridge_with_a_request():
    assert not bridge_shows_pending(dict(BUSY))
    assert not bridge_shows_pending(dict(BUSY, _bridge_request_type="ActionsAvailable"))
    assert bridge_shows_pending(dict(BUSY, _bridge_connected=True, _bridge_request_type="ActionsAvailable"))
    assert bridge_shows_pending(dict(BUSY, _bridge_connected=True, _bridge_trigger={"has_pending": True}))
    assert not bridge_shows_pending(dict(BUSY, _bridge_connected=True, _bridge_trigger={"has_pending": False}))


def test_engine_busy_without_a_live_request_still_waits():
    engine = _engine()
    engine._detect_manual_play = MagicMock(side_effect=AssertionError("must not get this far"))
    assert engine.process_trigger(dict(BUSY), "decision_required") is True


def test_engine_busy_with_a_live_bridge_request_plans_anyway():
    engine = _engine()
    reached = []
    engine._detect_manual_play = MagicMock(side_effect=lambda state: reached.append(True) or True)
    state = dict(BUSY, _bridge_connected=True, _bridge_request_type="ActionsAvailable")
    assert engine.process_trigger(state, "decision_required") is False  # stood down by the stub, past the gate
    assert reached == [True]


def test_bridge_submission_is_not_skipped_while_the_bridge_shows_the_request():
    engine = _engine()
    bridge = MagicMock()
    bridge.connect.return_value = False  # stop right after the busy gate
    engine._progress_bridge = lambda *a, **k: bridge
    engine._try_gre_bridge(MagicMock(), dict(BUSY))
    bridge.connect.assert_not_called()
    engine._try_gre_bridge(MagicMock(), dict(BUSY, _bridge_connected=True, _bridge_request_class="X"))
    bridge.connect.assert_called_once()
