"""Offline regressions for ranked monitoring #330, #335 and #336.

Reduced public-state cases from 00077e73/b92b3450 (false NO TARGETS),
c45ecae9 T10 (Shock366 -> Kutzil325, Strike368 redirected to Cat354),
and fcd7fc5d T12 (Great Hall285 live Creature+Land 2/4, Iroh mode idx:1).
No live game, model service, card database refresh or gameplay input is used.
"""

import time
from types import SimpleNamespace

import pytest
from tests.test_burn_targeting_20261019 import (
    LOCAL,
    OPP,
    SHOCK,
    STRIKE,
    _creature,
    _planner,
    _spell,
    _state,
    _targets,
)
from tests.test_modal_no_kill_switch_20261019 import IROH
from tests.test_target_state_facts import _option_facts, _prompt

from arenamcp.cast_triggers import untargeted_mode_instead
from arenamcp.coach import CoachEngine
from arenamcp.combat_keywords import is_creature_now
from arenamcp.decisions import expand_target_selection
from arenamcp.play_safety import our_stack_damage


def analyze(text, battlefield=()):
    opposing = [c for c in battlefield if c.get("controller_seat_id") == OPP]
    creatures = [c for c in battlefield if is_creature_now(c)]
    rejected = set()
    info = CoachEngine._analyze_removal(
        SimpleNamespace(),
        text.lower(),
        [c for c in opposing if is_creature_now(c)],
        opposing,
        creatures,
        list(battlefield),
        "Spell",
        rejected,
    )
    return info, rejected


@pytest.mark.parametrize(
    "oracle",
    [
        SHOCK,
        STRIKE,
        "Tectonic Hazard deals 1 damage to each opponent and each creature they control.",
        "Boltwave deals 3 damage to each opponent.",
        "Spell deals 2 damage to target player.",
        "Spell deals 2 damage to target opponent.",
        "Spell deals 2 damage to target planeswalker.",
        "Spell deals 2 damage to up to one target creature.",
        "Choose one — Spell deals 1 damage to each opponent; or Spell deals 4 damage to target creature.",
    ],
)
def test_empty_creature_pool_does_not_prove_burn_has_no_targets(oracle):
    walker = {
        "instance_id": 329,
        "name": "Jace",
        "type_line": "Planeswalker",
        "card_types": ["CardType_Planeswalker"],
        "loyalty": 2,
        "controller_seat_id": OPP,
    }
    for board in ([], [walker]):
        info, rejected = analyze(oracle, board)
        assert "NO TARGETS" not in info and not rejected


def test_creature_only_empty_board_still_has_no_targets_but_own_creature_counts():
    text = "Spell deals 4 damage to target creature."
    assert analyze(text)[1] == {"Spell"}
    own = _creature(100, "Our creature", LOCAL, 2, 2)
    assert not analyze(text, [own])[1]
    assert analyze("Spell deals 4 damage to target creature an opponent controls.", [own])[1] == {"Spell"}


def combo():
    kutzil = _creature(325, "Kutzil, Malamet Exemplar", OPP, 5, 5)
    cat = _creature(354, "Cat", OPP, 3, 3)
    shock = _spell(366, "Shock", "Instant", "{R}", SHOCK)
    shock["targeting"] = [325]
    strike = _spell(368, "Lightning Strike", "Instant", "{1}{R}", STRIKE)
    return _state([kutzil, cat], stack=[shock, strike], source=368)


def planner_for_combo():
    planner = _planner()
    planner._last_decision_reasoning = "Lightning Strike finishes Kutzil after Shock."
    planner._cast_intent = {
        "card": "Lightning Strike",
        "line_target": "Cat",
        "line_target_id": 354,
        "at": time.monotonic(),
    }
    return planner


@pytest.mark.parametrize("fallback", [False, True])
def test_335_pending_shock_preserves_intended_strike_even_when_search_prefers_cat(fallback):
    state = combo()
    decision = _targets((LOCAL, OPP, 325, 354), "Lightning Strike")
    pending = our_stack_damage(state, exclude=368)
    assert pending.by_target == {325: 2} and not pending.doomed
    assert planner_for_combo()._prefer_lethal_damage_target(
        decision,
        state,
        ["tgt:325"],
        fallback=fallback,
    ) == ["tgt:325"]
    facts = _option_facts(_prompt(state, decision), "tgt:325")
    assert facts["dies_to_damage"] == 5  # pending damage is not marked damage
    assert facts["pending_damage_if_resolves"] == 2
    assert facts["additional_damage_after_pending"] == 3


@pytest.mark.parametrize(
    "change",
    [
        "gone",
        "different_target",
        "unknown_target",
        "opponent_controlled",
        "countered",
        "ward",
        "pump_response",
        "protected",
        "stale_target",
    ],
)
def test_pending_damage_requires_current_uncontested_target_evidence(change):
    state = combo()
    shock, strike = state["stack"]
    if change == "gone":
        state["stack"] = [strike]
    elif change == "different_target":
        shock["targeting"] = [354]
    elif change == "unknown_target":
        shock["targeting"] = []
    elif change == "opponent_controlled":
        shock["controller_seat_id"] = OPP
    elif change == "countered":
        state["stack"].append({"instance_id": 400, "controller_seat_id": OPP, "targeting": [366]})
    elif change == "ward":
        state["stack"].append(
            {
                "instance_id": 400,
                "controller_seat_id": OPP,
                "parent_instance_id": 325,
                "object_kind": "ABILITY",
            }
        )
    elif change == "pump_response":
        state["stack"].append({"instance_id": 400, "controller_seat_id": OPP, "targeting": [325]})
    elif change == "protected":
        state["battlefield"][0]["oracle_text"] = "Protection from red"
    elif change == "stale_target":
        state["battlefield"][0]["instance_id"] = 500  # same named card reentered
    assert not our_stack_damage(state, exclude=368).by_target.get(325)
    if change != "stale_target":
        decision = _targets((LOCAL, OPP, 325, 354), "Lightning Strike")
        assert planner_for_combo()._prefer_lethal_damage_target(decision, state, ["tgt:325"]) == ["tgt:354"]


def test_current_spell_is_not_counted_twice_and_indestructibility_is_not_a_kill():
    state = combo()
    state["stack"][1]["targeting"] = [325]
    assert our_stack_damage(state, exclude=368).by_target == {325: 2}
    assert our_stack_damage(state).by_target == {325: 5}
    state["battlefield"][0]["keywords"] = ["indestructible"]
    assert not our_stack_damage(state).doomed
    assert planner_for_combo()._prefer_lethal_damage_target(
        _targets((325, 354), "Lightning Strike"),
        state,
        ["tgt:325"],
    ) == ["tgt:354"]


def test_illegal_face_or_protected_target_absent_from_menu_is_never_invented():
    state = combo()
    state["stack"] = state["stack"][1:]
    # Arena excludes the protected player and protected Cat from this request.
    decision = _targets((325,), "Lightning Strike")
    result = planner_for_combo()._prefer_lethal_damage_target(decision, state, ["tgt:325"])
    assert result == ["tgt:325"]
    assert not expand_target_selection(decision, ["tgt:1"])
    assert not expand_target_selection(decision, ["tgt:354"])


def hall():
    return _creature(
        285,
        "Great Hall of the Biblioplex",
        OPP,
        2,
        4,
        type_line="Land",
        card_types=["CardType_Creature", "CardType_Land"],
    )


def _with_payoff(state):
    """A cast-trigger attacker makes the untargeted mode's cast payoff real (#65/#329)."""
    state["battlefield"].append(_creature(300, "Slickshot Show-Off", LOCAL, 1, 2, keywords=["prowess"]))
    return state


def test_336_animated_land_keeps_four_damage_mode_and_creature_removal_annotation():
    state = _with_payoff(_state([hall()]))
    assert untargeted_mode_instead(IROH, ["idx:1"], state) is None
    assert not analyze("Spell deals 4 damage to target creature.", state["battlefield"])[1]
    # A targeted removal request sees the same live creature type.
    spell = _spell(400, "Removal", "Sorcery", "{1}{R}", "Removal deals 4 damage to target creature.")
    state.update(stack=[spell], _bridge_request_payload={"sourceId": 400})
    assert _planner()._prefer_lethal_damage_target(
        _targets((285,), "Removal"),
        state,
        ["tgt:285"],
    ) == ["tgt:285"]


def test_live_noncreature_type_overrides_stale_printed_creature_and_pt():
    card = hall()
    card.update(type_line="Land Creature", card_types=["CardType_Land"])
    state = _with_payoff(_state([card]))
    assert untargeted_mode_instead(IROH, ["idx:1"], state)[0] == "idx:0"
    assert analyze("Spell deals 4 damage to target creature.", [card])[1] == {"Spell"}
    state["stack"] = [{**_spell(366, "Shock", "Instant", "{R}", SHOCK), "targeting": [285]}]
    assert our_stack_damage(state).by_target == {}


def test_unknown_current_creature_toughness_or_type_does_not_force_mode_change():
    for card in (dict(hall(), toughness=None), {"instance_id": 285, "controller_seat_id": OPP}):
        assert untargeted_mode_instead(IROH, ["idx:1"], _with_payoff(_state([card]))) is None


def test_indestructible_or_too_large_land_still_allows_cast_trigger_mode_fallback():
    for card in (dict(hall(), toughness=7), dict(hall(), keywords=["indestructible"])):
        assert untargeted_mode_instead(IROH, ["idx:1"], _with_payoff(_state([card])))[0] == "idx:0"


def test_untargeted_damage_ignores_hexproof_but_not_protection_or_contested_state():
    state = combo()
    state["stack"] = [
        _spell(
            400, "Sweep", "Sorcery", "{R}", "Sweep deals 2 damage to each creature your opponents control."
        )
    ]
    state["battlefield"][0]["keywords"] = ["hexproof"]
    assert our_stack_damage(state).by_target[325] == 2
    state["battlefield"][0]["oracle_text"] = "Protection from red"
    assert 325 not in our_stack_damage(state).by_target
    state["battlefield"][0]["oracle_text"] = ""
    state["stack"].append({"controller_seat_id": OPP, "targeting": [325]})
    assert 325 not in our_stack_damage(state).by_target


def test_authoritative_empty_stack_menu_discards_stale_pending_damage():
    state = combo()
    state["_bridge_request_type"] = "ActionsAvailable"
    state["_bridge_actions"] = [{"actionType": "ActionType_Play", "instanceId": 700}]
    assert our_stack_damage(state).by_target == {}
    state["_bridge_request_type"] = "SelectTargets"
    # A cached action list is not the current target request's menu.
    assert our_stack_damage(state).by_target == {325: 2}


def test_an_already_doomed_five_five_does_not_justify_a_redundant_strike():
    state = combo()
    # Two already targeted Strikes kill Kutzil without the current spell.
    state["stack"][0]["oracle_text"] = STRIKE
    state["stack"].insert(1, dict(state["stack"][0], instance_id=367))
    assert our_stack_damage(state, exclude=368).doomed == {325}
    assert planner_for_combo()._prefer_lethal_damage_target(
        _targets((325, 354), "Lightning Strike"),
        state,
        ["tgt:325"],
    ) == ["tgt:354"]


def test_land_with_live_stats_but_missing_current_types_cannot_prove_no_creatures():
    card = hall()
    card.pop("card_types")
    assert untargeted_mode_instead(IROH, ["idx:1"], _with_payoff(_state([card]))) is None


@pytest.mark.parametrize("creature_now", [True, False])
def test_hand_formatter_uses_live_land_types_for_creature_only_removal(creature_now):
    card = hall()
    if not creature_now:
        card["card_types"] = ["CardType_Land"]
    spell = _spell(400, "Removal", "Sorcery", "{1}{R}", "Removal deals 4 damage to target creature.")
    state = _state([card], hand=[spell])
    lines, rejected, _ = CoachEngine.__new__(CoachEngine)._format_hand_cards(
        state,
        LOCAL,
        2,
        {"R": 2},
        [card],
        [card],
        True,
        "Phase_Main1",
        12,
        [],
    )
    assert ("Removal" in rejected) is not creature_now
    assert ("NO TARGETS" in "\n".join(lines)) is not creature_now
