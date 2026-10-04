"""Replay the commander/token trade in bug_20261004_094314 offline."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.commander_combat import commander_block_context
from arenamcp.gamestate import GameState

HOBBIT = "The Notary Hobbits"
RULES = (
    "When The Notary Hobbits enter, if they're not a token, create two tokens "
    "that are copies of them, except the tokens aren't legendary.\n"
    "{oT}: Add {oC} for each Halfling you control."
)


@pytest.fixture
def combat():
    state = {
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "turn": {"turn_number": 7, "active_player": 2, "priority_player": 1, "phase": "Combat"},
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 28, "commander_ids": [472]},
            {"seat_id": 2, "is_local": False, "life_total": 26, "commander_ids": [467]},
        ],
        "commander_casts": {103511: 1},
        "battlefield": [
            {
                "instance_id": iid,
                "grp_id": 103511,
                "name": HOBBIT,
                "oracle_text": RULES,
                "type_line": "Legendary Creature — Halfling Advisor",
                "mana_cost": "{3}{G}{G}",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "power": 1,
                "toughness": 1,
                "object_kind": "CARD" if iid == 472 else "TOKEN",
            }
            for iid in (472, 481, 482)
        ]
        + [
            {
                "instance_id": 495,
                "name": "Gnome",
                "power": 1,
                "toughness": 1,
                "type_line": "Artifact Creature — Gnome",
                "object_kind": "TOKEN",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "is_attacking": True,
            }
        ],
        "decision_context": {
            "type": "declare_blockers",
            "legal_blocker_ids": [472, 481, 482],
            "raw_blockers": [
                {"blockerInstanceId": iid, "attackerInstanceIds": [495]} for iid in (472, 481, 482)
            ],
        },
        "legal_actions": [f"Block with: {HOBBIT} #{i}" for i in (1, 2, 3)],
    }
    return state


def render(state):
    return "\n".join(commander_block_context(state, state["decision_context"]))


def test_report_block_prompt_distinguishes_recastable_original_and_permanent_token_loss(combat):
    planner = ActionPlanner(backend=Mock())
    prompt = planner._build_action_prompt(combat, "combat_blockers", combat["legal_actions"])
    assert f"{HOBBIT} [id:472] is YOUR COMMANDER" in prompt
    assert "printed {3}{G}{G} plus {2} commander tax" in prompt
    assert "*The Notary Hobbits [id:481], *The Notary Hobbits [id:482] would die" in prompt
    assert "prefer losing the commander over a token" in prompt
    assert "SURVIVING board after untapping" in prompt
    assert "Never count the dying commander as a source" in prompt
    assert RULES in prompt
    assert prompt.count("Commander recovery:") == 1
    # A concrete choice of the original must bind to 472, never a same-named token.
    plan = planner._parse_response(
        '{"action_type":"declare_blockers", "blocker_assignments":'
        '{"The Notary Hobbits [id:472]":"*Gnome [id:495]"}}',
        combat["legal_actions"],
        combat["decision_context"],
        game_state=combat,
    )
    assert plan.actions[0].blocker_instance_assignments == {472: 495}


@pytest.mark.parametrize("casts", [{"103511": 5}, {}])
def test_high_or_unknown_tax_is_not_presented_as_a_cheap_recast(combat, casts):
    combat["commander_casts"] = casts
    prompt = render(combat)
    assert ("plus {10} commander tax" if casts else "UNKNOWN commander tax") in prompt
    assert "if its next recast is affordable" in prompt
    assert "Preserve the commander when recasting is impractical" in prompt


@pytest.mark.parametrize("change", ["illegal", "not_commander", "token", "stolen"])
def test_only_the_owned_legal_original_gets_commander_recovery_value(combat, change):
    if change == "illegal":
        combat["decision_context"]["raw_blockers"] = combat["decision_context"]["raw_blockers"][1:]
    elif change == "not_commander":
        combat["players"][0]["commander_ids"] = []
    elif change == "token":
        combat["players"][0]["commander_ids"] = [481]
    else:
        combat["battlefield"][0]["owner_seat_id"] = 2
    assert render(combat) == ""


@pytest.mark.parametrize("change", ["survives", "unknown_stats", "different_legal_attacker"])
def test_no_death_comparison_when_original_does_not_die_or_cannot_make_the_same_block(combat, change):
    if change == "survives":
        combat["battlefield"][0]["toughness"] = 2
    elif change == "unknown_stats":
        combat["battlefield"][0]["toughness"] = None
    else:
        combat["decision_context"]["raw_blockers"][0]["attackerInstanceIds"] = [467]
    assert "Recovery comparison against" not in render(combat)


def designation(seat, tax, kind=1):
    return {
        "affectedIds": [seat],
        "type": ["AnnotationType_Designation"],
        "details": [
            {"key": key, "valueInt32": [value]}
            for key, value in (("grpid", 103511), ("CostIncrease", tax), ("DesignationType", kind))
        ],
    }


def test_observed_gre_tax_survives_reconnect_and_duplicate_designation_messages():
    gs = GameState()
    gs.local_seat_id = 1
    # No old command-zone instance is required: GRE published tax=2 after
    # changing commander instance 244 to 472, and publishes it on reconnects.
    ann = designation(1, 2)
    gs._process_annotations([ann, deepcopy(ann)])
    gs.publish_snapshot()
    assert gs.get_published_snapshot()["commander_casts"] == {103511: 1}
    gs._process_annotations([designation(1, 4)])
    assert gs.commander_casts == {103511: 2}


@pytest.mark.parametrize("seat,tax,kind", [(2, 6, 1), (1, 2, 9), (1, -2, 1), (1, 3, 1)])
def test_other_players_designations_and_invalid_taxes_do_not_replace_local_count(seat, tax, kind):
    gs = GameState()
    gs.local_seat_id = 1
    gs.commander_casts = {103511: 1}
    gs._process_annotations([designation(seat, tax, kind)])
    assert gs.commander_casts == {103511: 1}
