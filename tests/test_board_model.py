"""The shared board model (``arenamcp.board_model``) on real 2026-10-06 states.

P0 phase bug: bridge snapshots carry ``CurrentPhase.ToString()`` names
("Main1", step "None"; all twelve bug_20261006_*.json game states do), which
``board_assessment`` never matched, so no attack was ever pending on the Mac.
``canonical_phase_step`` maps both spellings to the log's form, and
``build_board_model`` must reproduce the facts ``_assess`` computed inline on
the fixtures in tests/strategic_states.py.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from copy import deepcopy

import pytest
from tests.strategic_states import (
    G1_T8,
    G1_T10,
    G1_T12,
    G1_T12_AFTER_VOLUME,
    G1_T14,
    G1_T15_FROM_OPPONENT,
    card,
)

from arenamcp import board_assessment as ba
from arenamcp.board_model import BoardModel, able_now, build_board_model, canonical_phase_step
from arenamcp.combat_keywords import has_combat_keyword
from arenamcp.concede import normalize_phases

FIXTURES = {
    "T8": G1_T8,
    "T10": G1_T10,
    "T12": G1_T12,
    "T12_AFTER_VOLUME": G1_T12_AFTER_VOLUME,
    "T14": G1_T14,
    "T15_FROM_OPPONENT": G1_T15_FROM_OPPONENT,
}

# GRE Phase/Step enums (re-output/GreProtobuf/.../Phase.cs, Step.cs) without "None".
PHASES = ("Beginning", "Main1", "Combat", "Main2", "Ending")
STEPS = (
    "Untap", "Upkeep", "Draw", "BeginCombat", "DeclareAttack", "DeclareBlock",
    "FirstStrikeDamage", "CombatDamage", "EndCombat", "End", "Cleanup",
)  # fmt: skip


def _model(state) -> BoardModel:
    model = build_board_model(deepcopy(state))
    assert model is not None
    return model


def _with_turn(state, *, phase, step, active=None, turn=None):
    result = deepcopy(state)
    result["turn"].update(phase=phase, step=step)
    if active is not None:
        result["turn"]["active_player"] = result["turn"]["priority_player"] = active
    if turn is not None:
        result["turn"]["turn_number"] = turn
    return result


def _fresh_assess(state, monkeypatch):
    # The legacy greedy pipeline: the facts below predate the line search.
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    ba._CACHE.clear()
    return ba.assess(deepcopy(state))


# --- canonical phase/step ------------------------------------------------------


@pytest.mark.parametrize(
    ("phase", "step", "expected"),
    [
        ("Main1", "None", ("Phase_Main1", "")),
        ("Combat", "DeclareBlock", ("Phase_Combat", "Step_DeclareBlock")),
        ("Combat", "CombatDamage", ("Phase_Combat", "Step_CombatDamage")),
        ("Combat", "DeclareAttack", ("Phase_Combat", "Step_DeclareAttack")),
        ("Main2", "None", ("Phase_Main2", "")),
        ("Beginning", "Upkeep", ("Phase_Beginning", "Step_Upkeep")),
        ("Ending", "End", ("Phase_Ending", "Step_End")),
        ("None", "None", ("", "")),
        ("", "", ("", "")),
        (None, None, ("", "")),
        ("Phase_None", "Step_None", ("", "")),
        ("Phase_Main1", "", ("Phase_Main1", "")),
        ("Phase_Combat", "Step_DeclareBlock", ("Phase_Combat", "Step_DeclareBlock")),
        ("Phase_Combat", "Step_CombatDamage", ("Phase_Combat", "Step_CombatDamage")),
        (" main1 ", "declareblock", ("Phase_Main1", "Step_DeclareBlock")),
    ],
)
def test_canonical_phase_step_maps_bridge_and_log_names_to_log_form(phase, step, expected):
    assert canonical_phase_step({"phase": phase, "step": step}) == expected
    assert canonical_phase_step(dict(zip(("phase", "step"), expected, strict=True))) == expected


def test_canonical_phase_step_covers_every_gre_name_and_is_idempotent():
    for name in PHASES:
        assert canonical_phase_step({"phase": name})[0] == f"Phase_{name}"
        assert canonical_phase_step({"phase": f"Phase_{name}"})[0] == f"Phase_{name}"
    for name in STEPS:
        assert canonical_phase_step({"step": name})[1] == f"Step_{name}"
        assert canonical_phase_step({"step": f"Step_{name}"})[1] == f"Step_{name}"
    for turn_info in (None, {}, "Main1", 7, {"phase": "Main1"}, {"turn_number": 4}):
        phase, step = canonical_phase_step(turn_info)
        assert canonical_phase_step({"phase": phase, "step": step}) == (phase, step)
    assert canonical_phase_step(None) == ("", "")


def test_canonical_phase_step_composes_with_concede_normalize_phases():
    for phase, step in [("Main1", "None"), ("Combat", "DeclareBlock"), ("Phase_Main2", ""), ("", "None")]:
        state = {"turn": {"phase": phase, "step": step}}
        canonical = canonical_phase_step(state["turn"])
        assert canonical_phase_step(normalize_phases(state)["turn"]) == canonical
        already = {"turn": {"phase": canonical[0], "step": canonical[1]}}
        assert normalize_phases(already) is already


# --- the model reproduces today's _assess facts ----------------------------------

# Facts today's _assess computed inline on each fixture (HEAD 778fee8).
PINNED = {
    "T8": dict(ours=0, theirs=2, sources=(3, 3), lands=(3, 4), hand_lands=[], drop=False, colors="GU"),
    "T10": dict(ours=0, theirs=3, sources=(3, 3), lands=(3, 5), hand_lands=["Forest"], drop=True, colors="GU"),
    "T12": dict(ours=0, theirs=4, sources=(4, 4), lands=(4, 5), hand_lands=["Island"], drop=True, colors="GU"),
    "T12_AFTER_VOLUME": dict(
        ours=0, theirs=4, sources=(2, 5), lands=(4, 5), hand_lands=["Island"], drop=True, colors="BGRUW"
    ),
    "T14": dict(
        ours=0, theirs=5, sources=(6, 6), lands=(5, 5), hand_lands=["Island", "Island"], drop=True,
        colors="BGRUW",
    ),
    "T15_FROM_OPPONENT": dict(ours=5, theirs=1, sources=(5, 5), lands=(5, 6), hand_lands=[], drop=False, colors="RU"),
}  # fmt: skip


@pytest.mark.parametrize("name", list(FIXTURES))
def test_model_pins_todays_inline_facts(name):
    model, pinned = _model(FIXTURES[name]), PINNED[name]
    assert (model.phase, model.step, model.our_turn) == ("Phase_Main1", "", True)
    assert model.our_attack_pending and not model.their_attack_pending
    assert model.first_their_attackers is None
    assert (len(model.ours), len(model.theirs)) == (pinned["ours"], pinned["theirs"])
    assert (len(model.sources_now), len(model.sources_all)) == pinned["sources"]
    assert (model.our_lands, model.their_lands) == pinned["lands"]
    assert [c["name"] for c in model.hand_lands] == pinned["hand_lands"]
    assert model.land_drop_available is pinned["drop"]
    assert "".join(sorted(model.colors_all - {"C"})) == pinned["colors"]
    assert model.missing_colors == () and not model.has_x_spells and not model.unknown_bodies
    assert (model.t_casts_pre_combat, model.t_casts_post_combat, model.t_instant_only) == (True, False, False)
    assert not model.stack_nonempty


def test_model_spells_are_the_hand_nonlands_in_order():
    spells = [(s.name, s.role, s.mana_value, s.has_x) for s in _model(G1_T12).spells]
    assert spells == [
        ("Undulating Witness", "creature", 5, False),
        ("Murmuring Volume", "ramp", 3, False),
        ("Archive Arbiter", "creature", 6, False),
        # {U}{U} plus "behold a Jace or pay {1}": the additional mana is part of its cost.
        ("Countersculpt", "counter", 3, False),
    ]
    t15 = _model(G1_T15_FROM_OPPONENT)
    # One Cadet is tapped: four of our five creatures can attack now.
    assert [b["name"] for b in t15.first_our_attackers] == [
        "Fatehold Chronologist",
        "Paradox Shaper",
        "Heartstring Puller",
        "Cadet",
    ]
    assert (t15.our_life, t15.opp_life, t15.their_hand) == (20, 4, 2)


@pytest.mark.parametrize("name", list(FIXTURES))
def test_model_matches_the_assessment_fields(name, monkeypatch):
    a, model = _fresh_assess(FIXTURES[name], monkeypatch), _model(FIXTURES[name])
    assert (a.turn, a.our_turn, a.phase, a.our_life, a.opp_life) == (
        model.turn,
        model.our_turn,
        model.phase,
        model.our_life,
        model.opp_life,
    )
    assert (a.our_creatures, a.their_creatures) == (len(model.ours), len(model.theirs))
    assert (a.our_power, a.their_power) == (
        sum(b["power"] for b in model.ours),
        sum(b["power"] for b in model.theirs),
    )
    assert (a.our_hand, a.their_hand, a.our_lands, a.their_lands) == (
        len(model.hand),
        model.their_hand,
        model.our_lands,
        model.their_lands,
    )
    assert (a.lands_in_hand, a.land_drop_available) == (len(model.hand_lands), model.land_drop_available)
    assert a.colors == "".join(sorted(c for c in model.colors_all if c != "C"))
    assert a.missing_colors == "".join(model.missing_colors)
    assert a.unknowns[: len(model.unknowns)] == model.unknowns


@pytest.mark.parametrize("name", list(FIXTURES))
def test_model_drives_the_legacy_clocks_and_lookahead_unchanged(name, monkeypatch):
    """Feeding the model's tuples to the private helpers reproduces assess()."""
    a, m = _fresh_assess(FIXTURES[name], monkeypatch), _model(FIXTURES[name])
    our_clock, _ = ba._simulate_attacks(
        m.ours,
        m.theirs,
        m.opp_life,
        first_attackers=m.first_our_attackers,
        first_blockers=m.untapped_theirs if m.our_attack_pending else None,
    )
    their_clock, _ = ba._simulate_attacks(
        m.theirs,
        m.ours,
        m.our_life,
        first_attackers=m.first_their_attackers,
        first_blockers=m.our_first_blockers,
    )
    assert (our_clock, their_clock) == (a.our_clock, a.their_clock)
    assert a.lethal_now == (m.our_attack_pending and our_clock == 1)
    survival_hint = (
        a.opp_lethal_on_board
        or a.race == "behind"
        or (a.their_clock is not None and a.their_clock <= 2)
        or (a.their_creatures > a.our_creatures + 1 and a.their_power > a.our_power + 2)
    )
    budgets = ba._budget_turns(
        m.our_turn, m.turn, m.sources_now, m.sources_all, m.hand_lands, m.land_drop_now
    )
    schedule = ba._schedule(m.spells, budgets, survival=survival_hint and not a.lethal_now, theirs=m.theirs)
    lookahead, dead_in, now_life = ba._project(
        schedule=schedule,
        budgets=budgets,
        spells=m.spells,
        ours=m.ours,
        theirs=m.theirs,
        our_life=m.our_life,
        turn=m.turn,
        our_rules=m.our_rules,
        their_attack_pending=m.their_attack_pending,
        first_their_attackers=m.first_their_attackers,
        untapped_ours=m.our_first_blockers,
    )
    assert (lookahead, dead_in, now_life) == (a.lookahead, a.dead_in, a.our_life_now_attack)


# --- bridge names (the P0 fix) ---------------------------------------------------


@pytest.mark.parametrize("name", list(FIXTURES))
def test_bridge_phase_names_build_the_same_model_as_log_names(name):
    bridge = _with_turn(FIXTURES[name], phase="Main1", step="None")
    assert _model(bridge) == _model(FIXTURES[name])


def test_bridge_names_see_our_pending_lethal_attack():
    model = _model(_with_turn(G1_T15_FROM_OPPONENT, phase="Main1", step="None"))
    assert (model.phase, model.step) == ("Phase_Main1", "")
    assert model.our_attack_pending and model.pre_combat
    assert len(model.first_our_attackers) == 4


def _their_combat(phase, step):
    """G1_T14's board on the opponent's T15, with two of their creatures attacking."""
    state = _with_turn(G1_T14, phase=phase, step=step, active=2, turn=15)
    for permanent in state["battlefield"]:
        if permanent["instance_id"] in (238, 333):
            permanent.update(is_tapped=True, is_attacking=True)
    return state


@pytest.mark.parametrize(
    ("bridge", "log", "pending"),
    [
        (("Combat", "DeclareAttack"), ("Phase_Combat", "Step_DeclareAttack"), True),
        (("Combat", "DeclareBlock"), ("Phase_Combat", "Step_DeclareBlock"), True),
        (("Combat", "CombatDamage"), ("Phase_Combat", "Step_CombatDamage"), False),
        (("Combat", "EndCombat"), ("Phase_Combat", "Step_EndCombat"), False),
        (("Main2", "None"), ("Phase_Main2", ""), False),
    ],
)
def test_their_attack_timing_under_bridge_and_log_names(bridge, log, pending):
    model = _model(_their_combat(*bridge))
    assert model == _model(_their_combat(*log))
    assert not model.our_turn and not model.our_attack_pending
    assert model.any_theirs_attacking
    assert model.their_attack_pending is pending
    if pending:
        assert sorted(b["name"] for b in model.first_their_attackers) == ["Cadet", "Heartstring Puller"]
    else:
        assert model.first_their_attackers is None


def test_our_first_blockers_are_untapped_only_before_their_attack():
    # Seat 2's board seen on seat 1's turn: one of our five creatures is tapped.
    pending = _model(_with_turn(G1_T15_FROM_OPPONENT, phase="Main1", step="None", active=1))
    assert pending.their_attack_pending and len(pending.our_first_blockers) == 4
    over = _model(_with_turn(G1_T15_FROM_OPPONENT, phase="Main2", step="None", active=1))
    assert not over.their_attack_pending and len(over.our_first_blockers) == 5


# --- their first-strike damage step ------------------------------------------------
# At Step_FirstStrikeDamage that damage is already in our life total: only the
# attack's regular damage is still to come. The whole attack used to count
# again (first strikers twice, double strikers at double damage once more).


def _creature(iid, name, seat, power, toughness, text="", keywords=(), **extra):
    return {
        "instance_id": iid, "name": name, "controller_seat_id": seat, "owner_seat_id": seat,
        "type_line": "Creature", "card_types": ["CardType_Creature"], "power": power, "toughness": toughness,
        "oracle_text": text, "keywords": list(keywords), "turn_entered_battlefield": 3, **extra,
    }  # fmt: skip


def _first_strike_board(phase, step, *, life=3, attackers=("Skyknight", "Grizzly Bears")):
    """Their T9: a 3/3 flying first striker already hit us (6 -> 3); their 2/2 is blocked by our wall."""
    land = {
        "type_line": "Basic Land — Forest",
        "card_types": ["CardType_Land"],
        "oracle_text": "({T}: Add {G}.)",
    }
    theirs = {
        "Skyknight": _creature(30, "Skyknight", 2, 3, 3, "Flying, first strike", ("flying", "first strike")),
        "Grizzly Bears": _creature(31, "Grizzly Bears", 2, 2, 2),
        # Arena's capitalised keyword list; trample comes from the rules text alone.
        "Blade Knight": _creature(32, "Blade Knight", 2, 3, 3, "Double strike, trample", ("Double strike",)),
    }
    return {
        "local_seat_id": 1, "opponent_seat_id": 2,
        "turn": {"turn_number": 9, "active_player": 2, "priority_player": 1, "phase": phase, "step": step},
        "players": [
            {"seat_id": 1, "life_total": life, "is_local": True, "lands_played": 0},
            {"seat_id": 2, "life_total": 20, "is_local": False, "lands_played": 1},
        ],
        "battlefield": [
            *[{**land, "instance_id": 10 + i, "name": "Forest", "controller_seat_id": 1} for i in range(4)],
            *[{**theirs[name], "is_tapped": True, "is_attacking": True} for name in attackers],
            _creature(40, "Wall of Wood", 1, 0, 4, "Defender", ("defender",)),
            _creature(41, "Llanowar Elves", 1, 1, 1, "{T}: Add {G}."),
        ],
        "hand": [_creature(50, "Giant Spider", 1, 2, 4, "Reach", ("reach",), mana_cost="{3}{G}")],
        "stack": [],
        "zones": {"library_count": 30, "opponent_hand_count": 3},
    }  # fmt: skip


@pytest.mark.parametrize(
    ("phase", "step"), [("Combat", "FirstStrikeDamage"), ("Phase_Combat", "Step_FirstStrikeDamage")]
)
def test_first_strike_damage_step_leaves_only_regular_damage_pending(phase, step):
    model = _model(_first_strike_board(phase, step))
    assert model.step == "Step_FirstStrikeDamage"
    assert model.their_attack_pending
    assert [b["name"] for b in model.first_their_attackers] == ["Grizzly Bears"]
    # Their later attacks still have the first striker.
    assert sorted(b["name"] for b in model.theirs) == ["Grizzly Bears", "Skyknight"]
    declared = _model(_first_strike_board(phase, step.replace("FirstStrikeDamage", "DeclareBlock")))
    assert sorted(b["name"] for b in declared.first_their_attackers) == ["Grizzly Bears", "Skyknight"]


@pytest.mark.parametrize("step", ["FirstStrikeDamage", "Step_FirstStrikeDamage"])
def test_a_double_striker_deals_its_regular_damage_once_more(step):
    state = _first_strike_board("Combat", step, life=7, attackers=("Blade Knight",))
    before = json.dumps(state, sort_keys=True)
    model = _model(state)
    (rest,) = model.first_their_attackers
    (knight,) = model.theirs
    assert rest["instance_id"] == knight["instance_id"] and rest["power"] == 3
    assert has_combat_keyword(knight, "double strike")  # later attacks: full double strike
    for keyword in ("double strike", "first strike"):
        assert not has_combat_keyword(rest, keyword)
    assert has_combat_keyword(rest, "trample") and rest["keywords"] == ["trample"]
    assert json.dumps(state, sort_keys=True) == before
    declared = _model(_first_strike_board("Combat", "DeclareBlock", life=7, attackers=("Blade Knight",)))
    assert declared.first_their_attackers == model.theirs


def test_only_first_strikers_attacking_leaves_nothing_pending_after_first_strike_damage():
    model = _model(_first_strike_board("Combat", "FirstStrikeDamage", attackers=("Skyknight",)))
    assert model.any_theirs_attacking
    assert not model.their_attack_pending and model.first_their_attackers is None
    # The same facts as once combat damage is dealt: our next blockers are everything.
    done = _model(_first_strike_board("Combat", "CombatDamage", attackers=("Skyknight",)))
    assert dataclasses.replace(model, step=done.step, in_combat_before_damage=False) == done
    assert len(model.our_first_blockers) == 2


@pytest.mark.parametrize(
    ("active", "phase", "expected"),
    [
        (1, "Beginning", (True, False, False)),
        (1, "Main1", (True, False, False)),
        (1, "Combat", (False, True, False)),
        (1, "Main2", (False, True, False)),
        (1, "Ending", (False, False, True)),
        (1, "None", (False, False, False)),
        (2, "Main1", (True, False, False)),
        (2, "Combat", (True, False, False)),
        (2, "Ending", (True, False, False)),
    ],
)
def test_cast_timing_flags(active, phase, expected):
    for spelled in (phase, f"Phase_{phase}"):
        model = _model(_with_turn(G1_T12, phase=spelled, step="None", active=active))
        assert (model.t_casts_pre_combat, model.t_casts_post_combat, model.t_instant_only) == expected


# --- unknowns, colours, stack, contract -------------------------------------------


def test_unknown_bodies_x_spells_and_missing_colors_match_assess(monkeypatch):
    state = deepcopy(G1_T12)
    state["battlefield"].append(card(990, "Cadet", 2, power=None, toughness=None, is_tapped=False))
    state["hand"].append(card(991, "Blazing Crescendo", 1))
    state["hand"].append(
        {
            "instance_id": 992,
            "name": "Fireball",
            "controller_seat_id": 1,
            "type_line": "Sorcery",
            "mana_cost": "{X}{R}",
            "oracle_text": "Fireball deals X damage to any target.",
            "card_types": ["CardType_Sorcery"],
        }
    )
    state["stack"] = [{"instance_id": 993, "name": "Twinned Vision"}]
    model = _model(state)
    assert model.unknown_bodies == ("Cadet",)
    assert model.has_x_spells and model.missing_colors == ("R",)
    assert model.unknowns == ["Cadet has unknown power/toughness", "X spells are not scheduled"]
    assert model.stack_nonempty
    a = _fresh_assess(state, monkeypatch)
    assert a.unknowns[:2] == model.unknowns
    assert a.missing_colors == "R"


def test_unknown_seats_or_turn_give_no_model():
    assert build_board_model({"players": [], "turn": {"turn_number": 3}}) is None
    assert build_board_model(_with_turn(G1_T12, phase="Main1", step="None", turn=0)) is None


def _bug_223955(seated: bool = False):
    """bug_20261005_223955 (Mac bridge): their T26 attack, we are at 2 with an
    untapped Mindseeker Oculus. No card in the report had a seat: Mac card
    entries carry controller_entity_id, not the controller_id the server
    reads. ``seated`` restores the creatures' seats (Oculus was ours)."""

    def permanent(iid, name, type_line, power=None, toughness=None, text="", attacking=False, seat=None):
        return {
            "instance_id": iid, "name": name, "type_line": type_line, "power": power, "toughness": toughness,
            "oracle_text": text, "is_tapped": attacking, "is_attacking": attacking,
            "controller_seat_id": seat if seated else None, "owner_seat_id": seat if seated else None,
            "turn_entered_battlefield": -1, "object_kind": "TOKEN" if "Token" in type_line else "CARD",
        }  # fmt: skip

    flying = "Flying\nWhenever this creature attacks, it deals 1 damage to each opponent and you gain 1 life."
    return {
        "local_seat_id": 1, "opponent_seat_id": 2,
        "turn": {"turn_number": 26, "active_player": 2, "priority_player": 2, "phase": "Combat", "step": "DeclareAttack"},
        "players": [{"seat_id": 1, "life_total": 2, "is_local": True}, {"seat_id": 2, "life_total": 9, "is_local": False}],
        "battlefield": [
            *[permanent(199 + 2 * i, kind, f"Basic Land — {kind}") for i, kind in enumerate(("Island", "Swamp", "Plains") * 3)],
            permanent(310, "Cadet", "Token Creature — Wizard Soldier", 2, 2, attacking=True, seat=2),
            permanent(374, "Screeching Soulbreaker", "Creature — Siren Bard", 1, 4, flying, True, seat=2),
            permanent(378, "Void Extrapolator", "Creature — Aetherborn Warlock", 3, 3, attacking=True, seat=2),
            permanent(401, "Screeching Soulbreaker", "Creature — Siren Bard", 1, 4, flying, True, seat=2),
            permanent(412, "Mindseeker Oculus", "Creature — Homunculus", 2, 1, "When this creature enters, empower Jace 4.", seat=1),
        ],
        "hand": [], "stack": [], "zones": {"library_count": 11, "opponent_hand_count": 2},
    }  # fmt: skip


def test_unseated_creatures_give_no_model_instead_of_counting_ours_as_theirs(monkeypatch):
    # Before: all five creatures were "theirs", our blockers empty, and the
    # assessment read 'OPPONENT HAS LETHAL ON BOARD' and control/stabilize.
    assert build_board_model(_bug_223955()) is None
    assert _fresh_assess(_bug_223955(), monkeypatch) is None
    model = _model(_bug_223955(seated=True))  # the lands still have no seat
    assert (
        [b["name"] for b in model.ours]
        == [b["name"] for b in model.our_first_blockers]
        == ["Mindseeker Oculus"]
    )
    assert len(model.theirs) == 4 and model.their_attack_pending
    one_unseated = _bug_223955(seated=True)
    one_unseated["battlefield"][-1].update(controller_seat_id=None, owner_seat_id=None)
    assert build_board_model(one_unseated) is None


def test_an_unseated_land_alone_still_gives_a_model():
    # bug_20261004_001406: one Forest without a seat among 23 permanents.
    state = deepcopy(G1_T12)
    land = next(c for c in state["battlefield"] if c["name"] == "Forest")
    land.update(controller_seat_id=None, owner_seat_id=None)
    model = _model(state)
    assert model.our_lands == _model(G1_T12).our_lands - 1


def test_model_is_frozen_deterministic_and_leaves_the_state_alone():
    state = deepcopy(G1_T14)
    before = json.dumps(state, sort_keys=True)
    model = build_board_model(state)
    assert json.dumps(state, sort_keys=True) == before
    assert model == build_board_model(deepcopy(G1_T14))
    with pytest.raises(dataclasses.FrozenInstanceError):
        model.our_life = 20  # type: ignore[misc]
    with pytest.raises(TypeError):
        model.our_rules["haste"] = True  # type: ignore[index]
    for field in dataclasses.fields(model):
        value = getattr(model, field.name)
        assert not isinstance(value, (list, set, dict)), field.name
    assert able_now(list(model.theirs)) == [b for b in model.theirs if not b["_tapped"] and not b["_sick"]]


def test_board_model_imports_no_llm_backend():
    """board_model's own import closure (package __init__ bypassed) has no backend."""
    code = (
        "import importlib.util, sys, types\n"
        "spec = importlib.util.find_spec('arenamcp')\n"
        "pkg = types.ModuleType('arenamcp')\n"
        "pkg.__path__ = list(spec.submodule_search_locations)\n"
        "sys.modules['arenamcp'] = pkg\n"
        "import arenamcp.board_model\n"
        "bad = [m for m in sys.modules if m.startswith(('arenamcp.backends', 'arenamcp.coach'))"
        " or m in ('requests', 'httpx', 'openai')]\n"
        "print(','.join(sorted(bad)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=60, check=True
    )
    assert result.stdout.strip() == ""


# --- 2026-10-07 review regressions ------------------------------------------------------------


def test_casting_costs_and_conditions_come_from_the_card():
    from tests.strategic_states import BUG_174855, bridge_card

    state = deepcopy(G1_T12)
    state["hand"].append(card(990, "Silence the Echo", 1))
    silence = next(s for s in _model(state).spells if s.name == "Silence the Echo")
    assert silence.mana_value == 5 and not silence.uncastable  # {1}{B} + "or pay {3}"
    proft_state = deepcopy(BUG_174855)
    proft_state["hand"].append(bridge_card(991, 106480, "Proft, Sinister Mastermind", 2, "hand"))
    model = _model(proft_state)
    assert model.our_graveyard == 6
    assert next(s for s in model.spells if s.name.startswith("Proft")).uncastable  # Threshold: seven
    assert _model(G1_T12).our_graveyard == 5  # our five, not their five


def test_our_creature_spells_on_the_stack_are_bodies_entering_now():
    from tests.strategic_states import G1_T14_MODE_STATE, G1_T14_ON_STACK

    bodies = _model(G1_T14_ON_STACK).our_stack_bodies
    assert [(b["name"], b["power"], b["toughness"], b["_sick"]) for b in bodies] == [
        ("Archive Arbiter", 4, 4, True)
    ]
    assert _model(G1_T14_MODE_STATE).our_stack_bodies == ()  # a triggered ability is not a body
    assert _model(G1_T12).our_stack_bodies == ()


# --- commanders in the command zone (Brawl, 2026-10-07) ----------------------------------------


def _commander(model, name):
    return next(s for s in model.spells if s.name == name)


def test_our_commander_is_a_spell_from_the_command_zone_priced_by_arenas_cast_action():
    from tests.strategic_states import BRAWL_T7

    model = _model(BRAWL_T7)
    (commander,) = model.commanders
    assert (commander.name, commander.cost, commander.casts, commander.from_action) == (
        "The Notary Hobbits", "{3}{G}{G}", 0, True,
    )  # fmt: skip
    spell = _commander(model, "The Notary Hobbits")
    assert (spell.zone, spell.mana_value, spell.pips) == ("command", 5, (frozenset("G"), frozenset("G")))
    # The hand is still the hand; its spells keep their order and come first.
    assert len(model.hand) == 7 and [s.zone for s in model.spells][-1] == "command"
    assert [s.name for s in model.spells if s.zone == "hand"] == [
        "Icetill Explorer", "Disciple of Freyalise", "Ugin, Eye of the Storms", "Kami of Bamboo Groves",
        "Elvish Mystic", "Woodfall Primus",
    ]  # fmt: skip
    assert model.unknowns == []


@pytest.mark.parametrize(
    ("casts", "action", "cost", "mana_value", "note"),
    [
        (0, None, "{2}{B}{R}", 4, ""),
        (1, None, "{4}{B}{R}", 6, ""),
        (2, None, "{6}{B}{R}", 8, ""),
        # Unknown tax: the printed cost, said so.
        (
            None,
            None,
            "{2}{B}{R}",
            4,
            "our commander Prosper, Tome-Bound: commander tax unknown (no previous casts assumed)",
        ),
        # Arena's cast action includes the tax: it wins, and needs no tax count.
        (
            None,
            [(["ManaColor_Generic"], 4), (["ManaColor_Black"], 1), (["ManaColor_Red"], 1)],
            "{4}{B}{R}",
            6,
            "",
        ),
        # The Mac bridge writes each colour as an enum name string.
        (
            1,
            [("ManaColor_Generic", 4), ('[ "ManaColor_Black" ]', 1), ("ManaColor_Red", 1)],
            "{4}{B}{R}",
            6,
            "",
        ),
    ],
)
def test_the_commander_tax_adds_two_per_previous_cast(casts, action, cost, mana_value, note):
    from tests.strategic_states import BRAWL_COMMANDER_SAVES

    state = deepcopy(BRAWL_COMMANDER_SAVES)
    state["commander_casts"] = {} if casts is None else {"81845": casts}  # JSON keys are strings
    if action:
        state["legal_actions_raw"] = [
            {"actionType": "ActionType_Cast", "grpId": 81845, "instanceId": 301,
             "manaCost": [{"color": color, "count": count} for color, count in action]},
        ]  # fmt: skip
    model = _model(state)
    (commander,) = model.commanders
    assert commander.cost == cost and _commander(model, "Prosper, Tome-Bound").mana_value == mana_value
    assert commander.printed == "{2}{B}{R}"
    assert model.unknowns == ([note] if note else [])


def test_only_our_commander_counts_whatever_the_snapshot_shape():
    from tests.strategic_states import BRAWL_COMMANDER_SAVES

    # The shared command zone also holds the opponent's commander (theirs: never cast by us).
    assert [c.name for c in _model(BRAWL_COMMANDER_SAVES).commanders] == ["Prosper, Tome-Bound"]
    # Mac bridge snapshots: no seats on command-zone cards; the players' commander_ids decide.
    bridge = deepcopy(BRAWL_COMMANDER_SAVES)
    for entry in bridge["command"]:
        entry.pop("owner_seat_id"), entry.pop("controller_seat_id")
    bridge["commander_grp_ids"] = []
    bridge["players"][0]["commander_ids"] = [301]
    bridge["players"][1]["commander_ids"] = [302]
    assert [c.name for c in _model(bridge).commanders] == ["Prosper, Tome-Bound"]
    # Without any designation, an unseated card is nobody's commander.
    for player in bridge["players"]:
        player.pop("commander_ids")
    assert _model(bridge).commanders == ()
    # Emblems and dungeons in our command zone are never cast.
    emblem = deepcopy(BRAWL_COMMANDER_SAVES)
    emblem["commander_grp_ids"] = []
    emblem["command"] += [
        {"instance_id": 310, "name": "Emblem", "type_line": "Emblem — Ring", "owner_seat_id": 1, "object_kind": "EMBLEM"},
        {"instance_id": 311, "name": "Undercity", "type_line": "Dungeon", "owner_seat_id": 1, "object_kind": "CARD"},
    ]  # fmt: skip
    assert [c.name for c in _model(emblem).commanders] == ["Prosper, Tome-Bound"]


def test_a_commander_without_card_data_uses_arenas_cost_and_says_its_text_is_unknown():
    from tests.strategic_states import BRAWL_COMMANDER_SAVES

    state = deepcopy(BRAWL_COMMANDER_SAVES)
    # What the snapshot holds when the card database misses a (digital-only) commander.
    state["command"][0] = {
        "instance_id": 301, "grp_id": 81845, "name": "Unknown (81845)", "type_line": "", "mana_cost": "",
        "oracle_text": "", "owner_seat_id": 1, "controller_seat_id": 1, "power": 1, "toughness": 4,
        "card_types": ["CardType_Creature"], "object_kind": "CARD",
    }  # fmt: skip
    state["commander_casts"] = {81845: 1}
    state["legal_actions_raw"] = [
        {"actionType": "ActionType_Cast", "grpId": 81845, "instanceId": 301,
         "manaCost": [{"color": ["ManaColor_Generic"], "count": 4}, {"color": ["ManaColor_Black"], "count": 1},
                      {"color": ["ManaColor_Red"], "count": 1}]},
    ]  # fmt: skip
    model = _model(state)
    (commander,) = model.commanders
    assert (commander.cost, commander.printed, commander.text_unknown) == ("{4}{B}{R}", "{2}{B}{R}", True)
    assert commander.card["_card_unknown"] and commander.card["mana_cost"] == "{2}{B}{R}"
    assert state["command"][0]["mana_cost"] == ""  # a copy: the snapshot is left alone
    assert _commander(model, "Unknown (81845)").mana_value == 6
    assert model.unknowns == ["our commander Unknown (81845): rules text unknown"]
    # No cast action either (their turn): its cost is unknown, so the lines never cast it.
    del state["legal_actions_raw"]
    model = _model(state)
    assert model.commanders[0].cost == "" and all(s.zone == "hand" for s in model.spells)
    assert model.unknowns == [
        "our commander Unknown (81845): cost unknown (not cast in the lines)",
        "our commander Unknown (81845): rules text unknown",
    ]


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_boards_without_a_command_zone_have_no_commander(name):
    model = _model(FIXTURES[name])
    assert model.commanders == () and {s.zone for s in model.spells} <= {"hand"}
