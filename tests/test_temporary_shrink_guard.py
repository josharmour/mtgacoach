"""Temporary -X/-Y shrinks and discard-for-effect, from bug_20261007_183358 (2026-10-07 18:33, Pick-Two FRA T16).

standalone.log / Player.log, our turn 16 main phase 1 at 3 life to their 34:

* 18:33:23 (gameStateId 436) the menu offered "Cast Void Extrapolator [OK]",
  "Cast Proft, Sinister Mastermind [OK]" (threshold live: eight cards in our
  graveyard; a 5/5 menace), Cryotheory Adept's stun and Proft's "{B}, Discard
  this card: Target creature gets -3/-1 until end of turn". The model cast
  Void Extrapolator (idx:0).
* 18:33:26 (437) with our own spell on the stack only the discard remained:
  "Discarding uncastable Proft for {B} kills Hallway Heckler" — a 2/3 goes to
  -1/2 and lives. Proft was castable again once the stack emptied.
* 18:33:31 (439) the target became the 6/4 Apex Witchstalker, "removes the
  biggest crackback threat": -3/-1 wore off at our end step, before their
  attack. The 5/5 was discarded for nothing.

The states are tests/strategic_states.py's BUG_183358 (the report, checked
against its whitelisted copy) wound back to those two windows.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from typing import Any

import pytest
from tests import strategic_states as fx

from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner
from arenamcp.backends.proxy import BackendError
from arenamcp.decisions import build_pending_decision
from arenamcp.play_safety import (
    castable_discard_for_minor_effect,
    claimed_kills,
    filter_play_options,
    shrink_note,
    shrink_would_kill,
    temporary_shrink_wasted,
    unsafe_play_reason,
)

LOCAL, OPP = 2, 1
PROFT, HECKLER, APEX, SOULBREAKER = 396, 271, 389, 292
SHRINK_ID, SHRINK_META = fx.BUG_183358_PROFT_SHRINK[0], fx.BUG_183358_PROFT_SHRINK[2]
SHRINK_TEXT = "Target creature gets -3/-1 until end of turn."
MODEL_18_33_29 = (
    "Discarding uncastable Proft for {B} kills Hallway Heckler, removing their card-draw engine "
    "and a blocker/potential attacker."
)
MODEL_18_33_31 = (
    "Shrinking the 6/4 Apex Witchstalker to 3/3 removes the biggest crackback threat while keeping "
    "my blockers safe."
)


class _Backend:
    def __init__(self, *, answer: str = "", error: Exception | None = None):
        self.answer, self.error, self.prompts = answer, error, []

    def available(self) -> bool:
        return True

    def complete(self, system_prompt, user_message, *args, **kwargs):
        self.prompts.append(user_message)
        if self.error is not None:
            raise self.error
        return self.answer


def _planner(reply: str | None = None) -> ActionPlanner:
    backend = (
        _Backend(answer=reply) if reply is not None else _Backend(error=BackendError("Request timed out."))
    )
    return ActionPlanner(backend=backend, timeout=5.0)


def _card(state: dict[str, Any], instance_id: int) -> dict[str, Any]:
    return next(
        c
        for zone in ("battlefield", "hand", "graveyard", "stack")
        for c in state[zone]
        if c["instance_id"] == instance_id
    )


def _with_toughness(state: dict[str, Any], instance_id: int, toughness: int) -> dict[str, Any]:
    return fx.amend(state, cards={instance_id: {"toughness": toughness}})


def _their_combat(state: dict[str, Any]) -> dict[str, Any]:
    """Their next turn's declare-blockers step: Apex Witchstalker attacks us."""
    return fx.amend(
        state,
        cards={APEX: {"is_attacking": True, "attack_target_id": LOCAL}},
        turn={**state["turn"], "turn_number": 17, "active_player": OPP, "priority_player": LOCAL, "phase": "Combat", "step": "DeclareBlock"},
    )  # fmt: skip


def _shrink_window_menu():
    return fx.actions_decision(fx.BUG_183358_ON_STACK_MENU)


def _option_ids(decision) -> set[str]:
    return {option.option_id for option in decision.options}


def _targets_decision(ids):
    names = {
        HECKLER: "Hallway Heckler",
        APEX: "Apex Witchstalker",
        SOULBREAKER: "Screeching Soulbreaker",
        256: "Screeching Soulbreaker",
    }
    slot = [
        {"targetInstanceId": iid, "targetIdx": 1, "grpId": 0, "legalAction": "Select", "highlight": "Tepid"}
        for iid in ids
    ]
    poll = {
        "has_pending": True, "request_type": "SelectTargets", "request_class": "SelectTargetsRequest",
        "game_state_id": 438, "msg_id": 700, "can_cancel": True,
        "target_selections": [{"targetIdx": 1, "minTargets": 1, "maxTargets": 1, "selectedTargets": 0, "targets": slot}],
        "target_candidates": slot,
    }  # fmt: skip
    return build_pending_decision(poll, resolve_instance=lambda iid: names.get(iid, ""))


def _targeting_state(base: dict[str, Any]) -> dict[str, Any]:
    """18:33:29 (gameStateId 438): Proft's ability (400) on the stack asking for its target."""
    ability = {
        "instance_id": 400, "grp_id": 208364, "name": "Ability (ID: 208364)", "type_line": "Ability",
        "oracle_text": SHRINK_TEXT, "owner_seat_id": LOCAL, "controller_seat_id": LOCAL,
        "object_kind": "ABILITY", "parent_instance_id": PROFT,
    }  # fmt: skip
    return fx.amend(
        base,
        stack=[ability, *base.get("stack", [])],
        decision_context={"type": "target_selection", "source_card": "Proft, Sinister Mastermind", "source_id": 400, "source_parent_instance_id": PROFT, "source_oracle_text": SHRINK_TEXT},
        _bridge_request_payload={"requestType": "SelectTargets", "requestClass": "SelectTargetsRequest", "sourceId": 400, "abilityGrpId": 208364},
        _bridge_request_type="SelectTargets",
    )  # fmt: skip


# --- the board -----------------------------------------------------------------------


def test_main_phase_board_is_the_18_33_23_window():
    state = fx.BUG_183358_T16_MAIN1
    assert (state["turn"]["phase"], state["turn"]["active_player"]) == ("Main1", LOCAL)
    assert [c["name"] for c in state["hand"]] == ["Proft, Sinister Mastermind", "Void Extrapolator"]
    assert (_card(state, APEX)["power"], _card(state, APEX)["toughness"]) == (6, 4)
    assert len([c for c in state["graveyard"] if c["owner_seat_id"] == LOCAL]) == 8  # threshold live
    ours = [c for c in state["battlefield"] if c["controller_seat_id"] == LOCAL and "Land" in c["card_types"]]
    assert len(ours) == 7 and not any(c["is_tapped"] for c in ours)
    assert not _card(state, 256)["counters"]
    on_stack = fx.BUG_183358_ON_STACK
    assert [c["name"] for c in on_stack["stack"]] == ["Void Extrapolator"]
    assert [
        c["instance_id"]
        for c in on_stack["battlefield"]
        if c["controller_seat_id"] == LOCAL and c["is_tapped"]
    ] == [244, 248]


# --- 1. the -3/-1 on our main phase, nothing dies ------------------------------------


def test_shrink_kill_math_counts_marked_damage():
    assert shrink_would_kill({"toughness": 3}, 1) is False
    assert shrink_would_kill({"toughness": 1}, 1) is True
    assert shrink_would_kill({"toughness": 3, "damage": 2}, 1) is True
    assert shrink_would_kill({"toughness": 3, "is_attacking": True}, 1) is None


def test_proft_discard_is_withheld_on_our_main_phase_with_no_kill(caplog):
    state = fx.BUG_183358_T16_MAIN1
    reason = temporary_shrink_wasted(_card(state, PROFT), state, activation=True, metadata=SHRINK_META)
    assert "kills no opposing creature" in reason and "wears off" in reason
    with caplog.at_level(logging.INFO, logger="arenamcp.play_safety"):
        kept = filter_play_options(fx.actions_decision(fx.BUG_183358_MENU), state)
    assert _option_ids(kept) == {"idx:0", "idx:1", "idx:2", "pass"}
    assert "Withholding Activate: Proft" in caplog.text


def test_castable_proft_is_not_discarded_while_our_own_spell_is_on_the_stack():
    state = fx.BUG_183358_ON_STACK
    proft = _card(state, PROFT)
    # Both guards refuse it: the shrink kills nothing on our turn, and the card is castable
    # again ({2}{B} from five untapped lands) as soon as Void Extrapolator resolves.
    assert "wears off" in temporary_shrink_wasted(proft, state, activation=True, metadata=SHRINK_META)
    assert "castable now" in castable_discard_for_minor_effect(
        proft, {**state, "_bridge_actions": [SHRINK_META]}, SHRINK_META
    )
    assert _option_ids(filter_play_options(_shrink_window_menu(), state)) == {"pass"}


def test_discard_guard_needs_a_cast_on_offer_and_an_unproven_kill_keeps_it():
    # Their main phase (the shrink itself is fine there): the discard still loses a castable card
    # unless something dies — but only while a Cast of the same card is on offer.
    state = fx.amend(
        fx.BUG_183358_T16_MAIN1,
        turn={
            **fx.BUG_183358_T16_MAIN1["turn"],
            "turn_number": 17,
            "active_player": OPP,
            "priority_player": LOCAL,
        },
    )
    proft = _card(state, PROFT)
    assert temporary_shrink_wasted(proft, state, activation=True, metadata=SHRINK_META) == ""
    menu = {
        **state,
        "_bridge_actions": [
            SHRINK_META,
            {"actionType": "ActionType_Cast", "instanceId": PROFT, "hasAutoTap": True},
        ],
    }
    assert "castable now" in castable_discard_for_minor_effect(proft, menu, SHRINK_META)
    assert (
        castable_discard_for_minor_effect(proft, {**state, "_bridge_actions": [SHRINK_META]}, SHRINK_META)
        == ""
    )
    # An attacker's fate is not settled by the shrink alone (combat damage adds up): no refusal.
    assert (
        castable_discard_for_minor_effect(
            _card(state, PROFT),
            {**_their_combat(state), "_bridge_actions": menu["_bridge_actions"]},
            SHRINK_META,
        )
        == ""
    )


# --- it IS allowed when it kills, and during their combat ----------------------------


def test_shrink_stays_on_the_menu_when_it_kills_a_one_toughness_creature():
    state = _with_toughness(fx.BUG_183358_T16_MAIN1, HECKLER, 1)
    proft = _card(state, PROFT)
    assert temporary_shrink_wasted(proft, state, activation=True, metadata=SHRINK_META) == ""
    assert unsafe_play_reason(state, proft, "Activate", SHRINK_META) == ""
    assert SHRINK_ID in _option_ids(filter_play_options(fx.actions_decision(fx.BUG_183358_MENU), state))
    assert (
        shrink_note(state, proft, activation=True, metadata=SHRINK_META)
        == "  [-X/-1 until end of turn kills now: Hallway Heckler]"
    )


def test_shrink_stays_on_the_menu_during_their_combat():
    state = _their_combat(fx.BUG_183358_T16_MAIN1)
    proft = _card(state, PROFT)
    assert temporary_shrink_wasted(proft, state, activation=True, metadata=SHRINK_META) == ""
    assert unsafe_play_reason(state, proft, "Activate", SHRINK_META) == ""
    assert _option_ids(filter_play_options(_shrink_window_menu(), state)) == {"idx:0", "pass"}
    note = shrink_note(state, proft, activation=True, metadata=SHRINK_META)
    assert "kills nothing now" in note and "this turn's combat only" in note


def test_shrink_stays_on_the_menu_inside_our_own_combat():
    state = fx.amend(
        fx.BUG_183358_T16_MAIN1,
        cards={351: {"is_attacking": True, "attack_target_id": OPP}, HECKLER: {"is_blocking": True}},
        turn={**fx.BUG_183358_T16_MAIN1["turn"], "phase": "Combat", "step": "DeclareBlock"},
    )
    assert temporary_shrink_wasted(_card(state, PROFT), state, activation=True, metadata=SHRINK_META) == ""


def test_a_shrink_spell_from_hand_is_held_the_same_way():
    state = deepcopy(fx.BUG_183358_T16_MAIN1)
    spell = {
        "instance_id": 999, "grp_id": 1, "name": "Last Gasp", "type_line": "Instant", "mana_cost": "{1}{B}",
        "oracle_text": SHRINK_TEXT, "card_types": ["Instant"], "owner_seat_id": LOCAL, "controller_seat_id": LOCAL,
    }  # fmt: skip
    state["hand"].append(spell)
    assert "wears off" in unsafe_play_reason(
        state, spell, "Cast", {"actionType": "ActionType_Cast", "instanceId": 999}
    )
    assert (
        unsafe_play_reason(
            _their_combat(state), spell, "Cast", {"actionType": "ActionType_Cast", "instanceId": 999}
        )
        == ""
    )


# --- the fallback and the model's pick -----------------------------------------------


def test_fallback_never_picks_the_shrink():
    picked = _planner().plan_decision_options(
        fx.actions_decision(fx.BUG_183358_MENU), fx.BUG_183358_T16_MAIN1
    )
    assert picked and picked[0] in {"idx:0", "idx:1"}
    assert _planner().plan_decision_options(_shrink_window_menu(), fx.BUG_183358_ON_STACK) == ["pass"]


def test_model_choice_of_the_shrink_is_not_submitted(caplog):
    reply = json.dumps({"option_ids": [SHRINK_ID], "reasoning": MODEL_18_33_29})
    with caplog.at_level(logging.INFO):
        picked = _planner(reply).plan_decision_options(
            fx.actions_decision(fx.BUG_183358_MENU), fx.BUG_183358_T16_MAIN1
        )
    assert picked and picked[0] in {"idx:0", "idx:1"}
    reply = json.dumps({"option_ids": ["idx:0"], "reasoning": MODEL_18_33_29})
    assert _planner(reply).plan_decision_options(_shrink_window_menu(), fx.BUG_183358_ON_STACK) == ["pass"]


def test_prompt_options_carry_the_shrink_fact_and_the_rules():
    backend = _Backend(answer='{"option_ids": ["pass"], "reasoning": "hold"}')
    planner = ActionPlanner(backend=backend, timeout=5.0)
    planner.plan_decision_options(_shrink_window_menu(), _their_combat(fx.BUG_183358_T16_MAIN1))
    prompt = backend.prompts[-1]
    assert "[-X/-1 until end of turn kills nothing now" in prompt
    rules = ActionPlanner._DECISION_SYSTEM_PROMPT
    assert "until end of turn" in rules and "kills now" in rules
    assert "SEQUENCING" in rules and "Never discard a castable card" in rules
    assert "your own spell is on the stack" in rules


# --- 2. the model's stated kill is verified ------------------------------------------


def test_kill_claims_are_read_from_the_reasoning():
    opposing = [_card(fx.BUG_183358_T16_MAIN1, iid) for iid in (HECKLER, APEX, SOULBREAKER)]
    assert [c["name"] for c in claimed_kills(MODEL_18_33_29, opposing)] == ["Hallway Heckler"]
    assert claimed_kills(MODEL_18_33_31, opposing) == []
    assert [c["name"] for c in claimed_kills("the Heckler dies to -3/-1", opposing)] == ["Hallway Heckler"]
    assert [c["name"] for c in claimed_kills("finish off their Apex Witchstalker", opposing)] == [
        "Apex Witchstalker"
    ]


def test_false_kill_claim_on_their_turn_drops_the_activation(caplog):
    state = _their_combat(fx.BUG_183358_T16_MAIN1)
    reply = json.dumps({"option_ids": ["idx:0"], "reasoning": MODEL_18_33_29})
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        picked = _planner(reply).plan_decision_options(_shrink_window_menu(), state)
    assert picked == ["pass"]
    assert "claims kills Hallway Heckler (2/3)" in caplog.text and "cannot kill it" in caplog.text


def test_true_kill_claim_on_their_turn_is_kept():
    state = _with_toughness(_their_combat(fx.BUG_183358_T16_MAIN1), HECKLER, 1)
    reply = json.dumps({"option_ids": ["idx:0"], "reasoning": MODEL_18_33_29})
    assert _planner(reply).plan_decision_options(_shrink_window_menu(), state) == ["idx:0"]


# --- the target pick -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "reasoning"),
    [(HECKLER, MODEL_18_33_29), (APEX, MODEL_18_33_31)],
    ids=["claimed-kill", "no-kill-on-our-turn"],
)
def test_target_pick_that_kills_nothing_on_our_turn_is_declined(target, reasoning, caplog):
    state = _targeting_state(fx.BUG_183358_T16_MAIN1)
    decision = _targets_decision([HECKLER, APEX, SOULBREAKER, 256])
    reply = json.dumps(
        {
            "option_ids": [f"tgt:{target}"],
            "reasoning": reasoning,
            "target_controllers": {f"tgt:{target}": "opponent"},
        }
    )
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        assert _planner(reply).plan_decision_options(decision, state) == [DECLINE_DECISION]
    assert "overriding the model's" in caplog.text


def test_target_pick_is_moved_to_the_creature_that_dies():
    state = _targeting_state(_with_toughness(fx.BUG_183358_T16_MAIN1, HECKLER, 1))
    decision = _targets_decision([HECKLER, APEX, SOULBREAKER])
    reply = json.dumps(
        {
            "option_ids": [f"tgt:{APEX}"],
            "reasoning": "kills Apex Witchstalker, their biggest threat",
            "target_controllers": {f"tgt:{APEX}": "opponent"},
        }
    )
    assert _planner(reply).plan_decision_options(decision, state) == [f"tgt:{HECKLER}"]


def test_target_pick_during_their_combat_stands_without_a_kill():
    state = _targeting_state(_their_combat(fx.BUG_183358_T16_MAIN1))
    decision = _targets_decision([HECKLER, APEX, SOULBREAKER])
    reply = json.dumps(
        {
            "option_ids": [f"tgt:{APEX}"],
            "reasoning": "Shrinking the attacking Apex Witchstalker so Traxos survives the block",
            "target_controllers": {f"tgt:{APEX}": "opponent"},
        }
    )
    assert _planner(reply).plan_decision_options(decision, state) == [f"tgt:{APEX}"]


def test_target_fallback_without_the_model_keeps_the_card():
    state = _targeting_state(fx.BUG_183358_T16_MAIN1)
    assert _planner().plan_decision_options(_targets_decision([HECKLER, APEX, SOULBREAKER]), state) == [
        DECLINE_DECISION
    ]
