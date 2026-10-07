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


@pytest.mark.parametrize("mode", list(SEARCH_FALLBACKS))
@pytest.mark.parametrize("name", list(BASELINE))
def test_fixture_facts_match_the_log_named_baseline(name, mode, monkeypatch):
    # The greedy pipeline these rows record: the kill switch, a failing search
    # and a truncated one all keep it (a dead-now search has no T step to cut).
    SEARCH_FALLBACKS[mode](monkeypatch)
    role, lethal_now, all_in, dead_in, now_life, flags, lookahead = BASELINE[name]
    a = _fresh(ALL_NAMED[name])
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
