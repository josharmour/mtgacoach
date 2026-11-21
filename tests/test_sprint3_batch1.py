"""Regression tests for Sprint 3 Batch 1 (S3-1 bluff-land hold, S3-2 benign-trigger patience, S3-3 combat activation labels)."""

from __future__ import annotations

import json

from arenamcp.action_planner import ActionPlanner
from arenamcp.board_math_picks import board_math_option_pick
from arenamcp.burn_timing import hold_overrides
from arenamcp.decisions import DecisionOption, PendingDecision


class _FailBackend:
    def complete(self, *args, **kwargs):
        raise AssertionError("deterministic path should not call LLM")


class _CaptureBackend:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict[str, str]] = []

    def complete(self, system_prompt: str, user_message: str, **kwargs) -> str:
        self.calls.append({"system": system_prompt, "user": user_message})
        return self.reply


def _plain_land(iid: int, name: str = "Red Crag", seat: int = 1, tapped: bool = False) -> dict:
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Basic Land — Mountain",
        "oracle_text": "{T}: Add {R}.",
        "card_types": ["CardType_Land"],
        "subtypes": ["SubType_Mountain"],
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "is_tapped": tapped,
    }


def _bluff_land_state(
    n_lands: int = 6, extra_hand: list[dict] | None = None, extra_bf: list[dict] | None = None
) -> dict:
    lands = [_plain_land(10 + i) for i in range(n_lands)]
    sole = _plain_land(99, "Red Crag")
    return {
        "local_seat_id": 1,
        "turn": {
            "turn_number": 8,
            "active_player": 1,
            "priority_player": 1,
            "phase": "Phase_Main1",
            "step": "",
        },
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 18, "lands_played": 0},
            {"seat_id": 2, "is_local": False, "life_total": 14, "lands_played": 0},
        ],
        "hand": [sole, *(extra_hand or [])],
        "battlefield": [*lands, *(extra_bf or [])],
        "stack": [],
        "graveyard": [],
        "exile": [],
    }


def _bluff_land_decision() -> PendingDecision:
    return PendingDecision(
        (1, 1),
        "ActionsAvailable",
        (
            DecisionOption(
                "idx:0", "Play land: Red Crag", meta={"actionType": "ActionType_Play", "instanceId": 99}
            ),
            DecisionOption("pass", "Pass"),
        ),
    )


def test_s3_1_superfluous_bluff_land_passes_deterministically():
    """6+ lands on board, sole card in hand is a plain land -> deterministic Pass without LLM."""
    planner = ActionPlanner(_FailBackend())
    state = _bluff_land_state(n_lands=6)
    dec = _bluff_land_decision()
    assert planner.plan_decision_options(dec, state) == ["pass"]
    pick, _reason = board_math_option_pick(dec, state)
    assert pick == ["pass"]


def test_s3_1_control_plays_land_when_below_six_lands():
    """Control: with only 4 lands on board, the plain land is NOT held as a bluff."""
    state = _bluff_land_state(n_lands=4)
    dec = _bluff_land_decision()
    pick, _reason = board_math_option_pick(dec, state)
    assert pick == ["idx:0"]


def test_s3_1_control_plays_land_with_landfall_permanent():
    """Control: a landfall permanent on board still wants the 7th land dropped."""
    landfall_cre = {
        "instance_id": 50,
        "name": "Canyon Strider",
        "type_line": "Creature — Elemental",
        "oracle_text": "Landfall — Whenever a land you control enters, Canyon Strider gets +2/+2 until end of turn.",
        "card_types": ["CardType_Creature"],
        "power": 2,
        "toughness": 2,
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "is_tapped": False,
        "turn_entered_battlefield": 3,
    }
    state = _bluff_land_state(n_lands=6, extra_bf=[landfall_cre])
    dec = _bluff_land_decision()
    pick, _reason = board_math_option_pick(dec, state)
    assert pick == ["idx:0"]


def _benign_trigger_state(stack: list[dict]) -> dict:
    prowess_cre = {
        "instance_id": 200,
        "name": "Spark Adept",
        "type_line": "Creature — Mouse",
        "oracle_text": "Haste, prowess",
        "card_types": ["CardType_Creature"],
        "power": 2,
        "toughness": 2,
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "is_tapped": False,
        "turn_entered_battlefield": 3,
    }
    burn = {
        "instance_id": 201,
        "name": "Cinder Bolt",
        "type_line": "Instant",
        "mana_cost": "{1}{R}",
        "oracle_text": "Cinder Bolt deals 3 damage to any target.",
        "card_types": ["CardType_Instant"],
        "controller_seat_id": 1,
        "owner_seat_id": 1,
    }
    return {
        "local_seat_id": 1,
        "turn": {
            "turn_number": 5,
            "active_player": 1,
            "priority_player": 1,
            "phase": "Phase_Main1",
            "step": "",
        },
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20, "lands_played": 1},
            {"seat_id": 2, "is_local": False, "life_total": 16, "lands_played": 0},
        ],
        "hand": [burn],
        "battlefield": [_plain_land(10), _plain_land(11), _plain_land(12), prowess_cre],
        "stack": stack,
        "graveyard": [],
        "exile": [],
    }


def test_s3_2_benign_opposing_trigger_on_our_turn_does_not_override_burn_hold():
    """On our own turn with only a benign opposing lifegain trigger on stack, hold_overrides yields."""
    benign = {
        "instance_id": 300,
        "name": "Consul Watch ability",
        "object_kind": "ABILITY",
        "oracle_text": "Whenever a creature an opponent controls enters, you gain 1 life.",
        "controller_seat_id": 2,
        "owner_seat_id": 2,
    }
    state = _benign_trigger_state(stack=[benign])
    opt = DecisionOption(
        "idx:0", "Cast Cinder Bolt", meta={"actionType": "ActionType_Cast", "instanceId": 201}
    )
    dec = PendingDecision((1, 2), "ActionsAvailable", (opt, DecisionOption("pass", "Pass")))
    assert hold_overrides(dec, state, [(opt, state["hand"][0])]) == ""


def test_s3_2_control_empty_stack_still_allows_cast_pumped_override():
    """Control: once the benign trigger resolves (empty stack), cast_pumped_attackers still overrides."""
    state = _benign_trigger_state(stack=[])
    opt = DecisionOption(
        "idx:0", "Cast Cinder Bolt", meta={"actionType": "ActionType_Cast", "instanceId": 201}
    )
    dec = PendingDecision((1, 2), "ActionsAvailable", (opt, DecisionOption("pass", "Pass")))
    why = hold_overrides(dec, state, [(opt, state["hand"][0])])
    assert "Spark Adept" in why


def test_s3_3_combat_activation_prompt_distinguishes_unblocked_attacker_from_non_attacker():
    """In DeclareBlock, duplicate Activate options clearly label [UNBLOCKED ATTACKER] vs [NOT ATTACKING]."""
    attacker = {
        "instance_id": 401,
        "name": "Ember Scout",
        "type_line": "Creature — Lizard",
        "oracle_text": "{1}{R}: Scry 1.",
        "card_types": ["CardType_Creature"],
        "power": 1,
        "toughness": 2,
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "is_tapped": True,
        "is_attacking": True,
        "is_blocking": False,
        "blocked_by": [],
        "turn_entered_battlefield": 1,
    }
    sick = {
        "instance_id": 402,
        "name": "Ember Claw",
        "type_line": "Creature — Lizard",
        "oracle_text": "{1}{R}: Put a +1/+1 counter on this creature.",
        "card_types": ["CardType_Creature"],
        "power": 1,
        "toughness": 2,
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "is_tapped": False,
        "is_attacking": False,
        "is_blocking": False,
        "blocked_by": [],
        "turn_entered_battlefield": 5,
    }
    state = {
        "local_seat_id": 1,
        "turn": {
            "turn_number": 5,
            "active_player": 1,
            "priority_player": 1,
            "phase": "Phase_Combat",
            "step": "Step_DeclareBlock",
        },
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 15, "lands_played": 1},
            {"seat_id": 2, "is_local": False, "life_total": 5, "lands_played": 0},
        ],
        "hand": [],
        "battlefield": [_plain_land(10), _plain_land(11), attacker, sick],
        "stack": [],
        "graveyard": [],
        "exile": [],
    }
    dec = PendingDecision(
        (1, 3),
        "ActionsAvailable",
        (
            DecisionOption(
                "idx:0", "Activate: Ember Claw", meta={"actionType": "ActionType_Activate", "instanceId": 401}
            ),
            DecisionOption(
                "idx:1", "Activate: Ember Claw", meta={"actionType": "ActionType_Activate", "instanceId": 402}
            ),
            DecisionOption("pass", "Pass"),
        ),
    )
    backend = _CaptureBackend(json.dumps({"option_ids": ["idx:0"], "reasoning": "Pump unblocked attacker."}))
    planner = ActionPlanner(backend)
    assert planner.plan_decision_options(dec, state) == ["idx:0"]
    prompt = backend.calls[0]["user"]
    assert any("idx:0" in line and "[UNBLOCKED ATTACKER]" in line for line in prompt.splitlines())
    assert any("idx:1" in line and "[NOT ATTACKING]" in line for line in prompt.splitlines())


def test_s3_1_bluff_land_still_held_when_lethal_on_board():
    """Major #1: lethal attackers on board must not drop a superfluous bluff land hold."""
    lethal_attacker = {
        "instance_id": 60,
        "name": "Giant Wurm",
        "type_line": "Creature — Wurm",
        "oracle_text": "",
        "card_types": ["CardType_Creature"],
        "power": 20,
        "toughness": 20,
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "is_tapped": False,
        "turn_entered_battlefield": 2,
    }
    state = _bluff_land_state(n_lands=6, extra_bf=[lethal_attacker])
    dec = _bluff_land_decision()
    planner = ActionPlanner(_FailBackend())
    assert planner.plan_decision_options(dec, state) == ["pass"]


def test_s3_2_normal_entry_point_holds_on_benign_and_apnap_mixed_stack_but_not_non_benign():
    """Minor #2 & #4: normal entry point passes on benign/APNAP stacks; non-benign stack calls LLM."""
    benign = {
        "instance_id": 300,
        "name": "Consul Watch ability",
        "object_kind": "ABILITY",
        "oracle_text": "Whenever a creature an opponent controls enters, you gain 1 life.",
        "controller_seat_id": 2,
        "owner_seat_id": 2,
    }
    own_etb = {
        "instance_id": 301,
        "name": "Spark Adept ability",
        "object_kind": "ABILITY",
        "oracle_text": "When Spark Adept enters, scry 1.",
        "controller_seat_id": 1,
        "owner_seat_id": 1,
    }
    non_benign = {
        "instance_id": 302,
        "name": "Dark Removal",
        "object_kind": "CARD",
        "oracle_text": "Destroy target creature.",
        "controller_seat_id": 2,
        "owner_seat_id": 2,
    }
    opt = DecisionOption(
        "idx:0", "Cast Cinder Bolt", meta={"actionType": "ActionType_Cast", "instanceId": 201}
    )
    dec = PendingDecision((1, 2), "ActionsAvailable", (opt, DecisionOption("pass", "Pass")))

    # Pure benign opposing trigger -> deterministic pass without LLM
    assert ActionPlanner(_FailBackend()).plan_decision_options(dec, _benign_trigger_state([benign])) == [
        "pass"
    ]
    # APNAP mixed stack (our ETB on bottom + opposing benign trigger on top) -> deterministic pass
    assert ActionPlanner(_FailBackend()).plan_decision_options(
        dec, _benign_trigger_state([own_etb, benign])
    ) == ["pass"]
    # Control: non-benign opposing removal spell -> does NOT auto-hold; asks LLM
    backend = _CaptureBackend(json.dumps({"option_ids": ["idx:0"], "reasoning": "Respond."}))
    assert ActionPlanner(backend).plan_decision_options(dec, _benign_trigger_state([non_benign])) == ["idx:0"]
    assert len(backend.calls) == 1


def test_s3_3_declare_attack_and_blocked_attacker_and_non_creature_controls():
    """Major #2 & Minor #3: pre-block uses [ATTACKING], blocked uses [BLOCKED ATTACKER], non-creatures untagged."""
    attacker = {
        "instance_id": 401,
        "name": "Ember Scout",
        "type_line": "Creature — Lizard",
        "oracle_text": "{1}{R}: Scry 1.",
        "card_types": ["CardType_Creature"],
        "power": 1,
        "toughness": 2,
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "is_tapped": True,
        "is_attacking": True,
        "is_blocking": False,
        "blocked_by": [],
        "turn_entered_battlefield": 1,
    }
    artifact = {
        "instance_id": 403,
        "name": "Mana Relic",
        "type_line": "Artifact",
        "oracle_text": "{2}, {T}: Scry 1.",
        "card_types": ["CardType_Artifact"],
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "is_tapped": False,
        "turn_entered_battlefield": 2,
    }
    base_state = {
        "local_seat_id": 1,
        "turn": {
            "turn_number": 5,
            "active_player": 1,
            "priority_player": 1,
            "phase": "Phase_Combat",
            "step": "Step_DeclareAttack",
        },
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 15, "lands_played": 1},
            {"seat_id": 2, "is_local": False, "life_total": 5, "lands_played": 0},
        ],
        "hand": [],
        "battlefield": [_plain_land(10), _plain_land(11), attacker, artifact],
        "stack": [],
        "graveyard": [],
        "exile": [],
    }
    dec = PendingDecision(
        (1, 4),
        "ActionsAvailable",
        (
            DecisionOption(
                "idx:0", "Activate: Ember Claw", meta={"actionType": "ActionType_Activate", "instanceId": 401}
            ),
            DecisionOption(
                "idx:1", "Activate: Mana Relic", meta={"actionType": "ActionType_Activate", "instanceId": 403}
            ),
            DecisionOption("pass", "Pass"),
        ),
    )
    # 1. Pre-block (Step_DeclareAttack): [ATTACKING], NOT [UNBLOCKED ATTACKER]; artifact has no [NOT ATTACKING]
    backend = _CaptureBackend(json.dumps({"option_ids": ["pass"], "reasoning": "Wait for blocks."}))
    ActionPlanner(backend).plan_decision_options(dec, base_state)
    prompt = backend.calls[0]["user"]
    assert any(
        "idx:0" in line and "[ATTACKING]" in line and "[UNBLOCKED ATTACKER]" not in line
        for line in prompt.splitlines()
    )
    assert all(not ("idx:1" in line and "[NOT ATTACKING]" in line) for line in prompt.splitlines())

    # 2. Post-block (Step_DeclareBlock) with blocked_by -> [BLOCKED ATTACKER]
    attacker["blocked_by"] = [999]
    base_state["turn"]["step"] = "Step_DeclareBlock"
    backend2 = _CaptureBackend(json.dumps({"option_ids": ["pass"], "reasoning": "Blocked."}))
    ActionPlanner(backend2).plan_decision_options(dec, base_state)
    prompt2 = backend2.calls[0]["user"]
    assert any("idx:0" in line and "[BLOCKED ATTACKER]" in line for line in prompt2.splitlines())
