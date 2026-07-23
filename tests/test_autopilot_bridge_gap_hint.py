"""Regression tests for the bridge-gap UX hint on MANUAL REQUIRED.

Before this fix, autopilot's user notification was just:
    "MANUAL REQUIRED: Bridge couldn't handle X — take this action manually."
With no signal whether the bridge was offline, had no pending request, or
had a pending request the codebase doesn't yet handle. The user had no
useful next step. The hint surfaces which of those it is.
"""

from __future__ import annotations

# We bypass module-level PIL imports (autopilot.py imports PIL.ImageGrab at
# top, which isn't installed in CI). Pull the helper off the class via a
# trick: import autopilot.AutopilotEngine and call the unbound method.
import importlib
import sys
from unittest.mock import MagicMock


def _engine_with_format_hint():
    """Construct just enough of AutopilotEngine to call _format_bridge_gap_hint.

    Avoid the full __init__ — that spins up bridge / config / notifier wiring
    we don't need. The hint method only reads game_state.
    """
    try:
        autopilot = importlib.import_module("arenamcp.autopilot")
    except ImportError as e:
        if "PIL" in str(e):
            # Stub PIL so the import succeeds in CI environments without it
            sys.modules["PIL"] = MagicMock()
            sys.modules["PIL.ImageGrab"] = MagicMock()
            autopilot = importlib.import_module("arenamcp.autopilot")
        else:
            raise

    engine = autopilot.AutopilotEngine.__new__(autopilot.AutopilotEngine)
    return engine


def test_hint_for_bridge_gap_request_type():
    engine = _engine_with_format_hint()
    state = {
        "_bridge_connected": True,
        "_bridge_request_type": "SelectTargets",
        "_bridge_request_class": "SelectTargetsRequest",
        "pending_decision": "Select Targets",
    }
    hint = engine._format_bridge_gap_hint(state)
    assert "Bridge gap" in hint
    assert "SelectTargets" in hint


def test_hint_includes_decision_context_type_when_present():
    engine = _engine_with_format_hint()
    state = {
        "_bridge_connected": True,
        "_bridge_request_type": "SelectN",
        "decision_context": {"type": "selection_generic"},
    }
    hint = engine._format_bridge_gap_hint(state)
    assert "Bridge gap: SelectN" in hint
    assert "selection_generic" in hint


def test_hint_for_bridge_offline():
    engine = _engine_with_format_hint()
    state = {"_bridge_connected": False}
    hint = engine._format_bridge_gap_hint(state)
    assert hint == "Bridge offline"


def test_hint_for_no_pending_request():
    engine = _engine_with_format_hint()
    state = {
        "_bridge_connected": True,
        "_bridge_request_type": None,
        "_bridge_request_class": None,
    }
    hint = engine._format_bridge_gap_hint(state)
    assert "No bridge request pending" in hint


def test_hint_for_no_pending_with_log_decision_fallback():
    engine = _engine_with_format_hint()
    state = {
        "_bridge_connected": True,
        "_bridge_request_type": None,
        "pending_decision": "Mulligan",
    }
    hint = engine._format_bridge_gap_hint(state)
    assert "No bridge request pending" in hint
    assert "Mulligan" in hint


def test_hint_empty_when_no_game_state():
    engine = _engine_with_format_hint()
    assert engine._format_bridge_gap_hint(None) == ""
    # An empty-but-not-None dict is treated the same — there's nothing to
    # report. Callers expecting a hint should pass real snapshot data.
    assert engine._format_bridge_gap_hint({}) == ""
