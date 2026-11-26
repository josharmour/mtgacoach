"""Regression and control tests for 2026-11-26 bug_20261126_093513:

1. -N/-N survivor guard respecting follow-up burn in hand and kicked shrink reach
   (Eject the Warp Core -2/-2 + Planetary Bombardment 2 damage vs 5/4 Khan).
2. SelectN mandatory/payment selection framing and approved 1-of-1 fast path.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner
from arenamcp.decision_memory import DecisionRecord, stack_burn_combo_on
from arenamcp.decisions import (
    SELECT_N_DECLINE,
    DecisionOption,
    PendingDecision,
    build_pending_decision,
    select_n_note,
)

EJECT_ORACLE = (
    "Kicker—Sacrifice an artifact or creature.\n"
    "Target creature gets -2/-2 until end of turn. "
    "If this spell was kicked, that creature gets -6/-6 until end of turn instead."
)
BOMBARDMENT_ORACLE = (
    "Planetary Bombardment deals 2 damage to target creature. "
    "Planetary Bombardment deals 4 damage to that creature instead if a creature died this turn."
)


class _FixedJsonBackend:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls = 0

    def complete(self, *args, **kwargs) -> str:
        self.calls += 1
        return json.dumps(self._payload)


class _FailIfCalledBackend:
    def complete(self, *args, **kwargs) -> str:
        raise AssertionError("LLM should not be called for approved 1-of-1 payment SelectN")


def _eject_state(
    *,
    untapped_lands: int = 5,
    hand_cards: list[dict] | None = None,
    active_player: int = 1,
    phase: str = "Phase_Main1",
    khan_keywords: list[str] | None = None,
) -> dict:
    lands = [
        {
            "instance_id": 100 + i,
            "name": "Mountain" if i < 2 else "Swamp",
            "type_line": "Basic Land — Mountain" if i < 2 else "Basic Land — Swamp",
            "controller_seat_id": 1,
            "owner_seat_id": 1,
            "is_tapped": False,
        }
        for i in range(untapped_lands)
    ]
    hand = (
        hand_cards
        if hand_cards is not None
        else [
            {
                "instance_id": 273,
                "name": "Planetary Bombardment",
                "mana_cost": "{R}",
                "type_line": "Sorcery",
                "oracle_text": BOMBARDMENT_ORACLE,
                "owner_seat_id": 1,
            }
        ]
    )
    khan = {
        "instance_id": 265,
        "name": "Khan, Despot by Design",
        "type_line": "Legendary Creature — Human Noble",
        "power": 5,
        "toughness": 4,
        "controller_seat_id": 2,
        "owner_seat_id": 2,
        "oracle_text": "Trample",
        "keywords": khan_keywords or ["Trample"],
    }
    return {
        "match_id": "m-eject",
        "local_seat_id": 1,
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 15, "mana_pool": {}},
            {"seat_id": 2, "is_local": False, "life_total": 14, "mana_pool": {}},
        ],
        "turn": {"turn_number": 12, "active_player": active_player, "phase": phase, "step": ""},
        "stack": [
            {
                "instance_id": 246,
                "name": "Eject the Warp Core",
                "mana_cost": "{1}{B}",
                "type_line": "Instant",
                "oracle_text": EJECT_ORACLE,
                "controller_seat_id": 1,
                "owner_seat_id": 1,
            }
        ],
        "hand": hand,
        "battlefield": [*lands, khan],
        "graveyard": [],
    }


def _target_decision() -> PendingDecision:
    return PendingDecision(
        request_id=(272, 10),
        request_type="SelectTargets",
        options=(DecisionOption(option_id="tgt:265", label="Khan, Despot by Design"),),
        min_select=1,
        max_select=1,
        can_cancel=True,
        source_label="Eject the Warp Core",
        source_instance_id=246,
        source_oracle_text=EJECT_ORACLE,
    )


def _bare_shrink_planner(reasoning: str = "") -> ActionPlanner:
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._decision_source_oracle = lambda decision, state: EJECT_ORACLE
    planner._decision_source_instance = lambda state: 246
    planner._last_decision_reasoning = reasoning
    planner._last_decision_trace = {}
    return planner


# --- Fix 2: -N/-N survivor guard & kicked shrink -------------------------------------------


def test_shrink_followup_named_burn_keeps_target_via_plan_decision_options():
    backend = _FixedJsonBackend(
        {
            "option_ids": ["tgt:265"],
            "reasoning": "Debuff Khan to 3/2 so Planetary Bombardment finishes it next.",
            "target_controllers": {"tgt:265": "opponent"},
        }
    )
    planner = ActionPlanner(backend)
    state = _eject_state()
    chosen = planner.plan_decision_options(_target_decision(), state)
    assert chosen == ["tgt:265"]


def test_shrink_followup_unnamed_burn_combo_keeps_target():
    state = _eject_state()
    combo = stack_burn_combo_on(
        state, 265, source_id=246, source_name="Eject the Warp Core", damage=2, allow_shrink=True
    )
    assert combo is not None
    assert "Planetary Bombardment" in combo.spells

    planner = _bare_shrink_planner("Target Khan.")
    chosen = planner._verify_shrink_pick(_target_decision(), state, ["tgt:265"])
    assert chosen == ["tgt:265"]


def test_kicked_shrink_uses_kicked_amount_and_keeps_target_without_burn():
    planner = _bare_shrink_planner("Kicked Eject gives -6/-6 and kills Khan.")
    planner._decision_memory = SimpleNamespace(
        this_turn=lambda _state: [
            DecisionRecord(
                turn=12,
                request_type="CastingTimeOptions",
                source="Eject the Warp Core",
                chosen=["idx:0"],
                chosen_labels=["Kicker"],
                reasoning="Kick Eject sacrificing D'Ghor",
                option_labels={"idx:0": "Kicker", "idx:1": "Done"},
            )
        ]
    )
    state = _eject_state(hand_cards=[])
    assert planner._verify_shrink_pick(_target_decision(), state, ["tgt:265"]) == ["tgt:265"]


def test_shrink_guard_control_declines_without_followup_or_mana_or_legal_timing():
    decision = _target_decision()

    # 1. No burn in hand (and not kicked) -> must decline.
    no_burn = _eject_state(hand_cards=[])
    assert _bare_shrink_planner("Shrink Khan.")._verify_shrink_pick(decision, no_burn, ["tgt:265"]) == [
        DECLINE_DECISION
    ]

    # 2. Insufficient mana (2 lands total for {1}{B} + {R} = 3 mana) -> must decline even if named.
    low_mana = _eject_state(untapped_lands=2)
    assert _bare_shrink_planner("Planetary Bombardment finishes Khan.")._verify_shrink_pick(
        decision, low_mana, ["tgt:265"]
    ) == [DECLINE_DECISION]

    # 3. Sorcery burn in hand on opponent's turn claiming a kill -> must decline.
    opp_turn = _eject_state(active_player=2, phase="Phase_Combat")
    assert _bare_shrink_planner("Planetary Bombardment kills Khan.")._verify_shrink_pick(
        decision, opp_turn, ["tgt:265"]
    ) == [DECLINE_DECISION]

    # 4. Burn damage too small (1 damage + -2/-2 = 3 < 4 toughness) -> must decline.
    weak_burn = _eject_state(
        hand_cards=[
            {
                "instance_id": 274,
                "name": "Spark Ping",
                "mana_cost": "{R}",
                "type_line": "Instant",
                "oracle_text": "Spark Ping deals 1 damage to target creature.",
                "owner_seat_id": 1,
            }
        ]
    )
    assert _bare_shrink_planner("Spark Ping finishes Khan.")._verify_shrink_pick(
        decision, weak_burn, ["tgt:265"]
    ) == [DECLINE_DECISION]

    # 5. Indestructible target -> must decline.
    indestr = _eject_state(khan_keywords=["Trample", "Indestructible"])
    assert _bare_shrink_planner("Planetary Bombardment finishes Khan.")._verify_shrink_pick(
        decision, indestr, ["tgt:265"]
    ) == [DECLINE_DECISION]

    # 6. Fallback pick never keeps unkillable shrink via follow-up -> declines once.
    fallback_state = _eject_state()
    assert _bare_shrink_planner("").targeting_fallback_choice(decision, fallback_state) == [DECLINE_DECISION]


# --- Fix 3: SelectN payment framing & approved single-option fast path ------------------------


def _payment_poll(ids: list[int], *, can_cancel: bool = True) -> dict:
    return {
        "has_pending": True,
        "request_type": "SelectN",
        "payment_selection": True,
        "can_cancel": can_cancel,
        "source_card": "Pay non-mana cost: Sacrifice an artifact or creature",
        "source_instance_id": 246,
        "decision_context": {
            "type": "select_n",
            "source_id": 246,
            "prompt_text": "Sacrifice an artifact or creature",
        },
        "select_n_ids": ids,
        "select_n_min": 1,
        "select_n_max": 1,
    }


def _pay_state() -> dict:
    return {
        "match_id": "m-pay",
        "local_seat_id": 1,
        "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2, "is_local": False}],
        "turn": {"turn_number": 12, "active_player": 1, "phase": "Phase_Main1"},
        "stack": [
            {
                "instance_id": 246,
                "name": "Eject the Warp Core",
                "oracle_text": EJECT_ORACLE,
                "controller_seat_id": 1,
            }
        ],
        "battlefield": [
            {"instance_id": 218, "name": "D'Ghor Bloodletter", "controller_seat_id": 1},
            {"instance_id": 260, "name": "Cybernetic Specialist", "controller_seat_id": 1},
        ],
        "hand": [],
        "_payment_selection_approved": True,
    }


def test_approved_single_option_payment_select_n_submits_without_llm():
    names = {218: "D'Ghor Bloodletter"}
    decision = build_pending_decision(
        _payment_poll([218], can_cancel=True),
        resolve_instance=lambda iid: names.get(iid, ""),
    )
    assert decision is not None
    assert decision.is_payment_selection is True

    planner = ActionPlanner(_FailIfCalledBackend())
    chosen = planner.plan_decision_options(decision, _pay_state())
    assert chosen == ["sel:218"]
    assert planner._last_decision_trace.get("policy") == "single_option"


def test_two_option_payment_select_n_prompts_with_mandatory_payment_framing():
    names = {218: "D'Ghor Bloodletter", 260: "Cybernetic Specialist"}
    decision = build_pending_decision(
        _payment_poll([218, 260], can_cancel=True),
        resolve_instance=lambda iid: names.get(iid, ""),
    )
    assert decision is not None
    state = _pay_state()
    note = select_n_note(decision, state)
    assert "MANDATORY PAYMENT SELECTION" in note
    assert "already committed" in note
    assert "do NOT return option_ids: []" in note

    backend = _FixedJsonBackend({"option_ids": ["sel:260"], "reasoning": "Sacrifice the smaller body."})
    planner = ActionPlanner(backend)
    chosen = planner.plan_decision_options(decision, state)
    assert chosen == ["sel:260"]
    assert backend.calls == 1


def test_select_n_controls_optional_unapproved_cancel_blind_and_forced():
    # Control A: Optional SelectN keeps OPTIONAL framing and does not fast-path.
    opt_decision = PendingDecision(
        request_id=(1, 1),
        request_type="SelectN",
        options=(
            DecisionOption(option_id="sel:218", label="D'Ghor Bloodletter", meta={"identity_known": True}),
        ),
        min_select=0,
        max_select=1,
        can_cancel=True,
    )
    opt_note = select_n_note(opt_decision, {})
    assert "OPTIONAL SELECTION" in opt_note
    assert "MANDATORY" not in opt_note
    planner = ActionPlanner(_FailIfCalledBackend())
    assert planner._single_option_answer(opt_decision, payment_selection_approved=True) is None

    # Control B: SelectN with SELECT_N_DECLINE keeps OPTIONAL framing.
    decl_decision = PendingDecision(
        request_id=(1, 2),
        request_type="SelectN",
        options=(
            DecisionOption(option_id="sel:218", label="Card", meta={"identity_known": True}),
            DecisionOption(option_id=SELECT_N_DECLINE, label="Decline", meta={"identity_known": True}),
        ),
        min_select=1,
        max_select=1,
        can_cancel=True,
    )
    assert "OPTIONAL SELECTION" in select_n_note(decl_decision, {})

    # Control C: Cancellable non-payment 1-option SelectN does not auto-submit.
    cancel_non_pay = PendingDecision(
        request_id=(1, 3),
        request_type="SelectN",
        options=(
            DecisionOption(option_id="sel:218", label="D'Ghor Bloodletter", meta={"identity_known": True}),
        ),
        min_select=1,
        max_select=1,
        can_cancel=True,
        is_payment_selection=False,
    )
    assert planner._single_option_answer(cancel_non_pay, payment_selection_approved=True) is None

    # Control D: Forced 1-of-1 unidentified SelectN does not auto-submit blind.
    blind = PendingDecision(
        request_id=(1, 4),
        request_type="SelectN",
        options=(DecisionOption(option_id="sel:999", label="Option 999", meta={"identity_known": False}),),
        min_select=1,
        max_select=1,
        can_cancel=False,
    )
    assert planner._single_option_answer(blind, payment_selection_approved=True) is None

    # Control E: Non-cancellable forced 1-of-1 identified SelectN auto-submits directly.
    forced = PendingDecision(
        request_id=(1, 5),
        request_type="SelectN",
        options=(
            DecisionOption(option_id="sel:218", label="D'Ghor Bloodletter", meta={"identity_known": True}),
        ),
        min_select=1,
        max_select=1,
        can_cancel=False,
    )
    assert planner._single_option_answer(forced) == ["sel:218"]
