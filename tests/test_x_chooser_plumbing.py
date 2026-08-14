"""Regression tests for issue #390 — the two residual Python gaps left after
PR #427 delivered the C# side of the casting-time X chooser.

  1. `autopilot_bridge` submitted X via `submit_numeric`, but an X cost arrives
     as a CastingTimeOption_NumericInputRequest *child* of a
     CastingTimeOptionRequest. The plugin's HandleSubmitNumeric only matches a
     standalone NumericInputRequest, so the parent-type mismatch failed and
     every X cast fell through to MANUAL REQUIRED.
  2. The numeric bounds the plugin reports were never stamped onto the
     snapshot, so the planner — told to pick a value "within shown min/max" —
     was shown no bounds at all.
"""

from arenamcp.coach_prompt_utils import _format_numeric_constraints
from arenamcp.gre_bridge import _stamp_bridge_fields


def _normalized(has_pending=True):
    return {
        "has_pending": has_pending,
        "request_type": "CastingTimeOptions",
        "request_class": "CastingTimeOptionRequest",
        "actions": [],
        "request_payload": None,
    }


def _poll(**extra):
    poll = {
        "has_pending": True,
        "request_type": "CastingTimeOptions",
        "request_class": "CastingTimeOptionRequest",
        "can_pass": False,
    }
    poll.update(extra)
    return poll


# --- gap 2: numeric constraints reach the snapshot ---------------------------


def test_numeric_bounds_are_stamped_onto_snapshot():
    snapshot: dict = {}
    _stamp_bridge_fields(
        snapshot,
        _poll(numeric_min=0, numeric_max=7, numeric_step=1, numeric_suggested=[3, 7]),
        _normalized(),
    )
    assert snapshot["_bridge_numeric_min"] == 0
    assert snapshot["_bridge_numeric_max"] == 7
    assert snapshot["_bridge_numeric_step"] == 1
    assert snapshot["_bridge_numeric_suggested"] == [3, 7]


def test_numeric_bounds_cleared_when_nothing_pending():
    snapshot: dict = {}
    _stamp_bridge_fields(
        snapshot,
        _poll(numeric_min=0, numeric_max=7),
        _normalized(has_pending=False),
    )
    assert snapshot["_bridge_numeric_min"] is None
    assert snapshot["_bridge_numeric_max"] is None


def test_absent_numeric_fields_stamp_as_none():
    """A non-X request must not leave stale bounds behind."""
    snapshot: dict = {"_bridge_numeric_max": 99}
    _stamp_bridge_fields(snapshot, _poll(), _normalized())
    assert snapshot["_bridge_numeric_max"] is None


# --- gap 2: the bounds are actually rendered into the prompt ----------------


def test_x_range_line_rendered():
    line = _format_numeric_constraints(
        {
            "_bridge_numeric_min": 0,
            "_bridge_numeric_max": 7,
            "_bridge_numeric_step": 2,
            "_bridge_numeric_suggested": [4],
            "_bridge_numeric_disallowed": [1],
        }
    )
    assert line == "X_RANGE: min=0, max=7, step=2, suggested=4, disallowed=1"


def test_x_range_parity_flags():
    assert "odd values only" in _format_numeric_constraints(
        {"_bridge_numeric_min": 1, "_bridge_numeric_max": 5, "_bridge_numeric_disallow_even": True}
    )
    assert "even values only" in _format_numeric_constraints(
        {"_bridge_numeric_min": 0, "_bridge_numeric_max": 6, "_bridge_numeric_disallow_odd": True}
    )


def test_no_x_range_line_without_bounds():
    assert _format_numeric_constraints({}) == ""
    assert _format_numeric_constraints({"_bridge_numeric_step": 1}) == ""


def test_x_range_reaches_the_planner_prompt():
    """The planner drops heavy GRE dumps but must still see the bounds."""
    from arenamcp.coach_prompt_utils import _build_bridge_context_lines

    state = {"_bridge_request_type": "CastingTimeOptions", "_bridge_numeric_min": 0, "_bridge_numeric_max": 7}
    lines = _build_bridge_context_lines(state, [], for_planner=True)
    assert any(line.startswith("X_RANGE:") for line in lines), lines


# --- gap 1: submit_x is preferred over submit_numeric -----------------------


class _FakeBridge:
    def __init__(self, x_ok=True, numeric_ok=True):
        self.x_ok = x_ok
        self.numeric_ok = numeric_ok
        self.calls: list[tuple[str, int]] = []

    def connect(self):
        return True

    def submit_x(self, value):
        self.calls.append(("submit_x", value))
        return self.x_ok

    def submit_numeric(self, value):
        self.calls.append(("submit_numeric", value))
        return self.numeric_ok


def _run_numeric_input(bridge, value=5):
    """Drive just the NUMERIC_INPUT branch of the bridge submit path."""
    from arenamcp.action_planner import ActionType, GameAction
    from arenamcp.autopilot_bridge import _BridgeSubmitMixin

    executor = object.__new__(_BridgeSubmitMixin)
    executor._gre_bridge = bridge
    executor._gre_bridge_failed_methods = set()
    executor._log_execution_path = lambda *a, **k: None
    action = GameAction(action_type=ActionType.NUMERIC_INPUT, numeric_value=value)
    return executor._try_gre_bridge(action, {})


def test_submit_x_is_used_for_x_costs():
    bridge = _FakeBridge(x_ok=True)
    result = _run_numeric_input(bridge)
    assert result is not None and result.success
    assert bridge.calls == [("submit_x", 5)]


def test_falls_back_to_submit_numeric_on_older_plugins():
    bridge = _FakeBridge(x_ok=False, numeric_ok=True)
    result = _run_numeric_input(bridge)
    assert result is not None and result.success
    assert bridge.calls == [("submit_x", 5), ("submit_numeric", 5)]


def test_manual_required_when_both_fail():
    bridge = _FakeBridge(x_ok=False, numeric_ok=False)
    assert _run_numeric_input(bridge) is None
