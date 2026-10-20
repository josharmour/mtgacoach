"""A target window handed to the player: asked once, answered before Arena's clock ends.

2026-10-18 20:49:09.359 MANUAL REQUIRED for Spellbook Vendor's SelectTargets
(gameStateId 333, msgId 462). 0.19 s later the coaching loop's backstop
re-forced the same window ("Autopilot backstop: forced decision_required"):
its board-based given-up check did not match the autopilot's, so the window
was planned again (a second LLM call), refused again and declared MANUAL
REQUIRED again at 20:49:11.501 with a second Oops incident ("checking again
in 10s" twice; the second suppressed only by the 60 s Oops gap). Nothing
would have answered it had the user not: a target choice with no confirmed
side had no watchdog safe default.
"""

from __future__ import annotations

import pytest
from tests.fakes import TypedBridge, make_engine
from tests.test_target_veto_2026_10_18 import (
    ABILITIES,
    ME,
    OPP,
    creature,
    forest,
    pick,
    planner,
    poll_for,
    slots_of,
    state_for,
)

import arenamcp.action_planner as action_planner
from arenamcp.oops import STUCK_MANUAL, OopsController

PHASE_OUT = 556  # an effect neither Arena (Tepid) nor the text rules can side
PHASE_TEXT = "Target permanent phases out."


@pytest.fixture(autouse=True)
def _arena_ability_text(monkeypatch):
    abilities = {**ABILITIES, PHASE_OUT: PHASE_TEXT}
    monkeypatch.setattr(action_planner, "_ability_rules_text", lambda aid: abilities.get(int(aid), ""))


class TargetBridge(TypedBridge):
    def __init__(self, poll):
        super().__init__(poll)
        self.cancelled = 0
        self.auto_responded = 0

    def cancel_action(self):
        self.cancelled += 1
        return True

    def auto_respond(self):
        self.auto_responded += 1
        return True


SLOTS = slots_of((PHASE_OUT, [(445, "Tepid"), (700, "Tepid")], 1, 1))


def ambiguous_poll(*, can_cancel=False, msg=462):
    return poll_for(SLOTS, gsid=333, msg=msg, can_cancel=can_cancel)


def ambiguous_state():
    return state_for(
        SLOTS,
        source=613,
        battlefield=[forest(445), creature(700, "Grizzly Bears", OPP)],
        stack=[
            {"instance_id": 613, "name": "Odd Trigger", "oracle_text": PHASE_TEXT, "controller_seat_id": ME}
        ],
    )


def handed_engine(monkeypatch, *, can_cancel=False):
    """The engine right after the 20:49:09 stand-down: the model's pick refused, MANUAL REQUIRED."""
    bridge = TargetBridge(ambiguous_poll(can_cancel=can_cancel))
    model = planner(pick("tgt:700", reason="phase out their blocker"))
    engine = make_engine(bridge, model)
    pauses = []
    monkeypatch.setattr(engine, "_pause_for_manual", lambda reason, state=None: pauses.append(reason))
    if can_cancel:
        # A cancellable request is backed out of at once; hand it over the way a
        # failed cancel would, to exercise the watchdog's own fallbacks.
        bridge.cancel_action = lambda: False
    assert engine._try_typed_decision_path(ambiguous_state(), "decision_required") is True
    assert pauses == ["SelectTargets: no safe automatic choice — pick manually"]
    assert bridge.submitted == []
    return engine, bridge, model, pauses


def test_a_handed_over_request_is_not_planned_or_announced_again(monkeypatch):
    engine, bridge, model, pauses = handed_engine(monkeypatch)
    # The backstop's re-forced trigger on the same request.
    assert engine._try_typed_decision_path(ambiguous_state(), "decision_required") is True
    assert len(model._backend.prompts) == 1
    assert pauses == ["SelectTargets: no safe automatic choice — pick manually"]
    assert engine.request_handed_to_user(ambiguous_poll()) is True
    # A new request (the next message) is planned normally.
    assert engine.request_handed_to_user(ambiguous_poll(msg=465)) is False
    bridge.poll_resp = ambiguous_poll(msg=465)
    engine._try_typed_decision_path(ambiguous_state(), "decision_required")
    assert len(model._backend.prompts) == 2


def test_the_watchdog_submits_the_models_pick_when_nothing_else_is_safe(monkeypatch, caplog):
    engine, bridge, _, _ = handed_engine(monkeypatch)
    with caplog.at_level("INFO"):
        assert engine.watchdog_safe_default(ambiguous_state(), reason="20s left") == (
            True,
            "submitted the safe default",
        )
    assert bridge.submitted == [("targets", [700])]
    assert "the model's own pick" in caplog.text


def test_the_watchdog_backs_out_of_a_cancellable_request(monkeypatch):
    engine, bridge, _, _ = handed_engine(monkeypatch, can_cancel=True)
    bridge.cancel_action = TargetBridge.cancel_action.__get__(bridge)
    submitted, detail = engine.watchdog_safe_default(ambiguous_state())
    assert (submitted, detail) == (True, "submitted the safe default")
    assert bridge.cancelled == 1 and bridge.submitted == []


def test_without_a_model_pick_the_watchdog_lets_arena_choose(monkeypatch):
    bridge = TargetBridge(ambiguous_poll())
    engine = make_engine(bridge, planner("no json"))
    assert engine.watchdog_safe_default(ambiguous_state()) == (True, "submitted the safe default")
    assert bridge.auto_responded == 1


def test_a_one_sided_unclassified_target_gets_the_blind_pick(monkeypatch):
    slots = slots_of((PHASE_OUT, [(445, "Tepid"), (458, "Tepid")], 1, 1))
    state = state_for(
        slots,
        source=613,
        battlefield=[forest(445), forest(458)],
        stack=[
            {"instance_id": 613, "name": "Odd Trigger", "oracle_text": PHASE_TEXT, "controller_seat_id": ME}
        ],
    )
    bridge = TargetBridge(poll_for(slots, gsid=86, msg=119, can_cancel=False))
    engine = make_engine(bridge, planner("no json"))
    assert engine.watchdog_safe_default(state) == (True, "submitted the safe default")
    assert bridge.submitted in ([("targets", [445])], [("targets", [458])])
    assert bridge.auto_responded == 0


def test_one_stuck_window_is_one_oops_incident():
    clock = [100.0]
    oops = OopsController(
        emote_fn=lambda state: {"ok": True},
        settings_get=lambda key, default=None: default,
        autopilot_on=lambda: True,
        bridge_ready=lambda: True,
        clock=lambda: clock[0],
    )
    state = {"match_id": "80d57695", "turn": {"turn_number": 13}}
    reason = "SelectTargets: no safe automatic choice — pick manually"
    assert oops.report(STUCK_MANUAL, (reason, "window-a"), reason, state) is True
    clock[0] += 2.1  # 20:49:11.502, the re-declared window with a different board hash
    assert oops.report(STUCK_MANUAL, (reason, "window-b"), reason, state) is False
    assert len(oops._pending) == 1
