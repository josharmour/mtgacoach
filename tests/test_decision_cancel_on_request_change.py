"""A decision's model call stops when the bridge shows a different request.

2026-10-10: 95 decision calls ran to completion and were then discarded with
"typed-decision: request changed during planning", each holding a model-server
slot the next decision needed.

2026-10-18 18:18:21-26: the strategic path (plan_actions) had no such cancel; a
damage plan outlived its AssignDamage request and was submitted against the
commander's OptionalAction (tests.commander_return_states).
"""

import threading
from unittest.mock import MagicMock

import pytest
from tests.commander_return_states import (
    ASSIGN_DAMAGE_POLL,
    COMMANDER_RETURN_POLL,
    DAMAGE_PLAN,
    assign_damage_state,
    commander_return_state,
)
from tests.fakes import make_engine
from tests.jace_states import POLL, jace_engine, jace_state

from arenamcp.action_planner import ActionPlanner
from arenamcp.autopilot import AutopilotState
from arenamcp.backends.proxy import BackendCallCancelled

ANSWER = '{"option_ids": ["idx:2"], "reasoning": "Jace -3 draws."}'


class _WatchingBackend:
    """Delivers a bridge update to the autopilot's request listener mid-call."""

    def __init__(self, listeners: list, update: dict | None):
        self.listeners = listeners
        self.update = update
        self.cancel_events: list = []

    def complete(self, system, user, *args, cancel_event: threading.Event | None = None, **kwargs):
        self.cancel_events.append(cancel_event)
        if self.update is not None and "PENDING DECISION" in user:
            for listener in list(self.listeners):
                listener(self.update)
        return ANSWER


def _engine_with_watch(update: dict | None):
    listeners: list = []
    engine, bridge, _ = jace_engine(ANSWER)
    backend = _WatchingBackend(listeners, update)
    engine._planner._backend = backend

    def watch(listener):
        listeners.append(listener)
        return lambda: listeners.remove(listener)

    engine._request_watch_fn = watch
    return engine, bridge, backend, listeners


def test_a_new_request_mid_call_cancels_it_and_nothing_is_submitted():
    newer = dict(POLL, msg_id=700, game_state_id=494)
    engine, bridge, backend, listeners = _engine_with_watch(newer)
    assert engine._try_typed_decision_path(jace_state(), "decision_required") is True
    events = [e for e in backend.cancel_events if e is not None]
    assert events and all(e.is_set() for e in events)
    bridge.submit_action_by_index.assert_not_called()
    assert listeners == []  # unsubscribed after planning
    assert getattr(engine._planner, "_decision_cancel_event", None) is None


def test_the_same_request_or_an_empty_poll_does_not_cancel():
    engine, bridge, backend, _ = _engine_with_watch({"ok": True, "has_pending": False})
    assert engine._try_typed_decision_path(jace_state(), "decision_required") is True
    bridge.submit_action_by_index.assert_called_once()
    assert not any(e is not None and e.is_set() for e in backend.cancel_events)

    engine, bridge, backend, _ = _engine_with_watch(dict(POLL))
    assert engine._try_typed_decision_path(jace_state(), "decision_required") is True
    bridge.submit_action_by_index.assert_called_once()


def test_only_decision_calls_carry_the_cancel_event():
    backend = MagicMock()
    backend.complete.return_value = "ok"
    planner = ActionPlanner(backend=backend)
    event = threading.Event()
    planner.set_decision_cancel_event(event)
    planner._call_llm("s", "u", 10, timeout_s=1.0, call_class="decision.typed")
    assert backend.complete.call_args.kwargs["cancel_event"] is event
    planner._call_llm("s", "u", 10, timeout_s=1.0, call_class="background.game_plan")
    assert "cancel_event" not in backend.complete.call_args.kwargs
    planner.set_decision_cancel_event(None)
    planner._call_llm("s", "u", 10, timeout_s=1.0, call_class="decision.typed")
    assert "cancel_event" not in backend.complete.call_args.kwargs


def test_a_cancelled_call_raising_from_the_proxy_still_yields_without_submitting():
    newer = dict(POLL, msg_id=700, game_state_id=494)
    engine, bridge, backend, _ = _engine_with_watch(newer)
    original = backend.complete

    def complete(system, user, *args, **kwargs):
        original(system, user, *args, **kwargs)
        if kwargs.get("cancel_event") is not None and kwargs["cancel_event"].is_set():
            raise BackendCallCancelled("request cancelled")
        return ANSWER

    backend.complete = complete
    assert engine._try_typed_decision_path(jace_state(), "decision_required") is True
    bridge.submit_action_by_index.assert_not_called()
    assert engine._planner._memory().recent_actions_text() == ""


# --- the strategic path: plan_actions (2026-10-18 18:18:20-26) ---------------


class _SwitchingBackend:
    """Answers ``answers`` in turn; the first call moves the bridge to ``switch_to`` mid-call.

    ``honours_cancel``: raise like the proxy when the cancel event is set
    (False: a backend that answers anyway).
    """

    def __init__(self, bridge, listeners: list, switch_to: dict | None, answers: list, honours_cancel=True):
        self.bridge = bridge
        self.listeners = listeners
        self.switch_to = switch_to
        self.answers = list(answers)
        self.honours_cancel = honours_cancel
        self.cancel_events: list = []

    def complete(self, system, user, *args, cancel_event: threading.Event | None = None, **kwargs):
        self.cancel_events.append(cancel_event)
        answer = self.answers.pop(0)
        if self.switch_to is not None:
            self.bridge.get_pending_actions.return_value = self.switch_to
            for listener in list(self.listeners):
                listener(self.switch_to)
            self.switch_to = None
        if self.honours_cancel and cancel_event is not None and cancel_event.is_set():
            raise BackendCallCancelled("request cancelled")
        return answer


def _damage_engine(switch_to, answers, honours_cancel=True):
    bridge = MagicMock()
    bridge.connected = True
    bridge.get_pending_actions.return_value = ASSIGN_DAMAGE_POLL
    bridge.submit_assign_damage.return_value = True
    bridge.submit_optional.return_value = True
    listeners: list = []
    backend = _SwitchingBackend(bridge, listeners, switch_to, answers, honours_cancel)

    def log_state():  # the log snapshot follows the request Arena shows
        if bridge.get_pending_actions.return_value is COMMANDER_RETURN_POLL:
            return commander_return_state()
        return assign_damage_state()

    engine = make_engine(bridge, ActionPlanner(backend=backend), game_state=log_state)
    engine._game_plan_mgr = None  # only the decision calls answer here

    def watch(listener):
        listeners.append(listener)
        return lambda: listeners.remove(listener)

    engine._request_watch_fn = watch
    return engine, bridge, backend, listeners


@pytest.mark.parametrize("honours_cancel", [True, False], ids=["proxy-raises", "backend-answers-anyway"])
def test_a_damage_plan_whose_request_is_replaced_mid_call_is_cancelled_not_submitted(honours_cancel):
    engine, bridge, backend, listeners = _damage_engine(COMMANDER_RETURN_POLL, [DAMAGE_PLAN], honours_cancel)

    assert engine.process_trigger(assign_damage_state(), "stack_spell_yours") is True

    events = [e for e in backend.cancel_events if e is not None]
    assert events and all(e.is_set() for e in events)
    bridge.submit_assign_damage.assert_not_called()
    bridge.submit_optional.assert_not_called()
    assert engine._state == AutopilotState.IDLE  # yielded, not PAUSED for MANUAL REQUIRED
    assert not engine.is_window_given_up(assign_damage_state())
    assert listeners == []
    assert getattr(engine._planner, "_decision_cancel_event", None) is None


def test_the_next_cycle_answers_the_commander_return():
    accept = '{"option_ids": ["optional:accept"], "reasoning": "Keep Michelangelo available."}'
    engine, bridge, backend, _ = _damage_engine(COMMANDER_RETURN_POLL, [DAMAGE_PLAN, accept])
    assert engine.process_trigger(assign_damage_state(), "stack_spell_yours") is True

    assert engine.process_trigger(commander_return_state(), "decision_required") is True

    bridge.submit_optional.assert_called_once_with(True, expected_request_id=(339, 492))
    bridge.submit_assign_damage.assert_not_called()


def test_the_same_damage_request_throughout_is_still_submitted():
    engine, bridge, backend, _ = _damage_engine(None, [DAMAGE_PLAN])

    def submitted(assigners):
        bridge.get_pending_actions.return_value = {"ok": True, "has_pending": False}
        return True

    bridge.submit_assign_damage.side_effect = submitted
    assert engine.process_trigger(assign_damage_state(), "stack_spell_yours") is True
    bridge.submit_assign_damage.assert_called_once()
    assert not any(e is not None and e.is_set() for e in backend.cancel_events)
