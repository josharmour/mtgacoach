"""Regression tests for the unresolved-grpId leak in issue #407 (MEDIUM
finding: "2 unresolved card grpIds leaked into prompts 12x ... the model
invents properties for them").

The grpIds involved are *ability* ids, not card ids. An ability on the stack
is its own GameObject whose grp_id is the ability id, which no card DB can
resolve — so it rendered as "Card#147886" / "Pay costs for Card#136588"
(the same symptom appears in issue #483's advice history). The parent
permanent is the thing the player recognises.
"""

from arenamcp.gamestate import GameObject, GameState
from arenamcp.gamestate_decisions import _handle_pay_costs

# Straight from bug_20260813_205508.json: Wolfwillow Haven (grp 70716) at
# instance 642 activating ability 136588, which lands as instance 647.
WOLFWILLOW_GRP = 70716
ABILITY_GRP = 136588


def _state_with_ability():
    gs = GameState()
    gs.local_seat_id = 1
    gs.game_objects[642] = GameObject(
        instance_id=642,
        grp_id=WOLFWILLOW_GRP,
        zone_id=28,
        owner_seat_id=1,
        controller_seat_id=1,
    )
    gs.game_objects[647] = GameObject(
        instance_id=647,
        grp_id=ABILITY_GRP,
        zone_id=27,
        owner_seat_id=1,
        controller_seat_id=1,
        parent_instance_id=642,
    )
    return gs


def test_ability_named_after_its_source_permanent():
    gs = _state_with_ability()
    assert gs._resolve_object_display_name(gs.game_objects[647]) == "Wolfwillow Haven's ability"


def test_ordinary_card_name_is_unchanged():
    gs = _state_with_ability()
    assert gs._resolve_object_display_name(gs.game_objects[642]) == "Wolfwillow Haven"


def test_unresolvable_with_no_parent_still_degrades_to_card_id():
    """Without a parent to follow there is nothing better to say — but it
    must not crash or invent a name."""
    gs = GameState()
    gs.local_seat_id = 1
    gs.game_objects[900] = GameObject(
        instance_id=900, grp_id=999999999, zone_id=27, owner_seat_id=1, controller_seat_id=1
    )
    assert gs._resolve_object_display_name(gs.game_objects[900]) == "Card#999999999"


def test_unresolvable_parent_does_not_fabricate():
    gs = GameState()
    gs.local_seat_id = 1
    gs.game_objects[800] = GameObject(
        instance_id=800, grp_id=999999998, zone_id=28, owner_seat_id=1, controller_seat_id=1
    )
    gs.game_objects[801] = GameObject(
        instance_id=801,
        grp_id=999999999,
        zone_id=27,
        owner_seat_id=1,
        controller_seat_id=1,
        parent_instance_id=800,
    )
    assert gs._resolve_object_display_name(gs.game_objects[801]) == "Card#999999999"


def test_pay_costs_context_names_the_source_card():
    """The reported advice was "Pay costs for Card#136588"."""
    gs = _state_with_ability()
    _handle_pay_costs(
        gs,
        {
            "payCostsReq": {
                "manaCost": [
                    {"color": ["ManaColor_Generic"], "count": 3, "objectId": 647, "abilityGrpId": ABILITY_GRP}
                ]
            }
        },
    )
    ctx = gs.decision_context
    assert ctx["type"] == "pay_costs"
    assert ctx["source_card"] == "Wolfwillow Haven's ability"
    assert "Card#" not in ctx["source_card"]


def test_pay_costs_legal_actions_have_no_card_id():
    """End of the chain: the prompt line the coach reads out."""
    from arenamcp.rules_engine import RulesEngine

    gs = _state_with_ability()
    _handle_pay_costs(
        gs,
        {"payCostsReq": {"manaCost": [{"color": ["ManaColor_Generic"], "count": 3, "objectId": 647}]}},
    )
    actions = RulesEngine._get_decision_actions({"decision_context": gs.decision_context})
    assert actions[0] == "Pay costs for Wolfwillow Haven's ability"
