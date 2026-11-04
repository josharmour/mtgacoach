"""Default-off DeepSeek tactical experiment; one bounded thinker per process."""

from __future__ import annotations

import concurrent.futures
import os
import threading
import time

_THINKER = threading.BoundedSemaphore(1)
POLICY = "deepseek-effort1-cap256-v1"


def enabled(model):
    return (
        os.environ.get("ARENAMCP_SELECTIVE_THINKING", "").lower() in {"1", "true"}
        and isinstance(model, str)
        and (model.lower() == "current" or any(s in model.lower() for s in ("deepseek", "dsv4")))
    )


def selection_reason(decision, state, critical):
    """Only consequential typed choices; no menus, mulligans, or routine passing."""
    if decision.request_type not in {"ActionsAvailable", "SelectTargets"}:
        return ""
    if critical:
        return str(critical)
    local = state.get("local_seat_id")
    if local is None:
        return ""
    hostile = any(
        c.get("controller_seat_id") not in (None, local) and c.get("targeting")
        for c in state.get("stack", []) or []
    )
    can_respond = decision.request_type == "SelectTargets" or any(
        (o.meta or {}).get("actionType") in {"ActionType_Cast", "ActionType_Activate"}
        and o.payable is not False
        for o in decision.options
    )
    return "response to a targeted opposing spell/ability" if hostile and can_respond else ""


def race(ask, parse, message, *, deadline, trace, cancel_event=None):
    """First validated answer wins; rejected fast answers leave the thinker a chance.

    Both workers get the SAME absolute deadline. Never join a stalled backend.
    Keep the admission slot until its worker exits, even after cancellation.
    The caller owns board/factual validation in parse, identically for both legs.
    """
    if deadline <= time.monotonic():
        raise TimeoutError("selective decision deadline expired")
    if cancel_event is not None and cancel_event.is_set():
        raise concurrent.futures.CancelledError("decision superseded")
    admission = _THINKER
    if not admission.acquire(blocking=False):
        trace.update(admitted=False, skipped="another thinker is still active", selected_leg="fast")
        return parse(ask(message, effort=None, budget=max(0, deadline - time.monotonic())))
    trace.update(
        policy=POLICY, admitted=True, deadline_monotonic=deadline, effort=1, thinking_token_budget=256
    )
    started = time.monotonic()
    condition = threading.Condition()
    results = {}
    cancels = {leg: threading.Event() for leg in ("fast", "thinking")}

    def run(leg):
        try:
            remaining = deadline - time.monotonic()
            if (
                remaining <= 0
                or cancels[leg].is_set()
                or (cancel_event is not None and cancel_event.is_set())
            ):
                raise concurrent.futures.CancelledError("decision expired or superseded")
            value = parse(
                ask(
                    message,
                    effort="selective" if leg == "thinking" else None,
                    budget=remaining,
                    cancel_event=cancels[leg],
                )
            )
            outcome = (True, value)
        except Exception as exc:
            outcome = (False, exc)
        finally:
            if leg == "thinking":
                admission.release()
        with condition:
            results[leg] = (*outcome, time.monotonic())
            condition.notify_all()

    for leg in ("thinking", "fast"):
        threading.Thread(target=run, args=(leg,), daemon=True, name=f"selective-{leg}").start()
    winner = None
    try:
        with condition:
            while time.monotonic() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    raise concurrent.futures.CancelledError("decision superseded")
                ready = [
                    (result[2], leg) for leg, result in results.items() if result[0] and result[2] <= deadline
                ]
                if ready:
                    winner = min(ready)[1]
                    break
                if len(results) == 2:
                    raise ValueError("both selective decision answers rejected")
                condition.wait(min(0.02, max(0, deadline - time.monotonic())))
            if winner is None:
                raise TimeoutError("selective decision deadline exhausted")
            return results[winner][1]
    finally:
        for leg, event in cancels.items():
            if leg != winner:
                event.set()
        with condition:
            trace["selected_leg"] = winner
            trace["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
            trace["legs"] = {
                leg: {
                    "status": "valid" if r[0] else "rejected",
                    "elapsed_ms": round((r[2] - started) * 1000, 1),
                    "option_ids": r[1][1] if r[0] else None,
                    "error": None if r[0] else str(r[1])[:200],
                    "cancel_requested": cancels[leg].is_set(),
                }
                if (r := results.get(leg)) is not None
                else {"status": "pending", "cancel_requested": cancels[leg].is_set()}
                for leg in cancels
            }
            fast = results.get("fast")
            trace["changed_action"] = (
                results[winner][1][1] != fast[1][1] if winner == "thinking" and fast and fast[0] else None
            )
            trace["replaced_rejected_fast"] = bool(winner == "thinking" and fast and not fast[0])
