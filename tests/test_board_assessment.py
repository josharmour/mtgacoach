"""Deterministic board assessment on real 2026-10-06 FRA board states.

Field report: the autopilot was at 4 life vs 20 by its seventh turn while
casting a mana rock, landcycling its only castable creature and drawing cards
(standalone.log 15:41-15:45). These tests pin the facts the strategic layer
must see on those turns (states in tests/strategic_states.py, replayed from
Player.log).
"""

from __future__ import annotations

import time
from copy import deepcopy

from tests.strategic_states import (
    CARDS,
    G1_T8,
    G1_T10,
    G1_T12,
    G1_T12_AFTER_VOLUME,
    G1_T12_MENU,
    G1_T14,
    G1_T15_FROM_OPPONENT,
    actions_decision,
    activate,
    after_land_drop,
    card,
    cast,
    play,
)

from arenamcp import board_assessment as ba
from arenamcp.board_assessment import (
    ROLE_AGGRESSOR,
    ROLE_CONTROL,
    ROLE_DEFENDER,
    assess,
    card_role,
    option_role,
    removal_reach,
    role_guard,
)


def _fresh(state):
    ba._CACHE.clear()
    return assess(deepcopy(state))


# --- role, clocks and flags on the real turns ---------------------------------


def test_t8_empty_board_vs_trampler_and_token_is_defender_on_a_four_turn_clock():
    a = _fresh(G1_T8)
    assert a.role == ROLE_DEFENDER
    assert a.their_clock == 4 and a.our_clock is None
    assert a.race == "behind"
    assert a.survival_mode
    # Three lands, no land in hand: the knapsack deploys the best blocker.
    assert a.lookahead[0].casts == ["Theorix Metamage"]
    assert a.lookahead[0].mana == 3


def test_t10_far_behind_is_defender_with_a_three_turn_clock_and_lands_theorix():
    a = _fresh(G1_T10)
    assert a.role == ROLE_DEFENDER
    assert a.their_clock == 3
    assert a.race == "behind"
    assert a.lookahead[0].land == "Forest"
    assert a.lookahead[0].casts == ["Theorix Metamage"]
    assert [step.turn for step in a.lookahead] == [10, 12, 14]


def test_t12_two_turn_clock_and_lookahead_casts_witness_after_the_land_drop():
    a = _fresh(G1_T12)
    assert a.role == ROLE_DEFENDER
    assert a.their_clock == 2 and a.our_clock is None
    assert a.opp_lethal_on_board is False
    # Island + Undulating Witness (3/5 flyer) blocks the trampler: 11 -> 7.
    t = a.lookahead[0]
    assert (t.land, t.casts, t.mana) == ("Island", ["Undulating Witness"], 5)
    assert t.life_after == 7
    assert a.dead_in is None
    assert "Undulating Witness" in t.castable and "Murmuring Volume" in t.castable
    assert any("castable plays stabilize" in flag for flag in a.flags)
    assert "Murmuring Volume" not in t.casts


def test_t12_after_the_mana_rock_is_dead_in_two_unless_we_stabilize():
    a = _fresh(G1_T12_AFTER_VOLUME)
    assert a.role == ROLE_CONTROL
    assert a.dead_in == 2
    assert "DEAD IN 2 TURNS UNLESS WE STABILIZE" in a.flags
    assert a.lookahead[0].casts == []  # Witness no longer castable this turn
    assert "dead in 2" in a.role_reason


def test_t14_opponent_has_lethal_on_board():
    a = _fresh(G1_T14)
    assert a.role == ROLE_CONTROL
    assert a.opp_lethal_on_board and a.their_clock == 1
    assert any(flag.startswith("OPPONENT HAS LETHAL ON BOARD") for flag in a.flags)
    assert a.dead_in == 1
    # Splinter Twin's hasty copies are named as an engine threat, not counted.
    assert any(threat.name == "Splinter Twin" for threat in a.threats)
    assert any("token/copy engines" in item for item in a.unknowns)


def test_lethal_on_board_is_aggressor_and_never_survival_mode():
    a = _fresh(G1_T15_FROM_OPPONENT)
    assert a.lethal_now
    assert a.role == ROLE_AGGRESSOR
    assert not a.survival_mode
    assert "LETHAL AVAILABLE NOW" in a.flags[0]
    assert "ATTACK FOR LETHAL" in a.prompt_block()


def test_prompt_block_leads_with_role_then_this_turn_then_facts():
    lines = _fresh(G1_T12).prompt_block().splitlines()
    assert lines[0].startswith("STRATEGIC ROLE") and "DEFENDER" in lines[0]
    assert lines[1].startswith("  THIS TURN (T12, now): play Island; cast Undulating Witness")
    assert lines[2].startswith("  FACTS: they kill us in 2 attack(s)")
    assert lines[3].startswith("  NEXT: T14:")
    assert any(line.startswith("  Priority: survive first") for line in lines)
    assert "GAME PLAN:" not in "\n".join(lines)


def test_assessment_is_fast_on_real_and_crowded_boards():
    for state in (G1_T8, G1_T10, G1_T12, G1_T14, G1_T15_FROM_OPPONENT):
        assert _fresh(state).elapsed_ms < 50
    crowded = deepcopy(G1_T14)
    for index in range(7):
        crowded["battlefield"].append(
            card(900 + index, "Cadet", 2, is_tapped=False, turn_entered_battlefield=13)
        )
        crowded["battlefield"].append(
            card(950 + index, "Theorix Metamage", 1, is_tapped=False, turn_entered_battlefield=13)
        )
    started = time.perf_counter()
    assert _fresh(crowded) is not None
    assert (time.perf_counter() - started) * 1000 < 250


def test_unknown_power_and_seats_stay_unknown():
    state = deepcopy(G1_T12)
    state["battlefield"].append(card(990, "Cadet", 2, power=None, toughness=None, is_tapped=False))
    a = _fresh(state)
    assert any("unknown power/toughness" in item for item in a.unknowns)
    assert ba.assess({"players": [], "turn": {"turn_number": 3}}) is None


def test_evasion_counts_flying_and_tetsuko_unblockability():
    state = deepcopy(G1_T12)
    # Give us Tetsuko and a 1/1: both attack past four untapped blockers.
    state["battlefield"].append(
        card(991, "Tetsuko Umezawa, Fugitive", 1, is_tapped=False, turn_entered_battlefield=10)
    )
    state["battlefield"].append(
        card(992, "Fblthp, Impossibly Lost", 1, is_tapped=False, turn_entered_battlefield=10)
    )
    for permanent in state["battlefield"]:
        if permanent["controller_seat_id"] == 2:
            permanent["is_tapped"] = False
    a = _fresh(state)
    # Tetsuko (power 1) and Fblthp (1/1) connect for 2 a turn vs 20 life.
    assert a.our_clock == 10
    for permanent in state["battlefield"]:
        if permanent["name"].startswith("Tetsuko"):
            permanent["oracle_text"] = ""
    # Without the grant, four untapped blockers stop both: no clock at all.
    assert _fresh(state).our_clock is None


# --- card and option roles -------------------------------------------------------


def test_card_roles_from_oracle_text():
    roles = {name: card_role(card(1, name, 1)) for name in CARDS}
    assert roles["Murmuring Volume"] == "ramp"
    assert roles["Twinned Vision"] == "draw"
    assert roles["Unsummon"] == "bounce"
    assert roles["Countersculpt"] == "counter"
    assert roles["Fulminous Forte"] == "removal"
    assert roles["Archive Arbiter"] == "creature"  # body first, ETB second
    assert roles["Undulating Witness"] == "creature"
    assert roles["Island"] == "land"
    assert removal_reach(card(1, "Fulminous Forte", 1)) == ("damage", 5)  # strongest mode
    # Noncreature-only removal does not kill creatures.
    assert (
        removal_reach({"name": "X", "oracle_text": "Destroy target noncreature, nonland permanent."}) is None
    )


def test_option_roles_for_the_real_t12_menu():
    decision = actions_decision(G1_T12_MENU)
    roles = {o.option_id: option_role(o, G1_T12) for o in decision.options}
    assert roles == {"idx:1": "ramp", "idx:4": "cycling", "idx:5": "land", "pass": "pass"}


# --- role guard ------------------------------------------------------------------


def test_guard_plays_the_land_that_enables_witness_instead_of_the_mana_rock():
    decision = actions_decision(G1_T12_MENU)
    verdict = role_guard(_fresh(G1_T12), decision, "idx:1", G1_T12)
    assert verdict is not None and verdict.option_id == "idx:5"
    assert verdict.reason.startswith("Role guard: defender")
    assert "Undulating Witness" in verdict.reason and "Murmuring Volume" in verdict.reason
    assert "14 -> 6" in verdict.reason


def test_guard_casts_the_creature_over_the_rock_after_the_land_drop():
    state = after_land_drop(G1_T12, 284)
    decision = actions_decision(
        [
            ("idx:0", "Cast Undulating Witness", cast(229, 106272), True),
            ("idx:1", "Cast Murmuring Volume", cast(217, 106419), True),
            ("idx:4", "Activate: Undulating Witness [from hand: Basic landcycling {2}]", activate(229), True),
            ("pass", "Pass", None, None),
        ]
    )
    for chosen in ("idx:1", "idx:4"):  # the rock, and cycling the creature away
        verdict = role_guard(_fresh(state), decision, chosen, state)
        assert verdict is not None and verdict.option_id == "idx:0", chosen


def test_guard_never_overrides_board_plays_lands_or_passes():
    state = after_land_drop(G1_T12, 284)
    decision = actions_decision(
        [
            ("idx:0", "Cast Undulating Witness", cast(229, 106272), True),
            ("idx:1", "Cast Murmuring Volume", cast(217, 106419), True),
            ("pass", "Pass", None, None),
        ]
    )
    a = _fresh(state)
    assert role_guard(a, decision, "idx:0", state) is None
    assert role_guard(a, decision, "pass", state) is None
    assert role_guard(_fresh(G1_T12), actions_decision(G1_T12_MENU), "idx:5", G1_T12) is None


def test_guard_needs_a_castable_improvement():
    # After the rock: only cycling/draw/pass remain, nothing improves survival.
    decision = actions_decision(
        [
            ("idx:1", "Cast Twinned Vision", cast(218, 106399), True),
            ("idx:3", "Activate: Undulating Witness [from hand: Basic landcycling {2}]", activate(229), True),
            ("pass", "Pass", None, None),
        ]
    )
    state = after_land_drop(G1_T12_AFTER_VOLUME, 284)
    assert role_guard(_fresh(state), decision, "idx:3", state) is None
    # An unpayable creature is not an alternative.
    decision = actions_decision(
        [
            ("idx:0", "Cast Undulating Witness", cast(229, 106272), False),
            ("idx:1", "Cast Murmuring Volume", cast(217, 106419), True),
        ]
    )
    state = after_land_drop(G1_T12, 284)
    assert role_guard(_fresh(state), decision, "idx:1", state) is None


def test_guard_never_blocks_our_lethal():
    state = deepcopy(G1_T15_FROM_OPPONENT)
    state["hand"] = [card(801, "Twinned Vision", 2), card(802, "Theorix Metamage", 2)]
    decision = actions_decision(
        [
            ("idx:0", "Cast Twinned Vision", cast(801), True),
            ("idx:1", "Cast Theorix Metamage", cast(802), True),
        ]
    )
    assert role_guard(_fresh(state), decision, "idx:0", state) is None


def test_guard_stays_quiet_when_not_under_pressure():
    # Early game, empty boards: drawing is not "behind on board".
    state = deepcopy(G1_T8)
    state["battlefield"] = [c for c in state["battlefield"] if "Creature" not in c["type_line"]]
    decision = actions_decision(
        [
            ("idx:0", "Cast Murmuring Volume", cast(217), True),
            ("idx:1", "Cast Theorix Metamage", cast(125), True),
        ]
    )
    a = _fresh(state)
    assert not a.survival_mode
    assert role_guard(a, decision, "idx:0", state) is None


def test_payload_is_json_safe_for_the_ui():
    import json

    payload = _fresh(G1_T12).as_payload()
    json.dumps(payload)
    assert payload["role"] == ROLE_DEFENDER and payload["their_clock"] == 2
    assert payload["lookahead"][0] == {
        "turn": 12,
        "label": "T",
        "casts": ["Undulating Witness"],
        "life_after": 7,
    }


def test_play_and_cast_helpers_build_real_option_meta():
    assert play(284)["actionType"] == "ActionType_Play"
    assert cast(229)["instanceId"] == 229


def test_log_snapshot_counts_hidden_opponent_hand_cards():
    """Card advantage needs the opponent's hand size.

    The opponent's hand ids arrive as zone membership only (no GameObjects),
    so counting objects reported 0 all game (2026-10-06 replay: 6, 5, ... 1).
    """
    from arenamcp.gamestate import GameState, create_game_state_handler

    game = GameState()
    game.local_seat_id = 1
    handler = create_game_state_handler(game)
    handler(
        {
            "greToClientEvent": {
                "greToClientMessages": [
                    {
                        "type": "GREMessageType_GameStateMessage",
                        "gameStateMessage": {
                            "type": "GameStateType_Full",
                            "turnInfo": {"turnNumber": 4, "activePlayer": 1, "phase": "Phase_Main1"},
                            "players": [
                                {"systemSeatNumber": 1, "lifeTotal": 20},
                                {"systemSeatNumber": 2, "lifeTotal": 20},
                            ],
                            "zones": [
                                {
                                    "zoneId": 31,
                                    "type": "ZoneType_Hand",
                                    "ownerSeatId": 1,
                                    "objectInstanceIds": [119],
                                },
                                {
                                    "zoneId": 35,
                                    "type": "ZoneType_Hand",
                                    "ownerSeatId": 2,
                                    "visibility": "Visibility_Private",
                                    "objectInstanceIds": [165, 162, 161, 159],
                                },
                            ],
                            "gameObjects": [
                                {
                                    "instanceId": 119,
                                    "grpId": 106529,
                                    "zoneId": 31,
                                    "ownerSeatId": 1,
                                    "cardTypes": ["CardType_Land"],
                                }
                            ],
                        },
                    }
                ]
            }
        }
    )
    snapshot = game.get_published_snapshot()
    assert snapshot["zones"]["opponent_hand_count"] == 4
