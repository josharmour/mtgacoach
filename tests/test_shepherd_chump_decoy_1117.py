"""Regression tests for Allosaurus Shepherd chump-decoy attacks and PyPI/CI packaging."""

from __future__ import annotations

import copy
import pathlib

import pytest
import tomllib
import yaml

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.combat_strategy import losing_attack_prompt, losing_attackers

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCAL, OPP = 2, 1


def _turn_7_board(*, opp_life: int = 17, radagast_toughness: int = 5) -> dict:
    """Turn 7 board from match 402d53b6 (bug_20261117_153215)."""
    return {
        "match_id": "402d53b6-4523-4de7-bea1-323b3ff8e6c0",
        "turn": {
            "turn_number": 7,
            "active_player": LOCAL,
            "priority_player": LOCAL,
            "phase": "Combat",
            "step": "DeclareAttack",
        },
        "local_seat_id": LOCAL,
        "players": [
            {"seat_id": OPP, "life_total": opp_life, "is_local": False, "mana_pool": {}},
            {"seat_id": LOCAL, "life_total": 25, "is_local": True, "mana_pool": {}},
        ],
        "battlefield": [
            {
                "instance_id": 444,
                "name": "Copperline Gorge",
                "type_line": "Land",
                "card_types": ["Land"],
                "owner_seat_id": LOCAL,
                "controller_seat_id": LOCAL,
                "is_tapped": True,
            },
            {
                "instance_id": 450,
                "name": "Gemstone Caverns",
                "type_line": "Legendary Land",
                "card_types": ["Land"],
                "owner_seat_id": LOCAL,
                "controller_seat_id": LOCAL,
                "is_tapped": True,
            },
            {
                "instance_id": 463,
                "name": "Mountain",
                "type_line": "Basic Land — Mountain",
                "card_types": ["Land"],
                "owner_seat_id": LOCAL,
                "controller_seat_id": LOCAL,
                "is_tapped": True,
            },
            {
                "instance_id": 485,
                "name": "Multiversal Passage",
                "type_line": "Land",
                "card_types": ["Land"],
                "owner_seat_id": LOCAL,
                "controller_seat_id": LOCAL,
                "is_tapped": True,
            },
            {
                "instance_id": 445,
                "name": "Ragavan, Nimble Pilferer",
                "oracle_text": (
                    "Whenever Ragavan deals combat damage to a player, create a Treasure token "
                    "and exile the top card of that player's library. Until end of turn, you "
                    "may cast that card.\nDash {o1oR}"
                ),
                "type_line": "Legendary Creature — Monkey Pirate",
                "card_types": ["Creature"],
                "subtypes": ["Monkey", "Pirate"],
                "owner_seat_id": LOCAL,
                "controller_seat_id": LOCAL,
                "power": 2,
                "toughness": 1,
                "is_tapped": False,
                "turn_entered_battlefield": 3,
            },
            {
                "instance_id": 451,
                "name": "Allosaurus Shepherd",
                "oracle_text": (
                    "This spell can't be countered.\n"
                    "Green spells you control can't be countered.\n"
                    "{o4oGoG}: Until end of turn, each Elf creature you control has base "
                    "power and toughness 5/5 and becomes a Dinosaur in addition to its other "
                    "creature types."
                ),
                "type_line": "Creature — Elf Shaman",
                "card_types": ["Creature"],
                "subtypes": ["Elf", "Shaman"],
                "owner_seat_id": LOCAL,
                "controller_seat_id": LOCAL,
                "power": 1,
                "toughness": 1,
                "is_tapped": False,
                "turn_entered_battlefield": 4,
            },
            {
                "instance_id": 464,
                "name": "Minsc & Boo, Timeless Heroes",
                "oracle_text": (
                    "When Minsc & Boo enters and at the beginning of your upkeep, you may "
                    "create Boo, a legendary 1/1 red Hamster creature token with trample and haste."
                ),
                "type_line": "Legendary Planeswalker — Minsc",
                "card_types": ["Planeswalker"],
                "owner_seat_id": LOCAL,
                "controller_seat_id": LOCAL,
                "counters": {"Loyalty": 3},
                "is_tapped": False,
            },
            {
                "instance_id": 471,
                "name": "Boo",
                "oracle_text": "Trample\nHaste",
                "type_line": "Token Legendary Creature — Hamster",
                "card_types": ["Creature"],
                "subtypes": ["Hamster"],
                "object_kind": "TOKEN",
                "owner_seat_id": LOCAL,
                "controller_seat_id": LOCAL,
                "power": 7,
                "toughness": 7,
                "keywords": ["trample", "haste"],
                "counters": {"P1P1": 6},
                "is_tapped": False,
                "turn_entered_battlefield": 5,
            },
            {
                "instance_id": 486,
                "name": "Vibrance",
                "oracle_text": "",
                "type_line": "Creature — Elemental Incarnation",
                "card_types": ["Creature"],
                "owner_seat_id": LOCAL,
                "controller_seat_id": LOCAL,
                "power": 4,
                "toughness": 4,
                "is_tapped": False,
                "summoning_sickness": True,
                "turn_entered_battlefield": 7,
            },
            {
                "instance_id": 448,
                "name": "Forest",
                "type_line": "Basic Land Creature — Forest",
                "card_types": ["Creature", "Land"],
                "owner_seat_id": OPP,
                "controller_seat_id": OPP,
                "power": 1,
                "toughness": 1,
                "keywords": ["haste"],
                "counters": {"P1P1": 1},
                "is_tapped": True,
            },
            {
                "instance_id": 457,
                "name": "Forest",
                "type_line": "Basic Land — Forest",
                "card_types": ["Land"],
                "owner_seat_id": OPP,
                "controller_seat_id": OPP,
                "is_tapped": True,
            },
            {
                "instance_id": 477,
                "name": "Forest",
                "type_line": "Basic Land — Forest",
                "card_types": ["Land"],
                "owner_seat_id": OPP,
                "controller_seat_id": OPP,
                "is_tapped": True,
            },
            {
                "instance_id": 478,
                "name": "Radagast of Rhosgobel",
                "oracle_text": (
                    "The first creature spell you cast each turn costs {o2} less to cast "
                    "and can be cast as though it had flash."
                ),
                "type_line": "Legendary Creature — Avatar Wizard",
                "card_types": ["Creature"],
                "owner_seat_id": OPP,
                "controller_seat_id": OPP,
                "power": 2,
                "toughness": radagast_toughness,
                "is_tapped": False,
                "summoning_sickness": True,
                "turn_entered_battlefield": 6,
            },
        ],
        "hand": [],
        "graveyard": [],
        "recent_events": [],
        "decision_context": {
            "type": "declare_attackers",
            "legal_attackers": ["Ragavan, Nimble Pilferer", "Allosaurus Shepherd", "Boo"],
            "legal_attacker_ids": [445, 451, 471],
            "raw_attackers": [
                {
                    "attackerInstanceId": iid,
                    "mustAttack": False,
                    "legalDamageRecipients": [{"type": "Player", "playerSystemSeatId": OPP}],
                    "selectedDamageRecipient": None,
                }
                for iid in (445, 451, 471)
            ],
        },
    }


@pytest.mark.parametrize("line_mode", ["live", "off", "peel_bug"])
def test_planner_holds_ragavan_and_shepherd_only_attacks_with_boo(line_mode, monkeypatch):
    if line_mode == "off":
        monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    elif line_mode == "peel_bug":
        monkeypatch.setattr(
            "arenamcp.combat_strategy._line_search_attackers",
            lambda _state: frozenset({451, 471}),
        )

    state = _turn_7_board()
    names = ["Ragavan, Nimble Pilferer", "Allosaurus Shepherd", "Boo"]
    plan = ActionPlan(
        actions=[
            GameAction(
                ActionType.DECLARE_ATTACKERS,
                attacker_names=list(names),
                attacker_instance_ids=[445, 451, 471],
                attacker_targets=dict.fromkeys(names, "Opponent"),
            )
        ],
        overall_strategy="Radagast is tapped and can't block; all 10 damage gets through.",
    )
    ActionPlanner.__new__(ActionPlanner)._check_losing_attacks(plan, state, state["decision_context"])

    assert plan.actions[0].attacker_instance_ids == [471]
    assert plan.actions[0].attacker_names == ["Boo"]
    assert plan.fallback_reason == "planner_losing_attack"


def test_shepherd_and_prompt_both_flag_the_two_fodder_attackers():
    state = _turn_7_board()
    assert set(losing_attackers(state, [451, 471])) == {451}

    prompt = losing_attack_prompt(state, state["decision_context"])
    assert "Attacker [445]" in prompt
    assert "Attacker [451]" in prompt
    assert "Attacker [471]" not in prompt


def test_controls_keep_lethal_or_genuine_surviving_blocker_shields():
    # Opposite control 1: at 8 opponent life, Ragavan + Shepherd + Boo is lethal through blocks.
    lethal_state = _turn_7_board(opp_life=8)
    assert losing_attackers(lethal_state, [445, 451, 471]) == {}

    # Opposite control 2: when Radagast 2/8 survives blocking Boo 7/7, Shepherd 1/1 buys 7 real
    # trample damage without sparing a doomed blocker, so Shepherd + Boo both keep attacking.
    survives_state = _turn_7_board(radagast_toughness=8)
    assert losing_attackers(survives_state, [451, 471]) == {}

    # Opposite control 3: a 1/1 shielding a 3/1 past a 2/2 does not give a net free blocker kill
    # on rest ([3/1] would trade, material 6 vs 6), so sending [3/1, 1/1] is kept.
    trade_shield = copy.deepcopy(_turn_7_board())
    trade_shield["battlefield"] = [
        {
            "instance_id": 10,
            "name": "Mountain",
            "type_line": "Land",
            "card_types": ["Land"],
            "owner_seat_id": LOCAL,
            "controller_seat_id": LOCAL,
            "is_tapped": True,
        },
        {
            "instance_id": 11,
            "name": "Forest",
            "type_line": "Land",
            "card_types": ["Land"],
            "owner_seat_id": OPP,
            "controller_seat_id": OPP,
            "is_tapped": True,
        },
        {
            "instance_id": 101,
            "name": "Vanguard",
            "type_line": "Creature",
            "card_types": ["Creature"],
            "owner_seat_id": LOCAL,
            "controller_seat_id": LOCAL,
            "power": 3,
            "toughness": 1,
            "is_tapped": False,
            "turn_entered_battlefield": 3,
        },
        {
            "instance_id": 102,
            "name": "Squire",
            "type_line": "Creature",
            "card_types": ["Creature"],
            "owner_seat_id": LOCAL,
            "controller_seat_id": LOCAL,
            "power": 1,
            "toughness": 1,
            "is_tapped": False,
            "turn_entered_battlefield": 3,
        },
        {
            "instance_id": 201,
            "name": "Bear",
            "type_line": "Creature",
            "card_types": ["Creature"],
            "owner_seat_id": OPP,
            "controller_seat_id": OPP,
            "power": 2,
            "toughness": 2,
            "is_tapped": False,
            "turn_entered_battlefield": 4,
        },
    ]
    assert losing_attackers(trade_shield, [101, 102]) == {}


def test_costly_attacker_in_line_attack_and_cross_fight_trade_cases(monkeypatch):
    # Reviewer finding #1: `can_unmask` + `backed` must not drop a costly attacker that line_search kept.
    board = copy.deepcopy(_turn_7_board())
    board["players"] = [
        {"seat_id": OPP, "life_total": 20, "is_local": False, "mana_pool": {}},
        {"seat_id": LOCAL, "life_total": 20, "is_local": True, "mana_pool": {}},
    ]
    board["battlefield"] = [
        {
            "instance_id": 10,
            "name": "Mountain",
            "type_line": "Land",
            "card_types": ["Land"],
            "owner_seat_id": LOCAL,
            "controller_seat_id": LOCAL,
            "is_tapped": True,
        },
        {
            "instance_id": 11,
            "name": "Forest",
            "type_line": "Land",
            "card_types": ["Land"],
            "owner_seat_id": OPP,
            "controller_seat_id": OPP,
            "is_tapped": True,
        },
        {
            "instance_id": 1,
            "name": "Boo",
            "type_line": "Creature",
            "card_types": ["Creature"],
            "owner_seat_id": LOCAL,
            "controller_seat_id": LOCAL,
            "power": 7,
            "toughness": 7,
            "keywords": ["trample"],
            "is_tapped": False,
            "turn_entered_battlefield": 3,
        },
        {
            "instance_id": 2,
            "name": "Lizard",
            "type_line": "Creature",
            "card_types": ["Creature"],
            "owner_seat_id": LOCAL,
            "controller_seat_id": LOCAL,
            "power": 3,
            "toughness": 1,
            "is_tapped": False,
            "turn_entered_battlefield": 3,
        },
        {
            "instance_id": 3,
            "name": "Goblin",
            "type_line": "Creature",
            "card_types": ["Creature"],
            "owner_seat_id": LOCAL,
            "controller_seat_id": LOCAL,
            "power": 2,
            "toughness": 1,
            "is_tapped": False,
            "turn_entered_battlefield": 3,
        },
        {
            "instance_id": 20,
            "name": "Wall",
            "type_line": "Creature",
            "card_types": ["Creature"],
            "owner_seat_id": OPP,
            "controller_seat_id": OPP,
            "power": 2,
            "toughness": 8,
            "is_tapped": False,
            "turn_entered_battlefield": 4,
        },
        {
            "instance_id": 21,
            "name": "Sentry",
            "type_line": "Creature",
            "card_types": ["Creature"],
            "owner_seat_id": OPP,
            "controller_seat_id": OPP,
            "power": 1,
            "toughness": 4,
            "is_tapped": False,
            "turn_entered_battlefield": 4,
        },
    ]
    monkeypatch.setattr(
        "arenamcp.combat_strategy._line_search_attackers",
        lambda _state: frozenset({1, 2}),
    )
    assert 2 not in losing_attackers(board, [1, 2, 3])

    # Reviewer finding #2: an unrelated kept attacker trading across fights must not cancel Boo's chump kill on Radagast.
    cross_fight = copy.deepcopy(_turn_7_board(opp_life=15))
    cross_fight["battlefield"] = [
        {
            "instance_id": 10,
            "name": "Mountain",
            "type_line": "Land",
            "card_types": ["Land"],
            "owner_seat_id": LOCAL,
            "controller_seat_id": LOCAL,
            "is_tapped": True,
        },
        {
            "instance_id": 11,
            "name": "Forest",
            "type_line": "Land",
            "card_types": ["Land"],
            "owner_seat_id": OPP,
            "controller_seat_id": OPP,
            "is_tapped": True,
        },
        {
            "instance_id": 1,
            "name": "Boo",
            "type_line": "Creature",
            "card_types": ["Creature"],
            "owner_seat_id": LOCAL,
            "controller_seat_id": LOCAL,
            "power": 7,
            "toughness": 7,
            "keywords": ["trample"],
            "is_tapped": False,
            "turn_entered_battlefield": 3,
        },
        {
            "instance_id": 2,
            "name": "Allosaurus Shepherd",
            "type_line": "Creature",
            "card_types": ["Creature"],
            "owner_seat_id": LOCAL,
            "controller_seat_id": LOCAL,
            "power": 1,
            "toughness": 1,
            "is_tapped": False,
            "turn_entered_battlefield": 3,
        },
        {
            "instance_id": 3,
            "name": "DrawAttacker",
            "oracle_text": "Whenever this creature attacks, draw a card.",
            "type_line": "Creature",
            "card_types": ["Creature"],
            "owner_seat_id": LOCAL,
            "controller_seat_id": LOCAL,
            "power": 5,
            "toughness": 5,
            "is_tapped": False,
            "turn_entered_battlefield": 3,
        },
        {
            "instance_id": 20,
            "name": "Radagast",
            "type_line": "Creature",
            "card_types": ["Creature"],
            "owner_seat_id": OPP,
            "controller_seat_id": OPP,
            "power": 2,
            "toughness": 5,
            "is_tapped": False,
            "turn_entered_battlefield": 4,
        },
        {
            "instance_id": 21,
            "name": "Colossus",
            "type_line": "Creature",
            "card_types": ["Creature"],
            "owner_seat_id": OPP,
            "controller_seat_id": OPP,
            "power": 5,
            "toughness": 6,
            "is_tapped": False,
            "turn_entered_battlefield": 4,
        },
    ]
    assert set(losing_attackers(cross_fight, [1, 2, 3])) == {2}


def test_pyproject_sdist_includes_all_wheel_force_includes():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    hatch_targets = pyproject["tool"]["hatch"]["build"]["targets"]
    wheel_includes = hatch_targets["wheel"].get("force-include", {})
    sdist_includes = hatch_targets["sdist"].get("force-include", {})

    assert wheel_includes, "Expected wheel force-include entries"
    for src_rel in wheel_includes:
        assert (REPO_ROOT / src_rel).is_file(), f"Missing force-include source: {src_rel}"
        assert sdist_includes.get(src_rel) == src_rel, (
            f"Wheel force-include {src_rel!r} must be included in sdist so `python -m build` succeeds"
        )


def test_github_workflows_pin_osx_arm64_to_macos15_and_opt_in_node24():
    workflows_dir = REPO_ROOT / ".github" / "workflows"
    expected = [
        "tests.yml",
        "managed-desktop.yml",
        "managed-platform.yml",
        "publish-pypi.yml",
        "installer.yml",
        "retry-unacquired-runners.yml",
    ]
    for name in expected:
        data = yaml.safe_load((workflows_dir / name).read_text(encoding="utf-8"))
        assert str(data.get("env", {}).get("FORCE_JAVASCRIPT_ACTIONS_TO_NODE24", "")).lower() == "true", (
            f"{name} must set FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true"
        )

    desktop = yaml.safe_load((workflows_dir / "managed-desktop.yml").read_text(encoding="utf-8"))
    matrix = desktop["jobs"]["build"]["strategy"]["matrix"]["include"]
    osx_entries = [entry for entry in matrix if entry["rid"] == "osx-arm64"]
    assert osx_entries == [{"os": "macos-15", "rid": "osx-arm64"}]

    retry = yaml.safe_load((workflows_dir / "retry-unacquired-runners.yml").read_text(encoding="utf-8"))
    assert retry["permissions"]["checks"] == "read"
    retry_trigger = retry.get("on") or retry.get(True)
    watched = retry_trigger["workflow_run"]["workflows"]
    assert "Managed desktop releases" in watched
    assert "publish-pypi" in watched
    assert "tests" not in watched
