"""Deterministic board assessment on real 2026-10-06 FRA board states.

Field report: the autopilot was at 4 life vs 20 by its seventh turn while
casting a mana rock, landcycling its only castable creature and drawing cards
(standalone.log 15:41-15:45). These tests pin the facts the strategic layer
must see on those turns (states in tests/strategic_states.py, replayed from
Player.log).
"""

from __future__ import annotations

import dataclasses
import time
from copy import deepcopy

import pytest
from tests.strategic_states import (
    BUG_135027,
    BUG_174855,
    BUG_180436,
    CARDS,
    G1_T8,
    G1_T10,
    G1_T12,
    G1_T12_AFTER_VOLUME,
    G1_T12_MENU,
    G1_T14,
    G1_T14_MODE_STATE,
    G1_T14_ON_STACK,
    G1_T15_FROM_OPPONENT,
    G3_T10_BLOCKS,
    actions_decision,
    activate,
    after_land_drop,
    card,
    cast,
    log_phase,
    mac_phase,
    play,
)

from arenamcp import board_assessment as ba
from arenamcp.board_assessment import (
    ROLE_AGGRESSOR,
    ROLE_CONTROL,
    ROLE_DEFENDER,
    ROLE_RACE,
    assess,
    card_role,
    option_role,
    removal_reach,
    role_guard,
)


def _fresh(state):
    """A cold assessment. The line search is reproducible unless a wall-clock
    deadline cut it short (machine load, a GC pause): then it is run again."""
    for _attempt in range(3):
        ba._CACHE.clear()
        result = assess(deepcopy(state))
        stats = getattr(result, "search_stats", None) or {}
        if not (stats.get("bounded") or stats.get("truncated")):
            break
    return result


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


def test_t12_after_the_mana_rock_only_the_lifegain_line_survives():
    # The greedy projection (Witness at T14) is dead in 2; the line search
    # finds the line that lives: Archive Arbiter gaining 4 at T14. Landcycling
    # the Witness first also lives, so it is the best survivor, not the only one.
    a = _fresh(G1_T12_AFTER_VOLUME)
    assert a.role == ROLE_CONTROL
    assert a.dead_in_greedy == 2
    assert a.dead_in is None or a.dead_in >= 3
    assert not any(flag.startswith("ONLY SURVIVING LINE") for flag in a.flags)
    (only,) = [flag for flag in a.flags if flag.startswith("GREEDY LINE DIES; BEST SURVIVING LINE")]
    assert "Archive Arbiter" in only and "gain 4 life" in only
    assert "DEAD IN 2 TURNS UNLESS WE STABILIZE" not in a.flags
    assert a.lookahead[0].casts == []  # Witness no longer castable this turn
    assert a.lookahead[1].casts == ["Archive Arbiter"]
    assert a.lookahead[1].modes == {"Archive Arbiter": "gain 4 life"}
    assert "best surviving line" in a.role_reason and "only line" not in a.role_reason
    assert "cast Archive Arbiter (choose: gain 4 life)" in a.suggestion(1)


def test_t14_opponent_has_lethal_on_board():
    a = _fresh(G1_T14)
    assert a.role == ROLE_CONTROL
    assert a.opp_lethal_on_board and a.their_clock == 1
    assert any(flag.startswith("OPPONENT HAS LETHAL ON BOARD") for flag in a.flags)
    # Arbiter's "gain 4 life" mode survives their next attack (the greedy line
    # dies to it): dead in 2, not 1, and no all-in.
    assert (a.dead_in, a.dead_in_greedy) == (2, 1)
    assert not a.all_in
    (only,) = [flag for flag in a.flags if flag.startswith("ONLY LINE THAT SURVIVES THEIR NEXT ATTACK")]
    assert "gain 4 life" in only
    assert "only line:" in a.role_reason
    assert a.lookahead[0].modes == {"Archive Arbiter": "gain 4 life"} and a.lookahead[0].life_after == 2
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
    assert a.posture == "lethal" and a.lookahead[0].posture == "lethal"
    assert a.lines[0].outcome == "win" and a.lines[0].win_turn == 15


def test_prompt_block_leads_with_role_then_this_turn_then_facts():
    lines = _fresh(G1_T12).prompt_block().splitlines()
    assert lines[0].startswith("STRATEGIC ROLE") and "DEFENDER" in lines[0]
    assert lines[1].startswith("  THIS TURN (T12, now): play Island; cast Undulating Witness")
    assert lines[2].startswith("  FACTS: they kill us in 2 attack(s)")
    assert lines[3].startswith("  NEXT: T14:")
    assert lines[4].startswith("  LINES") and len(lines[4]) <= 320
    assert any(line.startswith("  Priority: survive first") for line in lines)
    assert "GAME PLAN:" not in "\n".join(lines)


def test_planning_block_lists_the_candidate_lines():
    a = _fresh(G1_T12)
    block = a.planning_block()
    assert "CANDIDATE LINES (2-turn search + greedy third turn;" in block
    assert "Prefer one of these lines; a deviation needs a concrete card or combat reason." in block
    lines = block.splitlines()
    start = next(n for n, line in enumerate(lines) if line.startswith("CANDIDATE LINES"))
    assert lines[start + 1].startswith("  1. T12: Island + Undulating Witness -> life 7")
    assert 1 <= len(a.lines) <= 5


def test_the_line_search_names_landcycling_and_the_attack_posture():
    # BUG_135027: landcycle Witness now, play the fetched Island and cast later.
    t = _fresh(BUG_135027).lookahead
    assert t[0].cycles == ["Undulating Witness"] and t[1].land == "Island"
    assert "landcycle Undulating Witness" in _fresh(BUG_135027).suggestion(0)
    # BUG_174855: attacking with Seasoned Cryomancer beats holding it back.
    a = _fresh(BUG_174855)
    assert a.posture == "attack" and "Seasoned Cryomancer" in a.lookahead[0].attack
    assert "attack with Seasoned Cryomancer" in a.suggestion(0)
    assert a.role == ROLE_AGGRESSOR and "best line attacks for" in a.role_reason
    assert a.role_reason.endswith(a.posture_reason)


def test_a_lethal_line_next_turn_is_flagged_and_drives_the_role():
    # Our T15 after combat: their attack first, then ours kills on T17.
    state = deepcopy(G1_T15_FROM_OPPONENT)
    state["turn"].update(phase="Phase_Main2", step="")
    a = _fresh(state)
    assert not a.lethal_now and a.lethal_next_turn
    (flag,) = [flag for flag in a.flags if flag.startswith("LETHAL LINE: ")]
    assert flag.endswith("lethal on T17")
    assert a.role == ROLE_AGGRESSOR and a.role_reason.startswith("best line kills on T17")
    assert a.lookahead[1].attack and "attack with" in a.suggestion(1)


def _fastest_ms(state, times: int = 5) -> float:
    """The fastest of ``times`` cold assessments in CPU ms (``strategic_states.cpu_ms``)."""
    from tests.strategic_states import cpu_ms

    def run() -> None:
        ba._CACHE.clear()
        assert assess(deepcopy(state)) is not None

    return cpu_ms(run, times)


def test_assessment_is_fast_on_real_and_crowded_boards():
    for state in (G1_T8, G1_T10, G1_T12, G1_T14, G1_T15_FROM_OPPONENT):
        assert _fastest_ms(state) < 50
    crowded = deepcopy(G1_T14)
    for index in range(7):
        crowded["battlefield"].append(
            card(900 + index, "Cadet", 2, is_tapped=False, turn_entered_battlefield=13)
        )
        crowded["battlefield"].append(
            card(950 + index, "Theorix Metamage", 1, is_tapped=False, turn_entered_battlefield=13)
        )
    assert _fastest_ms(crowded) < 250


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
    assert 1 <= len(payload["lines"]) <= 3 and payload["lines"][0]["outcome"] == "alive"
    assert isinstance(payload["posture"], str)
    assert set(payload["search"]) >= {"nodes", "ms", "bounded", "truncated"}
    for name, source in ALL_NAMED.items():
        payload = _fresh(source).as_payload()
        json.dumps(payload)
        assert len(payload["lines"]) <= 3 and isinstance(payload["posture"], str), name


def test_play_and_cast_helpers_build_real_option_meta():
    assert play(284)["actionType"] == "ActionType_Play"
    assert cast(229)["instanceId"] == 229


def test_log_snapshot_counts_hidden_opponent_hand_cards(monkeypatch):
    """Card advantage needs the opponent's hand size.

    The opponent's hand ids arrive as zone membership only (no GameObjects),
    so counting objects reported 0 all game (2026-10-06 replay: 6, 5, ... 1).
    """
    from types import SimpleNamespace

    from arenamcp import card_db
    from arenamcp.gamestate import GameState, create_game_state_handler

    # No card names needed. The real database loads Scryfall's bulk data on a
    # background thread for seconds, which pushed later tests' line searches
    # past their wall-clock deadlines (gen-2 GC pauses up to 335 ms).
    nothing = SimpleNamespace(
        prewarm_cards=lambda ids: None,
        get_card_by_arena_id=lambda grp_id: None,
        get_ability_text=lambda grp_id: None,
    )
    monkeypatch.setattr(card_db, "_card_db", nothing)  # the singleton every caller gets

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


# --- bridge phase names (P0 phase fix) ----------------------------------------------
#
# The Mac bridge publishes CurrentPhase.ToString(): "Main1" with step "None",
# "Combat"/"DeclareBlock" (all twelve bug_20261006_*.json game states do);
# Player.log publishes "Phase_Main1"/"" and "Phase_Combat"/"Step_DeclareBlock".
# The assessment matched only the log's names, so on the Mac no attack was ever
# pending: no LETHAL AVAILABLE NOW, no life after the attack under way, and
# their pending attack was played after our untap step with our whole board.

LOG_NAMED = {
    "G1_T8": G1_T8,
    "G1_T10": G1_T10,
    "G1_T12": G1_T12,
    "G1_T12_AFTER_VOLUME": G1_T12_AFTER_VOLUME,
    "G1_T14": G1_T14,
    "G1_T14_ON_STACK": G1_T14_ON_STACK,
    "G1_T14_MODE_STATE": G1_T14_MODE_STATE,
    "G1_T15_FROM_OPPONENT": G1_T15_FROM_OPPONENT,
    "G3_T10_BLOCKS": G3_T10_BLOCKS,
}
BRIDGE_NAMED = {"BUG_135027": BUG_135027, "BUG_174855": BUG_174855, "BUG_180436": BUG_180436}
ALL_NAMED = {**LOG_NAMED, **BRIDGE_NAMED}

ALL_IN = (
    "ALL-IN: no defensive line survives their next attack — attack with everything; "
    "holding back blockers changes nothing"
)
DEAD_NEXT = "DEAD NEXT ATTACK even after our best castable plays"

# The greedy pipeline's facts, recorded before the phase fix: (role, lethal_now,
# all_in, dead_in, our_life_now_attack, flags, lookahead as (land, casts, mana,
# life_after)). The log-named rows must not move. The bridge-named rows are the
# report's log-named copy, i.e. the corrected timing: their Main1 attack is
# pending now. Before the fix those had no our_life_now_attack and played that
# attack after our turn (lookahead lives 17/14/11 and 14/14/14 for 135027 and
# 174855, -14 at T for 180436).
BASELINE = {
    "G1_T8": (
        ROLE_DEFENDER, False, False, None, None, [],
        [("", ["Theorix Metamage"], 3, 17), ("", ["Tetsuko Umezawa, Fugitive"], 3, 17), ("", ["Murmuring Volume"], 3, 17)],
    ),
    "G1_T10": (
        ROLE_DEFENDER, False, False, None, None, [],
        [("Forest", ["Theorix Metamage"], 4, 11), ("", ["Murmuring Volume"], 4, 10), ("", ["Undulating Witness"], 5, 10)],
    ),
    "G1_T12": (
        ROLE_DEFENDER, False, False, None, None, ["their board kills us in 2 but our castable plays stabilize"],
        [("Island", ["Undulating Witness"], 5, 7), ("", ["Murmuring Volume"], 5, 5), ("", ["Archive Arbiter"], 6, 5)],
    ),
    "G1_T12_AFTER_VOLUME": (
        ROLE_CONTROL, False, False, 2, None, ["DEAD IN 2 TURNS UNLESS WE STABILIZE"],
        [("Island", [], 3, 4), ("", ["Undulating Witness"], 6, -2), ("", ["Archive Arbiter"], 6, None)],
    ),
    "G1_T14": (
        ROLE_CONTROL, False, False, 1, None,
        ["OPPONENT HAS LETHAL ON BOARD (9 through our best blocks vs 4 life)", DEAD_NEXT],
        [("Island", ["Archive Arbiter"], 7, -4), ("Island", [], 8, None), ("", [], 8, None)],
    ),
    "G1_T14_ON_STACK": (
        ROLE_CONTROL, False, False, 1, None,
        ["OPPONENT HAS LETHAL ON BOARD (9 through our best blocks vs 4 life)", DEAD_NEXT],
        [("Island", [], 1, -5), ("Island", [], 8, None), ("", [], 8, None)],
    ),
    "G1_T14_MODE_STATE": (
        ROLE_AGGRESSOR, False, True, 1, None,
        ["OPPONENT HAS LETHAL ON BOARD (8 through our best blocks vs 4 life)", DEAD_NEXT, ALL_IN],
        [("Island", [], 1, -4), ("Island", [], 8, None), ("", [], 8, None)],
    ),
    "G1_T15_FROM_OPPONENT": (
        ROLE_AGGRESSOR, True, False, None, None, ["LETHAL AVAILABLE NOW (6 through their blocks vs 4 life)"],
        [("", [], 5, 16), ("", [], 5, 16), ("", [], 5, 12)],
    ),
    "G3_T10_BLOCKS": (
        ROLE_DEFENDER, False, False, None, 20, [],
        [("Island", [], 6, 16), ("Room of Refuge", [], 6, 12), ("", [], 7, 10)],
    ),
    "BUG_135027": (
        ROLE_DEFENDER, False, False, None, 17, [],
        [("", ["Tam's Resistance"], 2, 14), ("", [], 2, 11), ("", [], 2, 8)],
    ),
    "BUG_174855": (
        ROLE_RACE, False, False, None, 12, [],
        [("Room of Refuge", ["Divining Duelist"], 4, 12), ("", ["Theorix Metamage"], 5, 12), ("", ["Mindseeker Oculus"], 5, 12)],
    ),
    "BUG_180436": (
        ROLE_AGGRESSOR, False, True, 1, -14,
        ["OPPONENT HAS LETHAL ON BOARD (17 through our best blocks vs 3 life)", DEAD_NEXT, ALL_IN],
        [("Island", [], 11, None), ("", [], 11, None), ("", [], 11, None)],
    ),
}  # fmt: skip


def _facts(assessment) -> dict:
    """Every assessment field except the wall-clock timings (lines as their payloads)."""
    facts = {}
    for item in dataclasses.fields(assessment):
        value = getattr(assessment, item.name)
        if item.name in ("elapsed_ms", "line_search"):
            continue  # the search result holds the board model; its facts are below
        if item.name == "lines":
            value = [line.as_payload() for line in value]
        elif item.name == "search_stats":
            value = {key: v for key, v in value.items() if key != "ms"}
        elif item.name in ("threats", "lookahead"):
            value = [dataclasses.asdict(entry) for entry in value]
        facts[item.name] = value
    return facts


def _truncated_search(monkeypatch):
    """search_lines past its hard deadline from the first expansion."""
    from arenamcp import line_search

    original = line_search.search_lines

    def truncated(model, **kwargs):
        return original(model, **{**kwargs, "soft_ms": 0.0, "hard_ms": 0.0})

    monkeypatch.setattr(line_search, "search_lines", truncated)


def _raising_search(monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("line search exploded")

    monkeypatch.setattr("arenamcp.line_search.search_lines", broken)


def _broken_result(monkeypatch):
    """The search runs, but turning its best line into lookahead rows fails."""

    def broken(self):
        raise RuntimeError("projection exploded")

    monkeypatch.setattr("arenamcp.line_search.LineSearchResult.to_projections", broken)


SEARCH_FALLBACKS = {
    "switched-off": lambda monkeypatch: monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0"),
    "raising": _raising_search,
    "truncated": _truncated_search,
    "broken-result": _broken_result,
}
RESULT_KEPT = ("truncated", "broken-result")  # the search ran: kept for its stats only


# Our own modal choice waits on the stack in these rows (G1 T14 decision 18).
PENDING = {
    "G1_T14_ON_STACK": "Archive Arbiter (choose one)",
    "G1_T14_MODE_STATE": "Archive Arbiter trigger (choose one)",
}


@pytest.mark.parametrize("mode", list(SEARCH_FALLBACKS))
@pytest.mark.parametrize("name", list(BASELINE))
def test_fixture_facts_match_the_log_named_baseline(name, mode, monkeypatch):
    # The greedy pipeline these rows record: the kill switch, a failing search
    # and a truncated one all keep it (a dead-now search has no T step to cut).
    # One exception: the pending-choice check reads only the stack, so with the
    # search switched on it qualifies the facts even when the search failed or
    # was cut short (review 2026-10-07: a truncated search brought back ALL-IN
    # next to our pending gain-4-life mode).
    SEARCH_FALLBACKS[mode](monkeypatch)
    role, lethal_now, all_in, dead_in, now_life, flags, lookahead = BASELINE[name]
    note = f" — before our pending {PENDING.get(name)} resolves"
    if name in PENDING and mode != "switched-off":
        flags = [flag + note if flag == DEAD_NEXT else flag for flag in flags if flag != ALL_IN]
        role, all_in = ROLE_CONTROL, False
    a = _fresh(ALL_NAMED[name])
    if name in PENDING:
        assert a.role_reason.endswith(note) == (mode != "switched-off")
    assert (a.role, a.lethal_now, a.all_in, a.dead_in, a.our_life_now_attack) == (
        role,
        lethal_now,
        all_in,
        dead_in,
        now_life,
    )
    assert a.flags == flags
    assert [(p.land, p.casts, p.mana, p.life_after) for p in a.lookahead] == lookahead
    assert a.dead_in_greedy == dead_in
    if mode != "truncated" or not a.search_stats.get("truncated"):
        return
    assert a.line_search is not None and a.lines == [] and a.posture == ""
    assert "LINES" not in a.prompt_block() and "CANDIDATE LINES" not in a.planning_block()


@pytest.mark.parametrize("mode", list(SEARCH_FALLBACKS))
def test_fallbacks_leave_no_search_facts(mode, monkeypatch):
    SEARCH_FALLBACKS[mode](monkeypatch)
    a = _fresh(G1_T12)
    assert a.lines == [] and a.posture == "" and a.posture_reason == ""
    assert all(not (p.attack or p.modes or p.cycles or p.posture) for p in a.lookahead)
    payload = a.as_payload()
    assert payload["lines"] == [] and payload["posture"] == ""
    assert (a.line_search is None) == (mode not in RESULT_KEPT)
    assert payload["search"] == (a.line_search.stats() if mode in RESULT_KEPT else {})
    assert "LINES" not in a.prompt_block() and "CANDIDATE LINES" not in a.planning_block()


@pytest.mark.parametrize("mode", [None, *SEARCH_FALLBACKS])
def test_all_in_boards_keep_their_verdict_in_every_mode(mode, monkeypatch):
    from tests.test_all_in import _state

    if mode is not None:
        SEARCH_FALLBACKS[mode](monkeypatch)
    dead = _fresh(_state(3))
    assert dead.all_in and dead.role == ROLE_AGGRESSOR
    assert any(flag.startswith("ALL-IN") for flag in dead.flags)
    assert not _fresh(_state(20)).all_in
    if mode is None:
        # Every T step dies to their next attack, but seven attackers into six
        # blockers is past the solver's exhaustive block search: the first
        # attack is not exact, so the verdict comes from the greedy rule.
        assert dead.line_search.all_dead_at_first and not dead.line_search.exact_first_attack
        assert dead.dead_in_greedy == 1


def test_an_exact_search_that_finds_a_survivor_is_not_all_in(monkeypatch):
    # G1_T14 plus a 1/1 of ours: greedy casts Arbiter without its mode and is
    # dead next attack (all-in: attack with the 1/1). The exact search finds
    # Arbiter's "gain 4 life" line that survives: no all-in.
    state = deepcopy(G1_T14)
    state["battlefield"].append(card(990, "Cadet", 1, is_tapped=False, turn_entered_battlefield=10))
    a = _fresh(state)
    assert a.line_search.exact_first_attack and not a.line_search.all_dead_at_first
    assert a.dead_in_greedy == 1 and a.opp_lethal_on_board and a.our_power > 0
    assert not a.all_in and a.role == ROLE_CONTROL
    assert any(flag.startswith("ONLY SURVIVING LINE") and "gain 4 life" in flag for flag in a.flags)
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    legacy = _fresh(state)
    assert legacy.all_in and legacy.role == ROLE_AGGRESSOR


def test_the_kill_switch_never_serves_the_other_pipeline_from_the_cache(monkeypatch):
    ba._CACHE.clear()
    state = deepcopy(G1_T12_AFTER_VOLUME)
    assert assess(state).dead_in is None
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    assert assess(state).dead_in == 2
    monkeypatch.delenv("ARENAMCP_LINE_SEARCH")
    assert assess(state).dead_in is None


@pytest.mark.parametrize("name", list(ALL_NAMED))
def test_dead_in_with_the_search_never_comes_sooner_than_greedy(name):
    a = _fresh(ALL_NAMED[name])
    assert a.dead_in_greedy == BASELINE[name][3]
    if a.dead_in_greedy is not None:
        assert a.dead_in is None or a.dead_in >= a.dead_in_greedy
    assert not a.search_stats["truncated"] and not a.search_stats["bounded"]


@pytest.mark.parametrize("name", list(ALL_NAMED))
def test_mac_phase_names_match_log_phase_names(name):
    state = ALL_NAMED[name]
    bridge, log = _fresh(mac_phase(state)), _fresh(log_phase(state))
    # BoardAssessment.phase holds the log's name whichever spelling came in.
    assert bridge.phase == log.phase == log_phase(state)["turn"]["phase"]
    assert _facts(bridge) == _facts(log)


@pytest.mark.parametrize(
    ("phase", "step", "expected"),
    [
        ("Main1", "None", "Phase_Main1"),
        ("Combat", "DeclareBlock", "Phase_Combat"),
        ("Phase_Main2", "", "Phase_Main2"),
        ("None", "None", ""),
        ("", "", ""),
    ],
)
def test_assessment_phase_is_the_log_name(phase, step, expected):
    state = deepcopy(G1_T12)
    state["turn"].update(phase=phase, step=step)
    assert _fresh(state).phase == expected


@pytest.mark.parametrize(
    ("phase", "step", "lethal"),
    [
        ("Main1", "None", True),
        ("Beginning", "Upkeep", True),
        ("Combat", "BeginCombat", True),
        ("Combat", "DeclareAttack", True),
        ("Combat", "CombatDamage", False),  # our attack is over
        ("Main2", "None", False),
    ],
)
def test_bridge_names_see_our_lethal_attack(phase, step, lethal):
    state = deepcopy(G1_T15_FROM_OPPONENT)
    state["turn"].update(phase=phase, step=step)
    a = _fresh(state)
    assert a.lethal_now is lethal  # never True on bridge names before the fix
    assert _facts(a) == _facts(_fresh(log_phase(state)))
    if lethal:
        assert a.role == ROLE_AGGRESSOR and a.flags[0].startswith("LETHAL AVAILABLE NOW")
        assert "ATTACK FOR LETHAL" in a.prompt_block()


def _their_turn(phase: str, step: str, attackers: tuple[int, ...] = ()) -> dict:
    """G1's T15, the opponent's turn, from our seat: 4 life and an untapped
    Archive Arbiter vs five creatures; ``attackers`` are declared and tapped."""
    state = deepcopy(G1_T15_FROM_OPPONENT)
    state["local_seat_id"], state["opponent_seat_id"] = 1, 2
    for player in state["players"]:
        player["is_local"] = player["seat_id"] == 1
    state["turn"].update(phase=phase, step=step)
    for permanent in state["battlefield"]:
        if permanent["instance_id"] in attackers:
            permanent.update(is_attacking=True, is_tapped=True)
    return state


THEIR_ABLE = (280, 260, 238, 333)  # their untapped creatures that can attack


@pytest.mark.parametrize(
    ("bridge", "log", "attackers"),
    [
        (("Combat", "DeclareBlock"), ("Phase_Combat", "Step_DeclareBlock"), THEIR_ABLE),
        (("Main1", "None"), ("Phase_Main1", ""), ()),
    ],
)
def test_their_pending_attack_is_seen_under_bridge_names(bridge, log, attackers):
    a = _fresh(_their_turn(*bridge, attackers))
    assert not a.our_turn
    assert a.our_life_now_attack is not None  # None on bridge names before the fix
    assert a.opp_lethal_on_board and a.dead_in == 1
    assert _facts(a) == _facts(_fresh(_their_turn(*log, attackers)))


def test_only_their_declared_attackers_hit_us_this_combat(monkeypatch):
    # They declared the 2/2 Cadet alone: Archive Arbiter blocks and we stay at
    # 4; their whole board kills us next time. With the bridge names hidden,
    # every creature "attacked" after our untap: lethal on board, all-in.
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    a = _fresh(_their_turn("Combat", "DeclareBlock", (333,)))
    assert a.our_life_now_attack == 4
    assert a.their_clock == 2 and not a.opp_lethal_on_board
    assert a.dead_in == 2 and not a.all_in
    assert a.role == ROLE_CONTROL
    assert _facts(a) == _facts(_fresh(_their_turn("Phase_Combat", "Step_DeclareBlock", (333,))))


@pytest.mark.parametrize("name", list(BRIDGE_NAMED))
def test_bridge_named_bug_reports_count_their_pending_attack(name):
    a = _fresh(BRIDGE_NAMED[name])
    assert BRIDGE_NAMED[name]["turn"]["phase"] == "Main1"  # the report's own spelling
    assert a.phase == "Phase_Main1" and not a.our_turn
    assert a.our_life_now_attack is not None


# --- their first-strike damage step (review of the phase fix) -----------------------

FIRST_STRIKE_STEPS = [("Combat", "FirstStrikeDamage"), ("Phase_Combat", "Step_FirstStrikeDamage")]


@pytest.mark.parametrize(("phase", "step"), FIRST_STRIKE_STEPS)
def test_first_strike_damage_already_dealt_is_not_counted_again(phase, step, monkeypatch):
    # Their unblocked 3/3 flying first striker took us 6 -> 3 and our wall
    # blocks their 2/2: we end the combat at 3 and Giant Spider stabilises.
    # Counting the whole attack again read -2, DEAD NEXT ATTACK and ALL-IN,
    # and validate_plan forced a control plan into an all-in attack.
    from tests.test_board_model import _first_strike_board

    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    a = _fresh(_first_strike_board(phase, step))
    assert a.our_life_now_attack == 3
    assert a.dead_in != 1 and not a.all_in and not a.opp_lethal_on_board
    assert not any(flag.startswith(("DEAD NEXT ATTACK", "ALL-IN")) for flag in a.flags)
    assert a.role != ROLE_AGGRESSOR


@pytest.mark.parametrize(("phase", "step"), FIRST_STRIKE_STEPS)
def test_a_double_striker_hits_once_more_after_first_strike_damage(phase, step, monkeypatch):
    # Unblocked 3/3 double striker, its first-strike half already taken (10 -> 7).
    from tests.test_board_model import _first_strike_board

    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    state = _first_strike_board(phase, step, life=7, attackers=("Blade Knight",))
    state["battlefield"] = [c for c in state["battlefield"] if c["name"] != "Wall of Wood"]
    next(c for c in state["battlefield"] if c["name"] == "Llanowar Elves")["is_tapped"] = True
    assert _fresh(state).our_life_now_attack == 4  # was 1: double damage again
    state["turn"]["step"] = step.replace("FirstStrikeDamage", "DeclareBlock")
    assert _fresh(state).our_life_now_attack == 1  # before first-strike damage: both halves


def test_unseated_mac_board_gives_no_assessment():
    # bug_20261005_223955: with every seat missing, our Mindseeker Oculus was
    # counted as theirs (no blockers, '7 through', control/stabilize).
    from tests.test_board_model import _bug_223955

    assert _fresh(_bug_223955()) is None
    seated = _fresh(_bug_223955(seated=True))
    assert seated.our_creatures == 1 and seated.their_creatures == 4
    assert seated.our_life_now_attack == -3 and seated.all_in


# --- 2026-10-07 review regressions ------------------------------------------------------------


def _their_body(name: str, **extra):
    return ba._body(card(700, name, 2, is_tapped=False, turn_entered_battlefield=1, **extra), 12, {})


def _our_body(name: str, **extra):
    return ba._body(card(701, name, 1, is_tapped=False, turn_entered_battlefield=1, **extra), 12, {})


def test_removal_respects_target_restrictions():
    surgical, fate = card(1, "Surgical Precision", 1), card(2, "Your Fate Ends Here", 1)
    assert not ba._kills(surgical, _their_body("Heartstring Puller"))  # "toughness 4 or greater"; a 3/1
    assert ba._kills(surgical, _their_body("Archive Arbiter"))
    assert not ba._kills(fate, _their_body("Cadet"))  # "mana value 3 or greater"; a token is 0
    assert ba._kills(fate, _their_body("Heartstring Puller"))
    absence = card(3, "Extended Absence", 1)
    assert not ba._kills(absence, _their_body("Ruric Thar, Magecrusher", keywords=["hexproof"]))
    assert ba._kills(absence, _their_body("Ruric Thar, Magecrusher", keywords=["reach"]))
    # Ward {1}: only when the ward's mana is left over.
    unsummon, warded = card(4, "Unsummon", 1), _their_body("Unflinching Hortimancer")
    assert not ba._kills(unsummon, warded) and ba._kills(unsummon, warded, ward_mana=1)
    # A fight needs a creature of ours whose power reaches the target's toughness.
    prey = {**card(5, "Unsummon", 1), "name": "Prey Upon", "type_line": "Sorcery",
            "oracle_text": "Target creature you control fights target creature you don't control."}  # fmt: skip
    cadet = _their_body("Cadet")
    assert not ba._kills(prey, cadet) and not ba._kills(
        prey, cadet, ours=[_our_body("Fatehold Chronologist")]
    )
    assert ba._kills(prey, cadet, ours=[_our_body("Heartstring Puller")])
    # Colour words: Essence Burn hits black or green only.
    burn = card(6, "Essence Burn", 1)
    assert ba._kills(burn, _their_body("Cadet", colors=["Green"]))
    assert not ba._kills(burn, _their_body("Cadet", colors=["Blue"]))
    assert not ba._kills(burn, _their_body("Cadet"))  # colourless token
    # Attacking or blocking only: never proactively, fine during their attack.
    gate = {
        **card(7, "Unsummon", 1),
        "name": "Test Gate",
        "oracle_text": "Destroy target attacking creature.",
    }
    assert not ba._kills(gate, cadet) and ba._kills(gate, cadet, attacking=True)
    # "you control": our own creature is no removal target.
    assert (
        removal_reach({"name": "Blink", "oracle_text": "Exile target creature you control, then return it."})
        is None
    )


@pytest.mark.parametrize("name", ["Gideon's Memorial", "Identity Echo", "Way of the Warlord"])
def test_a_permanents_activated_or_granted_removal_is_not_removal(name):
    assert removal_reach(card(1, name, 1)) is None
    assert card_role(card(1, name, 1)) not in ("removal", "bounce", "ramp")
    # An enters trigger still is removal.
    rip = {**card(2, "Unsummon", 1), "name": "Test Rip", "type_line": "Enchantment", "card_types": ["CardType_Enchantment"],
           "oracle_text": "When this enchantment enters, exile target nonland permanent an opponent controls with mana "
           "value 2 or less until this enchantment leaves the battlefield."}  # fmt: skip
    assert card_role(rip) == "removal"
    assert not ba._kills(rip, _their_body("Heartstring Puller")) and ba._kills(
        rip, _their_body("Fatehold Chronologist")
    )


def test_mana_that_cant_cast_spells_from_hand_is_not_a_source():
    for name in ("Heartwood Crafter", "Gideon's Memorial"):
        assert ba._mana_source(card(1, name, 1, turn_entered_battlefield=1), 12) is None
    assert ba._mana_source(card(2, "Murmuring Volume", 1, turn_entered_battlefield=1), 12) is not None


def test_end_step_greedy_line_casts_only_instants_and_concede_agrees(monkeypatch):
    from arenamcp import concede

    state = deepcopy(G1_T8)
    state["turn"].update(phase="Phase_Ending", step="Step_End")
    state["hand"] = [c for c in state["hand"] if c["name"] != "Countersculpt"]
    state["players"][0]["life_total"] = 3
    state["action_history"] = []
    search = _fresh(state)
    # Nothing is castable at our End step: no land drop, no sorcery-speed creature.
    assert search.lookahead[0].casts == [] and search.lookahead[0].land == ""
    assert search.dead_in == search.dead_in_greedy == 1
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    legacy = _fresh(state)
    assert legacy.lookahead[0].casts == [] and legacy.dead_in == 1
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "1")
    with_search = concede.estimate_loss(deepcopy(state)).confidence
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    assert concede.estimate_loss(deepcopy(state)).confidence == with_search  # the search adds no offer


def test_the_exact_all_dead_verdict_holds_when_the_soft_budget_cut_t1_short(monkeypatch):
    # G1_T14 plus a 1/1 of ours: the exact root finds Arbiter's lifegain line. A
    # bounded T+1 (as under CPU load before the work budget) must not flip all_in.
    from arenamcp import line_search

    original = line_search._Search._expand

    def bounded(self, chosen, extra):
        original(self, chosen, extra)
        self.bounded = True

    monkeypatch.setattr(line_search._Search, "_expand", bounded)
    state = deepcopy(G1_T14)
    state["battlefield"].append(card(990, "Cadet", 1, is_tapped=False, turn_entered_battlefield=10))
    ba._CACHE.clear()
    a = assess(deepcopy(state))
    assert a.search_stats["bounded"] and a.line_search.exact_first_attack
    assert a.dead_in_greedy == 1 and a.opp_lethal_on_board
    assert not a.all_in and not any(flag.startswith("ALL-IN") for flag in a.flags)


def test_concurrent_assessments_share_one_search(monkeypatch):
    import threading

    from arenamcp import line_search

    calls, original = [], line_search.search_lines

    def slow(*args, **kwargs):
        calls.append(1)
        time.sleep(0.05)
        return original(*args, **kwargs)

    monkeypatch.setattr(line_search, "search_lines", slow)
    ba._CACHE.clear()
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(assess(deepcopy(G1_T12)))) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert len(calls) == 1 and len({id(r) for r in results}) == 1 and results[0] is not None
    assert not ba._INFLIGHT


def test_bridge_and_log_phase_names_share_one_cache_entry():
    ba._CACHE.clear()
    first = assess(deepcopy(log_phase(G1_T12)))
    assert ba._signature(mac_phase(G1_T12)) == ba._signature(log_phase(G1_T12))
    assert assess(deepcopy(mac_phase(G1_T12))) is first


# --- casts the search can't value and our own pending choice (review 2026-10-07) ----------------


def _with_hand(source: dict, *cards: tuple[int, str]) -> dict:
    state = deepcopy(source)
    local = state["local_seat_id"]
    state["hand"] += [card(iid, name, local) for iid, name in cards]
    return state


def test_a_haste_token_maker_is_the_lethal_line_not_grizzly_bears():
    from tests.strategic_states import HASTE_TOKENS_LETHAL

    # The search used to give the token maker no body: role CONTROL, "only line: Grizzly Bears".
    a = _fresh(HASTE_TOKENS_LETHAL)
    assert a.role == ROLE_AGGRESSOR and a.role_reason.startswith("best line kills on T10: Elemental Uprising")
    assert any(flag.startswith("LETHAL LINE: Elemental Uprising") for flag in a.flags)
    assert not any(flag.startswith("ONLY") for flag in a.flags) and "only line" not in a.role_reason
    assert a.posture == "lethal" and not a.all_in


def test_tokens_the_search_cannot_read_make_no_only_line_claim():
    from tests.strategic_states import HASTE_TOKENS_UNREAD

    # The same lethal tokens, exiled at end of turn: still unmodelled, so Grizzly Bears is the
    # best line the search can see, never the only one.
    a = _fresh(HASTE_TOKENS_UNREAD)
    assert not any(flag.startswith(("ONLY", "LETHAL LINE")) for flag in a.flags)
    (flag,) = [flag for flag in a.flags if flag.startswith("BEST MODELLED LINE")]
    assert flag.startswith("BEST MODELLED LINE THROUGH THEIR NEXT ATTACK (not modelled: Elemental Surge): ")
    assert "only line" not in a.role_reason
    assert (
        "best modelled line: Grizzly Bears" in a.role_reason
        and "(not modelled: Elemental Surge)" in a.role_reason
    )
    dead = [flag for flag in a.flags if flag.startswith("DEAD IN 2")]
    assert dead and dead[0].endswith("(not modelled: Elemental Surge)")


def test_an_enters_trigger_that_removes_their_flier_is_the_line():
    from tests.strategic_states import ENTERS_REMOVAL

    a = _fresh(ENTERS_REMOVAL)
    assert a.lookahead[0].casts == ["Chupacabra"] and a.dead_in is None
    assert a.lines[0].steps[0].targets == (("Chupacabra", "Sky Knight"),)


def test_an_unmodelled_castable_card_turns_greedy_dies_into_a_best_modelled_line():
    # G1 T12 after the rock: the greedy line dies and Arbiter's gain-4 line survives. A
    # castable Pacifism could also save us: no "only"/"surviving" claim, no forced CONTROL.
    plain = _fresh(G1_T12_AFTER_VOLUME)
    assert plain.role == ROLE_CONTROL and plain.role_reason.startswith("the greedy line dies")
    a = _fresh(_with_hand(G1_T12_AFTER_VOLUME, (990, "Pacifism")))
    (flag,) = [flag for flag in a.flags if flag.startswith("GREEDY LINE DIES")]
    assert flag.startswith("GREEDY LINE DIES; BEST MODELLED LINE (not modelled: Pacifism): ")
    assert "the greedy line dies" not in a.role_reason and a.role != ROLE_CONTROL


def test_an_unmodelled_castable_card_qualifies_the_only_surviving_line():
    state = _with_hand(G1_T14, (990, "Pacifism"))
    state["battlefield"].append(card(991, "Cadet", 1, is_tapped=False, turn_entered_battlefield=10))
    a = _fresh(state)
    assert a.line_search.only_survivor
    assert not any(flag.startswith("ONLY") for flag in a.flags)
    (flag,) = [flag for flag in a.flags if flag.startswith("BEST MODELLED LINE")]
    assert flag.startswith("BEST MODELLED LINE (the other modelled lines die; not modelled: Pacifism): ")
    assert "gain 4 life" in flag and "only line" not in a.role_reason


def test_a_lethal_line_stays_lethal_and_names_what_it_cannot_value():
    state = _with_hand(G1_T15_FROM_OPPONENT, (990, "Seismic Jolt"))
    state["turn"].update(phase="Phase_Main2", step="")
    a = _fresh(state)
    (flag,) = [flag for flag in a.flags if flag.startswith("LETHAL LINE: ")]
    assert flag.endswith("lethal on T17 (not modelled: Seismic Jolt)")
    # A win found without the trick stands: the role is still the line's.
    assert a.role == ROLE_AGGRESSOR and a.role_reason.startswith("best line kills on T17")


def test_the_unmodelled_checks_need_the_search(monkeypatch):
    from tests.strategic_states import HASTE_TOKENS_UNREAD

    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    a = _fresh(HASTE_TOKENS_UNREAD)
    assert not any("not modelled" in flag for flag in a.flags) and "not modelled" not in a.role_reason


@pytest.mark.parametrize(
    ("name", "pending"),
    [
        ("G1_T14_MODE_STATE", "Archive Arbiter trigger (choose one)"),
        ("G1_T14_ON_STACK", "Archive Arbiter (choose one)"),
    ],
)
def test_our_pending_modal_choice_qualifies_the_dead_facts_and_rules_out_all_in(name, pending):
    # G1 T14 decision 18: the strategy block said ALL-IN (no defensive line survives) while
    # Archive Arbiter's trigger waited for its mode; gaining 4 survives their T15 attack.
    a = _fresh(ALL_NAMED[name])
    note = f" — before our pending {pending} resolves"
    assert not a.all_in and not any(flag.startswith("ALL-IN") for flag in a.flags)
    assert f"DEAD NEXT ATTACK even after our best castable plays{note}" in a.flags
    assert a.role == ROLE_CONTROL and a.role_reason.endswith(note)
    assert "all-in" not in a.role_reason


def test_only_our_own_stack_objects_are_pending():
    from tests.strategic_states import SLOW_202111

    from arenamcp.board_model import build_board_model

    state = deepcopy(SLOW_202111)  # the opponent's +1/+1 counter trigger is on the stack
    assert ba._our_pending(state, build_board_model(state)) == []
    removal = deepcopy(G1_T14)
    removal["stack"] = [card(500, "Unsummon", 1), card(501, "Unsummon", 2)]
    assert ba._our_pending(removal, build_board_model(removal)) == ["Unsummon"]
    draw = deepcopy(G1_T14)
    draw["stack"] = [card(500, "Twinned Vision", 1)]
    assert ba._our_pending(draw, build_board_model(draw)) == []


def test_the_pending_choice_check_follows_the_kill_switch(monkeypatch):
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    a = _fresh(G1_T14_MODE_STATE)
    assert a.all_in and ALL_IN in a.flags  # today's greedy facts, as recorded


def test_the_prompt_block_can_leave_the_lines_to_the_decision_prompt():
    # A typed decision shows the LINES line above its options; the strategy block need not repeat it.
    a = _fresh(G1_T12)
    full, short = a.prompt_block(), a.prompt_block(with_lines=False)
    assert "\n  LINES (" in full and "LINES (" not in short
    assert short == "\n".join(line for line in full.splitlines() if not line.startswith("  LINES ("))
    assert "CANDIDATE LINES" in a.planning_block()


# --- second review 2026-10-07 --------------------------------------------------------------------


def _land_board(**kwargs):
    from tests.strategic_states import land_board

    return land_board(**kwargs)


def test_a_conditional_enters_trigger_makes_no_lethal_or_only_line_claim():
    # Vraska destroys only with six or more lands; at four the search called its line LETHAL on
    # T10 (a) and the ONLY SURVIVING LINE at 4 life (b).
    lands = ["Swamp", "Swamp", "Forest", "Forest"]
    hand = [(501, "Vraska, the Cutting Glare")]
    a = _fresh(
        _land_board(life=20, their_life=6, lands=lands, theirs=[(410, "Sky Knight", 2, False, 5)], hand=hand,
                    ours=[(420, "Hill Giant", 1, False, 3), (421, "Hill Giant", 1, False, 3)])
    )  # fmt: skip
    assert not any("Sky Knight" in flag or "lethal on T10" in flag for flag in a.flags)
    assert a.posture != "lethal" and "on Sky Knight" not in a.role_reason
    b = _fresh(
        _land_board(life=4, their_life=20, lands=lands, hand=hand,
                    theirs=[(410, "Sky Knight", 2, False, 5), (413, "Gray Ogre", 2, False, 7)])
    )  # fmt: skip
    assert not any(flag.startswith(("ONLY", "BEST MODELLED", "their board kills us")) for flag in b.flags)
    assert (
        "DEAD NEXT ATTACK even after our best modelled plays (not modelled: Vraska, the Cutting Glare)"
        in b.flags
    )
    assert "only line" not in b.role_reason


@pytest.mark.parametrize("name", ["Greenhouse Propagator", "Corpse Knight"])
def test_a_trigger_on_another_creature_entering_makes_no_claim(name):
    # Greenhouse Propagator: "ONLY LINE THAT SURVIVES THEIR NEXT ATTACK" on a phantom +1 life.
    # Corpse Knight from one life: "LETHAL LINE ... lethal on T10" and posture lethal.
    if name == "Corpse Knight":
        a = _fresh(
            _land_board(life=20, their_life=1, lands=["Plains", "Swamp"], theirs=[], hand=[(501, name)])
        )
        assert not any(flag.startswith("LETHAL LINE") and "T10" in flag for flag in a.flags)
        # Its 2/2 body attacks an empty board next turn: that is the line, and it is right.
        assert a.posture != "lethal" and a.role_reason.startswith("best line kills on T12")
        return
    a = _fresh(
        _land_board(life=4, their_life=20, lands=["Forest"] * 3, theirs=[(410, "Sky Knight", 2, False, 5)],
                    hand=[(501, name)])
    )  # fmt: skip
    assert not any(flag.startswith(("ONLY", "BEST MODELLED")) for flag in a.flags)
    assert (
        a.dead_in == 1
        and f"DEAD NEXT ATTACK even after our best modelled plays (not modelled: {name})" in a.flags
    )
    assert "only line" not in a.role_reason


def test_an_instant_fog_in_hand_or_on_our_stack_rules_out_all_in():
    # BUG_180436 (their T22, 3 life, four untapped U/B lands): a castable fog could be the
    # defensive line. ALL-IN said "whatever we do" next to "not modelled: Fog Wall".
    plain = _fresh(BUG_180436)
    assert plain.all_in and ALL_IN in plain.flags
    held = deepcopy(BUG_180436)
    held["hand"].append(card(990, "Fog Wall", 1))
    a = _fresh(held)
    assert not a.all_in and ALL_IN not in a.flags and a.role == ROLE_CONTROL
    assert "DEAD NEXT ATTACK even after our best modelled plays (not modelled: Fog Wall)" in a.flags
    cast_ = deepcopy(BUG_180436)
    cast_["stack"] = [card(990, "Fog Wall", 1)]
    from arenamcp.board_model import build_board_model

    assert ba._our_pending(cast_, build_board_model(cast_)) == ["Fog Wall"]
    b = _fresh(cast_)
    assert not b.all_in and b.role_reason.endswith(" — before our pending Fog Wall resolves")


def test_a_sorcery_speed_card_does_not_qualify_their_attack_now():
    # Their T22 Main1: an aura can't be cast before the attack that kills us.
    source = deepcopy(BUG_180436)
    source["hand"].append(card(990, "Arrest U", 1))
    a = _fresh(source)
    assert DEAD_NEXT in a.flags and a.all_in and ALL_IN in a.flags
    assert a.unmodelled == ["Arrest U"]  # castable on our T23: it still qualifies line claims


def test_a_card_flow_modal_on_our_stack_is_not_pending():
    source = deepcopy(BUG_180436)
    source["stack"] = [card(990, "Flow Charm", 1)]
    from arenamcp.board_model import build_board_model

    assert ba._our_pending(source, build_board_model(source)) == []
    a = _fresh(source)
    assert a.all_in and ALL_IN in a.flags and a.pending == []


@pytest.mark.parametrize("name", ["Stroke U", "Mind Twist"])
def test_an_x_card_flow_spell_qualifies_nothing(name):
    t14 = _fresh(_with_hand(G1_T14, (990, name)))
    assert any(flag.startswith("ONLY LINE THAT SURVIVES THEIR NEXT ATTACK: ") for flag in t14.flags)
    assert "only line:" in t14.role_reason and t14.unmodelled == []
    t12 = _fresh(_with_hand(G1_T12_AFTER_VOLUME, (990, name)))
    assert t12.role == ROLE_CONTROL and t12.role_reason.startswith("the greedy line dies")
    assert any(flag.startswith("GREEDY LINE DIES; BEST SURVIVING LINE") for flag in t12.flags)


def test_an_x_burn_spell_still_qualifies_the_claims():
    from tests.strategic_states import mountain_board

    source = mountain_board(
        life=3, their_life=20, mountains=4, theirs=[(410, "Hill Giant", 2, False, 5)],
        hand=[(501, "Volcanic Spray"), (502, "Grizzly Bears")],
    )  # fmt: skip
    a = _fresh(source)
    assert a.unmodelled == ["Volcanic Spray"]
    assert not any(flag.startswith("ONLY") for flag in a.flags)


def test_a_pump_with_nothing_to_pump_qualifies_nothing():
    # G1 T12 after the rock plus Seismic Jolt (+3/+0) and no creature of ours: the search
    # used to cast it ("Island + Seismic Jolt; then ...") and the caveat flipped CONTROL to DEFENDER.
    a = _fresh(_with_hand(G1_T12_AFTER_VOLUME, (990, "Seismic Jolt")))
    assert a.unmodelled == [] and a.role == ROLE_CONTROL
    (flag,) = [flag for flag in a.flags if flag.startswith("GREEDY LINE DIES")]
    assert flag.startswith(
        "GREEDY LINE DIES; BEST SURVIVING LINE: Island; then Archive Arbiter (gain 4 life)"
    )
    assert all("Seismic Jolt" not in row.casts for row in a.lookahead)


def test_a_surviving_searched_line_rules_out_all_in(monkeypatch):
    # bug_20260928_212848 with Hushbringer's static removed: the search is not exact, the
    # greedy line dies to their next attack, but the search's line wins (Vaultborn Tyrant's
    # life gain). ALL-IN ("whatever we do") sat next to that lethal line.
    from tests.strategic_states import BUG_212848

    source = deepcopy(BUG_212848)
    for entry in source["battlefield"]:
        if entry["name"] == "Hushbringer":
            entry["oracle_text"] = "Flying\nLifelink"
    monkeypatch.setattr(ba, "_unmodelled_castable", lambda *_args: ([], []))  # nothing else in the way
    a = _fresh(source)
    assert not a.line_search.exact_first_attack and a.dead_in_greedy == 1 and a.dead_in is None
    assert not a.all_in and ALL_IN not in a.flags
    assert a.role == ROLE_AGGRESSOR and a.role_reason.startswith("best line kills on T22")


def test_the_recorded_212848_board_is_dead_without_contradictions():
    from tests.strategic_states import BUG_212848

    a = _fresh(BUG_212848)  # Hushbringer: Vaultborn Tyrant's life gain never happens
    assert a.dead_in == 1 and not any("stabilize" in flag or "survives" in flag for flag in a.flags)
    assert "DEAD NEXT ATTACK even after our best modelled plays (not modelled: Vaultborn Tyrant)" in a.flags
    assert all("survives" not in line.summary() for line in a.lines)


def test_the_prompts_mark_unmodelled_casts_and_the_pending_choice():
    lands = ["Swamp", "Swamp", "Forest", "Forest"]
    a = _fresh(
        _land_board(life=20, their_life=6, lands=lands, theirs=[(410, "Sky Knight", 2, False, 5)],
                    hand=[(501, "Vraska, the Cutting Glare")],
                    ours=[(420, "Hill Giant", 1, False, 3), (421, "Hill Giant", 1, False, 3)])
    )  # fmt: skip
    block, plan = a.prompt_block(), a.planning_block()
    lines = next(line for line in block.splitlines() if line.startswith("  LINES ("))
    assert "(Vraska, the Cutting Glare not modelled)" in lines
    this_turn = next(line for line in block.splitlines() if line.startswith("  THIS TURN"))
    assert this_turn.endswith(" (best modelled)")
    assert "T10: Vraska, the Cutting Glare (not modelled)" in plan
    pending = _fresh(G1_T14_MODE_STATE)
    lines = next(line for line in pending.prompt_block().splitlines() if line.startswith("  LINES ("))
    assert lines.endswith(" — before our pending Archive Arbiter trigger (choose one) resolves")
    assert (
        "(These lines read the board before our pending Archive Arbiter trigger (choose one) resolves.)"
        in (pending.planning_block())
    )
    # No caveat, no marks.
    clean = _fresh(G1_T12)
    assert "not modelled)" not in clean.prompt_block().split("their new cards/tricks not modelled)")[-1]
    assert "(best modelled)" not in clean.prompt_block()


# --- commanders in the command zone (Brawl, 2026-10-07) ----------------------------------------

_COMMANDER_LINE = (
    "COMMANDER (command zone: cast it like a hand card; each cast from there adds {2} to the next): "
)


def _commander_line(a) -> str:
    return next(line.strip() for line in a.prompt_block().splitlines() if "COMMANDER (" in line)


def test_our_commander_castable_now_is_the_only_surviving_line():
    from tests.strategic_states import BRAWL_COMMANDER_SAVES

    a = _fresh(BRAWL_COMMANDER_SAVES)
    assert "ONLY SURVIVING LINE: Prosper, Tome-Bound; then Mind Stone — survives (life 2, 2, 2)" in a.flags
    assert a.role == ROLE_CONTROL and "only line: Prosper, Tome-Bound" in a.role_reason
    assert a.lookahead[0].casts == ["Prosper, Tome-Bound"] and a.unmodelled == []
    assert _commander_line(a) == _COMMANDER_LINE + (
        "Prosper, Tome-Bound {2}{B}{R} now (no tax yet); castable this turn; the best line casts it on T10"
    )
    planning = a.planning_block()
    assert "  1. T10: Prosper, Tome-Bound -> life 2" in planning and _COMMANDER_LINE in planning
    assert "castable alone: Mind Stone, Prosper, Tome-Bound" in planning


def test_the_commander_tax_puts_our_only_out_beyond_our_mana():
    from tests.strategic_states import BRAWL_TAXED_OUT

    a = _fresh(BRAWL_TAXED_OUT)
    assert "DEAD NEXT ATTACK even after our best castable plays" in a.flags
    assert not any(flag.startswith(("ONLY", "BEST MODELLED")) for flag in a.flags)
    assert _commander_line(a) == _COMMANDER_LINE + (
        "Prosper, Tome-Bound {4}{B}{R} now ({2} tax for 1 previous cast); not castable by T14 (6 mana needed)"
    )


def test_a_second_cast_from_the_command_zone_pays_the_tax():
    from tests.strategic_states import BRAWL_SECOND_CAST

    a = _fresh(BRAWL_SECOND_CAST)
    assert any(flag.startswith("ONLY SURVIVING LINE: Prosper, Tome-Bound") for flag in a.flags)
    assert a.lookahead[0].casts == ["Prosper, Tome-Bound"] and a.lookahead[0].mana == 6
    assert _commander_line(a) == _COMMANDER_LINE + (
        "Prosper, Tome-Bound {4}{B}{R} now ({2} tax for 1 previous cast); castable this turn; "
        "the best line casts it on T10"
    )


def test_the_real_t7_commander_reaches_the_candidate_lines_and_is_marked_unmodelled():
    from tests.strategic_states import BRAWL_T7

    # Historic Brawl, 2026-10-07: the plan deferred The Notary Hobbits to "~T11-13" because no
    # candidate line ever cast it. Now the lines do, its copies are named as unmodelled, and
    # the prompt says it is castable this turn.
    a = _fresh(BRAWL_T7)
    assert "The Notary Hobbits" in a.unmodelled and "The Notary Hobbits" in a.lookahead[0].castable
    planning = a.planning_block()
    assert "T7: Forest + The Notary Hobbits (not modelled) -> life" in planning
    assert _commander_line(a).startswith(
        _COMMANDER_LINE + "The Notary Hobbits {3}{G}{G} now (no tax yet); castable this turn; the best line "
    )
    assert _commander_line(a).endswith("; not modelled: its tokens — weigh that yourself")
    assert not any(flag.startswith(("ONLY", "LETHAL LINE")) for flag in a.flags)


def _spider_board(commander: dict | None) -> dict:
    """At 4 life vs their Sky Knight (4/4 flier) and Gray Ogre: only a reach blocker survives their
    next attack (and dies blocking it).

    Five lands (Forest x2, Mountain x3) pay Giant Spider ({3}{R}, reach) or The Notary Hobbits
    ({3}{G}{G}), not both; the Hobbits' own body chump-blocks only the Ogre.
    """
    from tests.strategic_states import brawl_board

    board = brawl_board(
        lands=["Forest", "Forest", "Mountain", "Mountain", "Mountain"],
        theirs=[(410, "Sky Knight", 2, False, 5), (413, "Gray Ogre", 2, False, 7)],
        hand=[(501, "Giant Spider")],
    )
    board["command"] = [] if commander is None else [commander]
    board["commander_grp_ids"] = [103511]
    board["commander_casts"] = {103511: 0}
    return board


def test_an_unmodelled_commander_qualifies_the_only_line_through_their_attack():
    hobbits = card(301, "The Notary Hobbits", 1, turn_entered_battlefield=-1, object_kind="CARD")
    plain = _fresh(_spider_board(None))
    assert "ONLY LINE THAT SURVIVES THEIR NEXT ATTACK: Giant Spider — dead on T13" in plain.flags
    assert "only line: Giant Spider" in plain.role_reason
    a = _fresh(_spider_board(hobbits))
    assert a.unmodelled == ["The Notary Hobbits"] and a.line_search.only_first_attack_survivor
    assert not any(flag.startswith("ONLY") for flag in a.flags)
    assert (
        "BEST MODELLED LINE THROUGH THEIR NEXT ATTACK (not modelled: The Notary Hobbits): "
        "Giant Spider; then The Notary Hobbits — dead on T13" in a.flags
    )
    assert any(
        flag.startswith("DEAD IN 2") and flag.endswith("(not modelled: The Notary Hobbits)")
        for flag in a.flags
    )
    assert "only line" not in a.role_reason


def test_a_commander_without_card_data_or_cost_still_qualifies_the_claims():
    unknown = {
        "instance_id": 301, "grp_id": 103511, "name": "Unknown (103511)", "type_line": "", "mana_cost": "",
        "oracle_text": "", "owner_seat_id": 1, "controller_seat_id": 1, "power": 1, "toughness": 1,
        "card_types": ["CardType_Creature"], "object_kind": "CARD",
    }  # fmt: skip
    a = _fresh(_spider_board(unknown))
    # No cost anywhere (no card data, no cast action): never cast, but it may be castable now.
    assert a.unmodelled == ["Unknown (103511)"]
    assert any(
        flag.startswith("BEST MODELLED LINE THROUGH THEIR NEXT ATTACK (not modelled: Unknown (103511)): ")
        for flag in a.flags
    )
    assert "our commander Unknown (103511): cost unknown (not cast in the lines)" in a.unknowns
    assert _commander_line(a) == _COMMANDER_LINE + (
        "Unknown (103511): cost unknown (no card data, no cast action from Arena) — judge it yourself"
    )
    # Arena's cast action supplies the cost: a 1/1 the lines can cast, its text still unknown.
    board = _spider_board(unknown)
    board["legal_actions_raw"] = [
        {"actionType": "ActionType_Cast", "grpId": 103511, "instanceId": 301,
         "manaCost": [{"color": ["ManaColor_Generic"], "count": 3}, {"color": ["ManaColor_Green"], "count": 2}]},
    ]  # fmt: skip
    a = _fresh(board)
    assert a.unmodelled == ["Unknown (103511)"]
    assert _commander_line(a).endswith("; not modelled: its rules text is unknown — weigh that yourself")
    assert "our commander Unknown (103511): rules text unknown" in a.unknowns


def test_the_role_guard_plays_the_land_that_makes_our_commander_castable():
    from tests.strategic_states import BRAWL_COMMANDER_SAVES

    # Three lands and a Mountain in hand: the rock now, or the land that pays for Prosper.
    state = deepcopy(BRAWL_COMMANDER_SAVES)
    mountain = next(c for c in state["battlefield"] if c["instance_id"] == 404)
    state["battlefield"].remove(mountain)
    state["hand"].append({**mountain, "is_tapped": False, "turn_entered_battlefield": -1})
    state["players"][0]["lands_played"] = 0
    decision = actions_decision(
        [
            ("idx:0", "Cast Prosper, Tome-Bound", cast(301, 81845), False),
            ("idx:1", "Cast Mind Stone", cast(501), True),
            ("idx:2", "Play Land: Mountain", play(404), None),
        ]
    )
    verdict = role_guard(_fresh(state), decision, "idx:1", state)
    assert verdict is not None and verdict.option_id == "idx:2"
    assert "play Mountain first to cast Prosper, Tome-Bound (1/4)" in verdict.reason


@pytest.mark.parametrize(
    "source",
    [G1_T8, G1_T12, G1_T14, G1_T15_FROM_OPPONENT, BUG_174855],
    ids=["T8", "T12", "T14", "T15", "174855"],
)
def test_boards_without_a_command_zone_get_no_commander_facts(source):
    a = _fresh(source)
    assert a.commander == [] and "COMMANDER (" not in a.planning_block()
