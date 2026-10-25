"""Bug #206: the priority request after our own shouldStop cast was never surfaced.

Match 3 on v12, 17:30:23: the typed-decision path cast Leonardo, Big Brother
(idx 0, abilityGrpId 115, shouldStop: True). Arena accepted it and held
priority for us, but the bridge only reported "decision cleared (was: None)"
and never surfaced the follow-up priority request. No autopilot or coach line
appeared for 45 s, Arena's TimerType_NonActivePlayer rope expired
(TimerMessage 17:31:10), and Arena auto-passed.

Two halves are covered here:

1. After a submitted ActionsAvailable option whose raw action carries
   ``shouldStop``, the autopilot polls the bridge itself until a new request
   appears (and answers it) or the window closes — bounded.
2. When the bridge reports nothing but Player.log says the decision is ours
   (``turn.decision_player`` == our seat) and our timer is ticking, the
   engine synthesizes a stand-in request so the decision watchdog can act
   (re-plan, then a loud MANUAL REQUIRED) instead of standing down.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from tests.fakes import make_engine

from arenamcp.decision_watchdog import request_key
from arenamcp.standalone import StandaloneCoach


def _cast_poll(state_id=1, msg_id=100, should_stop=True, instance_id=10):
    return {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "game_state_id": state_id,
        "msg_id": msg_id,
        "can_pass": True,
        "can_cancel": True,
        "actions": [
            {
                "actionType": "Cast",
                "instanceId": instance_id,
                "grpId": 115,
                "abilityGrpId": 115,
                "hasAutoTap": True,
                **({"shouldStop": True} if should_stop else {}),
            }
        ],
    }


def _pass_poll(state_id=2, msg_id=200):
    return {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "game_state_id": state_id,
        "msg_id": msg_id,
        "can_pass": True,
        "actions": [{"actionType": "Pass"}],
    }


def _engine(polls, planner_choices):
    """An engine on a scripted bridge; ``polls`` are handed out in order."""
    planner = MagicMock()
    planner.plan_decision_options.side_effect = planner_choices
    engine = make_engine(planner=planner)
    bridge = MagicMock()
    bridge.connected = True
    queue = iter(polls)
    bridge.get_pending_actions.side_effect = lambda: next(
        queue, polls[-1] if polls else {"has_pending": False}
    )
    engine._gre_bridge = bridge
    # Keep the post-cast poll tight so tests stay fast.
    engine._POST_CAST_PRIORITY_POLL_INTERVAL_S = 0.001
    return engine


# --- Half 1: poll for the priority Arena hands back after a shouldStop play ----


def test_should_stop_cast_surfaces_and_answers_followup_request():
    engine = _engine(
        [_cast_poll(), _cast_poll(), _pass_poll()],
        [["idx:0"], ["pass"]],
    )
    assert engine._try_typed_decision_path({}, "decision_required") is True
    engine._gre_bridge.submit_action_by_index.assert_called_once()
    engine._gre_bridge.submit_pass.assert_called_once()
    assert engine._actions_executed == 2


def test_should_stop_cast_window_closing_stops_the_poll():
    engine = _engine(
        [_cast_poll(), _cast_poll(), {"has_pending": False}],
        [["idx:0"]],
    )
    assert engine._try_typed_decision_path({}, "decision_required") is True
    engine._gre_bridge.submit_action_by_index.assert_called_once()
    engine._gre_bridge.submit_pass.assert_not_called()
    assert engine._actions_executed == 1


def test_cast_without_should_stop_does_not_poll_after_submit():
    engine = _engine([_cast_poll(should_stop=False), _cast_poll(should_stop=False)], [["idx:0"]])
    assert engine._try_typed_decision_path({}, "decision_required") is True
    # Exactly the initial poll plus the pre-submit revalidation.
    assert engine._gre_bridge.get_pending_actions.call_count == 2
    engine._gre_bridge.submit_action_by_index.assert_called_once()


def test_post_cast_chain_is_bounded():
    """The hop limit stops the shouldStop chain; deeper requests go to the watchdog."""
    engine = _engine([_cast_poll()], [["idx:0"]])
    decision = MagicMock()
    decision.request_id = (1, 100)
    calls = []
    engine._try_typed_decision_path = lambda *a, **k: calls.append(a) or True

    engine._surface_post_cast_priority(
        decision,
        ["idx:0"],
        _cast_poll(),
        {},
        "decision_required",
        depth=engine._MAX_POST_CAST_PRIORITY_HOPS,
    )
    # depth + 1 would exceed the limit: surfaced, but not re-entered.
    assert calls == []


def test_post_cast_priority_recurses_on_a_new_request_only():
    engine = _engine([], [])
    engine._POST_CAST_PRIORITY_POLL_S = 0.05
    engine._POST_CAST_PRIORITY_POLL_INTERVAL_S = 0.01
    decision = MagicMock()
    decision.request_id = (1, 100)
    from arenamcp.decisions import build_pending_decision

    answered = build_pending_decision(_cast_poll(), resolve_instance=lambda _iid: "")
    calls = []
    engine._try_typed_decision_path = lambda *a, **k: calls.append((a, k)) or True
    engine._gre_bridge.get_pending_actions = lambda: _cast_poll()
    engine._surface_post_cast_priority(answered, ["idx:0"], _cast_poll(), {}, "decision_required")
    assert calls == []

    # A new request (different game state id): one recursion, then stop.
    engine._gre_bridge.get_pending_actions = lambda: _pass_poll()
    engine._surface_post_cast_priority(answered, ["idx:0"], _cast_poll(), {}, "decision_required")
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[1] == "decision_required"
    assert kwargs.get("depth") == 1
    assert args[0].get("_bridge_request_type") == "ActionsAvailable"


def test_post_cast_priority_gives_up_within_bounds():
    engine = _engine([], [])
    decision = MagicMock()
    decision.request_id = (1, 100)
    from arenamcp.decisions import build_pending_decision

    answered = build_pending_decision(_cast_poll(), resolve_instance=lambda _iid: "")
    engine._POST_CAST_PRIORITY_POLL_S = 0.05
    engine._POST_CAST_PRIORITY_POLL_INTERVAL_S = 0.01
    engine._try_typed_decision_path = lambda *a, **k: (_ for _ in ()).throw(AssertionError("recursed"))
    # Bridge never shows anything: the poll gives up (the watchdog backs it up).
    engine._gre_bridge.get_pending_actions = lambda: {"has_pending": False}
    engine._surface_post_cast_priority(answered, ["idx:0"], _cast_poll(), {}, "decision_required")


# --- Half 2: the watchdog keys off Player.log when the bridge is blind ---------


def _blind_state(**overrides):
    state = {
        "local_seat_id": 2,
        "turn": {
            "turn_number": 7,
            "phase": "Phase_Upkeep",
            "step": "",
            "active_player": 1,
            "priority_player": 2,
            "decision_player": 2,
        },
        "players": [{"seat_id": 2, "is_local": True}, {"seat_id": 1}],
        # Leonardo sits on the stack: Arena accepted our cast and owes us a
        # priority window the bridge never reported.
        "stack": [{"instance_id": 900, "name": "Leonardo, Big Brother"}],
        "timer_state": {2: {"is_ticking": True, "time_remaining_ms": 40_000, "received_at": time.time()}},
    }
    state.update(overrides)
    return state


def _blind_bridge():
    class BlindBridge:
        connected = True
        last_submission_at = 0.0

        def connect(self):
            return True

        def get_pending_actions(self):
            return {"has_pending": False}

    return BlindBridge()


def test_log_priority_request_synthesized_while_bridge_blind():
    engine = make_engine(bridge=_blind_bridge())
    resp = engine.log_priority_request(_blind_state())
    assert resp is not None
    assert resp["has_pending"] is True
    assert resp["request_type"] == "ActionsAvailable"
    assert resp["_log_derived"] is True
    # Stable identity while the window lasts, so the watchdog accumulates age.
    assert request_key(resp) == request_key(engine.log_priority_request(_blind_state()))


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "turn": {
                "turn_number": 7,
                "phase": "Phase_Upkeep",
                "active_player": 1,
                "priority_player": 1,
                "decision_player": 1,
            }
        },
        {"timer_state": {2: {"is_ticking": False, "time_remaining_ms": 40_000}}},
        {"stack": [], "pending_decision": None},
        {"players": [], "local_seat_id": None},
    ],
)
def test_log_priority_request_requires_our_decision_ticking_timer_and_a_window(overrides):
    engine = make_engine(bridge=_blind_bridge())
    assert engine.log_priority_request(_blind_state(**overrides)) is None


def test_log_priority_request_needs_a_connected_bridge():
    engine = make_engine()  # default FakeBridge is disconnected

    class OffBridge:
        connected = False

        def connect(self):
            return False

    engine._gre_bridge = OffBridge()
    assert engine.log_priority_request(_blind_state()) is None


def test_watchdog_alerts_on_log_derived_request_the_bridge_cannot_see():
    coach = StandaloneCoach.__new__(StandaloneCoach)
    now = [1000.0]
    coach._decision_watchdog = __import__(
        "arenamcp.decision_watchdog", fromlist=["DecisionWatchdog"]
    ).DecisionWatchdog(monotonic=lambda: now[0])
    engine = make_engine(bridge=_blind_bridge())
    coach._autopilot = engine
    coach._autopilot_enabled = True

    class BlindPoller:
        connected = True

        def __init__(self, bridge):
            self._bridge = bridge

        def latest_pending(self):
            return None

    coach._bridge_poller = BlindPoller(engine._gre_bridge)
    advice: list[str] = []
    coach.ui = SimpleNamespace(advice=lambda text, label: advice.append(text), log=lambda *_: None)
    coach.speak_advice = lambda *_, **__: None

    state = _blind_state()
    # First sight registers the request; nothing escalates yet.
    now[0] = 1000.0
    assert coach._tick_decision_watchdog(state) is None
    # Past REPLAN_AFTER_S: the watchdog forces a re-plan first.
    now[0] = 1013.0
    assert coach._tick_decision_watchdog(state) == "replan"
    # Past SAFE_DEFAULT_AFTER_S with the bridge still blind: no safe default
    # can ever submit, so the watchdog must fall through to the loud alert
    # instead of deferring until Arena's rope expires (#206).
    now[0] = 1035.0
    assert coach._tick_decision_watchdog(state) == "alerted"
    assert advice and advice[0].startswith("MANUAL REQUIRED")


def test_post_cast_yields_immediately_to_declaration_family():
    from arenamcp.decisions import build_pending_decision

    engine = _engine([], [])
    engine._gre_bridge.get_pending_actions = MagicMock(
        return_value={
            "has_pending": True,
            "request_type": "DeclareAttackers",
            "msg_id": 201,
        }
    )
    answered = build_pending_decision(_cast_poll(), resolve_instance=lambda _iid: "")
    engine._surface_post_cast_priority(answered, ["idx:0"], _cast_poll(), {}, "decision_required")
    assert engine._gre_bridge.get_pending_actions.call_count == 1
