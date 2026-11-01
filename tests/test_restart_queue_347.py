"""#347: restart during match creation must not reserve gameplay for Recent Match."""

import time

import pytest
from tests.test_recent_auto_queue import setup, step

from arenamcp.standalone_auto_queue import _AutoQueueMixin

MATCH = "814296ac-3f26-41ff-9d95-08d7ec00736c"


@pytest.mark.parametrize("turn", [0, 1, 3])
def test_known_unfinished_match_cannot_start_idle_navigation(turn):
    nav, bridge, state, *_ = setup()
    state.update(match_id=MATCH, turn={"turn_number": turn}, last_game_result=None)
    assert not nav.start_recent_match(state)
    assert not nav.active
    bridge.get_recent_match.assert_not_called()


def idle_record():
    # Previous versions mislabeled a startup match ID as ended in idle records.
    return {
        "saved_at": time.time(),
        "recent_mode": True,
        "idle_recent": True,
        "ended_match_id": MATCH,
        "seen_ends": [MATCH],
        "stage": "home",
    }


def test_restart_at_room_creation_does_not_arm_recent_match():
    nav, bridge, state, *_ = setup()
    state.update(match_id=MATCH, turn={"turn_number": 0}, pending_decision=None)
    assert not _AutoQueueMixin()._arm_auto_queue_after_restart(nav, state)
    assert not nav.active
    bridge.get_recent_match.assert_not_called()


@pytest.mark.parametrize("progress", [{"pending_decision": "Mulligan"}, {"turn": {"turn_number": 3}}])
@pytest.mark.parametrize("worker", [False, True])
def test_idle_resume_hands_same_match_back_before_navigation(progress, worker):
    nav, bridge, state, backend, controller = setup()
    assert nav.restore_after_reload(idle_record())
    state.update(match_id=MATCH, **progress)
    if worker:
        step(nav)
    else:
        assert not nav.process_tick(state)
    assert not nav.active
    bridge.get_survey.assert_not_called()
    bridge.get_recent_match.assert_not_called()
    bridge.queue_recent_match.assert_not_called()
    controller.capture.assert_not_called()
    backend.complete_with_image.assert_not_called()
    # The idle anchor was never a completed match: its real end can still arm.
    assert nav.note_match_end({"match_id": MATCH}, confirmed_match_end=True)


def test_match_appearing_during_recent_read_prevents_queue_submission():
    nav, bridge, state, *_ = setup()
    assert nav.start_recent_match(state)
    target = bridge.get_recent_match.return_value

    def read_recent():
        state.update(match_id=MATCH, turn={"turn_number": 0})
        return target

    bridge.get_recent_match.side_effect = read_recent
    step(nav)
    assert not nav.active
    bridge.queue_recent_match.assert_not_called()


def test_idle_reload_does_not_mark_its_anchor_already_requeued():
    nav, _, state, *_ = setup()
    runtime = _AutoQueueMixin()
    state.update(match_id=MATCH, last_game_result="win")
    assert runtime._resume_auto_queue_after_reload(nav, idle_record(), state)
    assert getattr(runtime, "_auto_queue_last_noted", None) != MATCH
    assert MATCH not in nav._seen_ends
    assert "since match" not in nav._situation(time.monotonic())


def test_finished_snapshot_and_empty_home_still_support_recent_match():
    for state in ({}, {"match_id": MATCH, "last_game_result": "win", "turn": {"turn_number": 12}}):
        nav, bridge, current, _, controller = setup()
        current.update(state)
        assert nav.start_recent_match(current)
        step(nav)
        bridge.queue_recent_match.assert_called_once()
        controller.capture.assert_not_called()


def test_confirmed_result_navigation_keeps_same_terminal_turn_snapshot():
    nav, bridge, state, *_ = setup()
    state.update(match_id=MATCH, turn={"turn_number": 12})
    nav.note_match_end({"match_id": MATCH}, confirmed_match_end=True)
    # Authoritative completion, not idle inference, owns this unchanged ID.
    assert not nav._new_match(state)
    step(nav)
    bridge.return_to_home.assert_called_once()
