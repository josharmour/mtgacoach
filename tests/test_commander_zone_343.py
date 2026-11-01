"""Replay #343's exact recipient and surviving copies through the optional choice."""

import json
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner
from arenamcp.commander_choice import commander_zone_note, false_battlefield_decline
from arenamcp.decisions import build_pending_decision
from arenamcp.gamestate import GameState
from arenamcp.gamestate_decisions import _handle_decision_message
from arenamcp.optional_prompts import prompt_note

BAD_REASON = (
    "Keep the commander on the battlefield; its tap engine and body are needed, "
    "and moving it to the command zone would cost recast tax."
)


@pytest.fixture
def replay():
    saved = json.loads((Path(__file__).parent / "fixtures/commander_343.json").read_text())
    game = GameState()
    game.local_seat_id = 2
    game._card_name_cache[103511] = "The Notary Hobbits"
    game.update_from_message(saved["game_state"])
    message = saved["optional_message"]
    _handle_decision_message(game, message["type"], message)
    state = {
        "local_seat_id": 2,
        "match_id": "9f593806-560b-4dcc-882a-5fef170f19ba",
        "turn": {"turn_number": 14, "active_player": 2, "phase": "Phase_Combat", "step": "Step_CombatDamage"},
        "decision_context": game.decision_context,
        "players": [{"seat_id": 2, "is_local": True, "life_total": 6}],
    }
    for zone in ("battlefield", "graveyard"):
        state[zone] = [dict(c.to_dict(), name="The Notary Hobbits") for c in getattr(game, zone)]
    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "OptionalAction",
            "game_state_id": 407,
            "msg_id": 542,
            "optional_mechanics": ["ZoneTransfer"],
            "optional_recipients": [888],
            "request_payload": message,
        },
        resolve_instance=lambda iid: "The Notary Hobbits" if iid == 888 else "",
    )
    return decision, state


def planner_with(*responses):
    backend = Mock()
    backend.complete.side_effect = [
        json.dumps({"option_ids": [response[0]], "reasoning": response[1]})
        if isinstance(response, tuple)
        else response
        for response in responses
    ]
    planner = ActionPlanner(backend, separate_strategy_planning=True)
    planner._has_deck_rule = lambda rule: rule == "commander_zone"
    return planner, backend


def test_actual_recipient_is_dead_despite_surviving_same_name_tokens(replay):
    decision, state = replay
    assert state["decision_context"]["commander_return"]
    assert [c["instance_id"] for c in state["graveyard"]] == [888]
    assert {c["instance_id"] for c in state["battlefield"]} == {835, 836}
    assert all(c["object_kind"] == "TOKEN" for c in state["battlefield"])
    note = prompt_note(decision, state)
    assert "[instance 888] is already in graveyard" in note
    assert "cannot keep or restore it on the battlefield" in note
    assert "does not cast it or pay a recast tax now" in note


@pytest.mark.parametrize(
    "choice,reason",
    [
        ("optional:accept", "Return the dead commander to command for future access."),
        ("optional:decline", "Leave it in the graveyard for our reanimation spell next turn."),
    ],
)
def test_false_battlefield_reason_gets_one_grounded_correction(replay, choice, reason):
    planner, backend = planner_with(("optional:decline", BAD_REASON), (choice, reason))
    assert planner.plan_decision_options(*replay) == [choice]
    assert backend.complete.call_count == 2
    trace = planner._last_decision_trace["commander_zone_check"]
    assert trace["original_reasoning"] == BAD_REASON
    assert trace["revised_ids"] == [choice]
    assert "instance 888" in trace["facts"]


@pytest.mark.parametrize(
    "reason",
    [
        "Leave the commander in the graveyard for reanimation instead of paying recast tax later.",
        "Keep the token copies on the battlefield and leave the original in the graveyard for recursion.",
        "Declining cannot keep the commander on the battlefield, but our pending trigger returns it.",
        "Keep it in the graveyard; the commander cannot stay on the battlefield now.",
        "Keep our blocker on the battlefield and reanimate the commander later.",
        "Keep it in the graveyard to return it on the battlefield next turn.",
    ],
)
def test_legitimate_graveyard_choices_stand_without_retry(replay, reason):
    planner, backend = planner_with(("optional:decline", reason))
    assert planner.plan_decision_options(*replay) == ["optional:decline"]
    assert backend.complete.call_count == 1
    assert "commander_zone_check" not in planner._last_decision_trace


@pytest.mark.parametrize("second", [("optional:decline", BAD_REASON), TimeoutError("budget exhausted")])
def test_failed_correction_never_submits_known_false_decline(replay, second):
    planner, backend = planner_with(("optional:decline", BAD_REASON), second)
    assert planner.plan_decision_options(*replay) == [DECLINE_DECISION]
    assert backend.complete.call_count == 2
    assert "failed" in planner._last_decision_trace["commander_zone_check"]


@pytest.mark.parametrize(
    "case",
    ["stale", "other_recipient", "unknown", "opponent", "token", "battlefield", "duplicate", "unrecognized"],
)
def test_requires_current_exact_owned_card_in_known_postmove_zone(replay, case):
    decision, state = replay
    card = state["graveyard"][0]
    if case == "stale":
        decision = replace(decision, request_id=(407, 543))
    elif case == "other_recipient":
        state["decision_context"]["recipient_ids"] = [835]
    elif case == "unknown":
        state["graveyard"] = []
    elif case == "opponent":
        card["owner_seat_id"] = 1
    elif case == "token":
        card["object_kind"] = "TOKEN"
    elif case == "battlefield":
        state["graveyard"] = []
        state["battlefield"].append(card)
    elif case == "duplicate":
        state["battlefield"].append(card)
    else:
        state["decision_context"]["commander_return"] = False
    assert commander_zone_note(decision, state) == ""


def test_exile_facts_preserve_recovery_choices(replay):
    decision, state = replay
    state["exile"] = state.pop("graveyard")
    assert "already in exile" in commander_zone_note(decision, state)
    planner, backend = planner_with(("optional:decline", "Our delayed trigger returns it from exile."))
    assert planner.plan_decision_options(decision, state) == ["optional:decline"]
    assert backend.complete.call_count == 1


def test_accept_not_rejected_for_battlefield_language():
    assert not false_battlefield_decline(["optional:accept"], "Recast to keep it on the battlefield.")


def test_correction_uses_remaining_decision_budget(replay, monkeypatch):
    planner, backend = planner_with()
    budgets = []

    def complete(*args, **kwargs):
        budgets.append(kwargs["request_timeout_s"])
        if len(budgets) == 1:
            assert "instance 888" in args[1]
            time.sleep(0.04)
            return json.dumps({"option_ids": ["optional:decline"], "reasoning": BAD_REASON})
        assert "COMMANDER ZONE CHECK" in args[1]
        return json.dumps({"option_ids": ["optional:accept"], "reasoning": "Return the dead commander."})

    backend.complete.side_effect = complete
    monkeypatch.setattr(planner, "_sized_budget", lambda *args: 1.0)
    assert planner.plan_decision_options(*replay) == ["optional:accept"]
    assert len(budgets) == 2
    assert 0 < budgets[1] < budgets[0] - 0.03


def test_failed_correction_has_explicit_handoff_and_watchdog_path(replay, monkeypatch):
    """No false decline, repeated model call, or invisible unanswered request."""
    from tests.fakes import make_engine

    decision, state = replay
    planner, backend = planner_with(("optional:decline", BAD_REASON), TimeoutError("no budget"))
    poll = {
        "has_pending": True,
        "request_type": "OptionalAction",
        "request_class": "OptionalActionMessageRequest",
        "game_state_id": decision.request_id[0],
        "msg_id": decision.request_id[1],
        "optional_mechanics": ["ZoneTransfer"],
        "optional_recipients": [888],
        "can_cancel": False,
        "decision_context": state["decision_context"],
    }
    bridge = Mock(connected=True)
    bridge.get_pending_actions.return_value = poll
    bridge.auto_respond.return_value = True
    engine = make_engine(bridge, planner, game_state=state)
    manual = Mock()
    monkeypatch.setattr(engine, "_pause_for_manual", manual)
    monkeypatch.setattr(engine, "_record_autopilot_decision", Mock())
    monkeypatch.setattr(engine, "_log_execution_path", Mock())
    assert engine._try_typed_decision_path(state, "decision_required") is True
    assert "no safe automatic choice" in manual.call_args.args[0]
    assert engine.request_handed_to_user(poll)
    bridge.submit_optional.assert_not_called()
    assert backend.complete.call_count == 2
    assert engine._try_typed_decision_path(state, "decision_required") is True
    assert backend.complete.call_count == 2
    submitted, detail = engine.watchdog_safe_default(state, "clock nearly expired")
    assert submitted and detail == "submitted the safe default"
    bridge.auto_respond.assert_called_once()
    bridge.submit_optional.assert_not_called()
