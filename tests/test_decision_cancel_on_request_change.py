"""A typed decision's model call stops when the bridge shows a different request.

2026-10-10: 95 decision calls ran to completion and were then discarded with
"typed-decision: request changed during planning", each holding a model-server
slot the next decision needed.
"""

import threading
from unittest.mock import MagicMock

from test_ability_choice_matching import POLL, _typed_engine, _typed_state

from arenamcp.action_planner import ActionPlanner

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
    engine, bridge, _ = _typed_engine(ANSWER)
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
    assert engine._try_typed_decision_path(_typed_state(), "decision_required") is True
    events = [e for e in backend.cancel_events if e is not None]
    assert events and all(e.is_set() for e in events)
    bridge.submit_action_by_index.assert_not_called()
    assert listeners == []  # unsubscribed after planning
    assert getattr(engine._planner, "_decision_cancel_event", None) is None


def test_the_same_request_or_an_empty_poll_does_not_cancel():
    engine, bridge, backend, _ = _engine_with_watch({"ok": True, "has_pending": False})
    assert engine._try_typed_decision_path(_typed_state(), "decision_required") is True
    bridge.submit_action_by_index.assert_called_once()
    assert not any(e is not None and e.is_set() for e in backend.cancel_events)

    engine, bridge, backend, _ = _engine_with_watch(dict(POLL))
    assert engine._try_typed_decision_path(_typed_state(), "decision_required") is True
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
    from arenamcp.backends.proxy import BackendCallCancelled

    newer = dict(POLL, msg_id=700, game_state_id=494)
    engine, bridge, backend, _ = _engine_with_watch(newer)
    original = backend.complete

    def complete(system, user, *args, **kwargs):
        original(system, user, *args, **kwargs)
        if kwargs.get("cancel_event") is not None and kwargs["cancel_event"].is_set():
            raise BackendCallCancelled("request cancelled")
        return ANSWER

    backend.complete = complete
    assert engine._try_typed_decision_path(_typed_state(), "decision_required") is True
    bridge.submit_action_by_index.assert_not_called()
    assert engine._planner._memory().recent_actions_text() == ""
