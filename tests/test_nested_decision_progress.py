"""Successful nested submissions reset stall time; background noise does not."""

import threading
from unittest.mock import Mock

import pytest
from tests.fakes import TARGET_POLL, TypedBridge, make_engine, planner_with, target_state

from arenamcp.standalone import StandaloneCoach


@pytest.mark.parametrize("success", [True, False])
def test_only_successful_submission_reports_progress(success):
    bridge = TypedBridge(TARGET_POLL)
    if not success:
        bridge.submit_targets = lambda ids: False
    engine = make_engine(bridge, planner_with('{"option_ids": ["tgt:2"]}'))
    engine._decision_progress_fn = Mock()
    engine._try_typed_decision_path(target_state(known_source=True), "decision_required")
    assert engine._decision_progress_fn.call_count == int(success)


def test_progress_counts_only_on_coaching_thread(monkeypatch):
    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach._loop_heartbeat = 10.0
    coach._coaching_thread = threading.current_thread()
    monkeypatch.setattr("arenamcp.standalone.time.monotonic", lambda: 20.0)
    coach._mark_coaching_progress()
    assert coach._loop_heartbeat == 20.0
    coach._coaching_thread = object()
    monkeypatch.setattr("arenamcp.standalone.time.monotonic", lambda: 30.0)
    coach._mark_coaching_progress()
    assert coach._loop_heartbeat == 20.0
