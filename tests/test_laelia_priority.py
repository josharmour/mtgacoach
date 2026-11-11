from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import build_pending_decision
from arenamcp.deck_strategy import analysis_reference
from arenamcp.laelia_strategy import priority_note


def state():
    return {
        "local_seat_id": 2,
        "commander_grp_ids": [83081],
        "deck_cards": [65385] * 91 + [71491, 91109, 98652, 87291, 87300, 87325, 87443, 87301],
        "turn": {"active_player": 2, "priority_player": 2, "phase": "Main1"},
        "command": [
            {
                "instance_id": 690,
                "grp_id": 83081,
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "name": "Laelia, the Blade Reforged",
            }
        ],
        "hand": [{"instance_id": 537, "grp_id": 87291, "owner_seat_id": 2, "name": "Daring Discovery"}],
    }


def menu(*actions):
    return build_pending_decision(
        {"has_pending": True, "request_type": "ActionsAvailable", "can_pass": True, "actions": list(actions)},
        resolve_name=lambda gid: str(gid),
    )


def cast(gid, iid, payable=True, **extra):
    return dict(actionType="ActionType_Cast", instanceId=iid, grpId=gid, hasAutoTap=payable, **extra)


def selected(monkeypatch, decision, game, reasoning):
    planner = ActionPlanner(backend=Mock())

    def passed(*args):
        planner._last_decision_trace = {}
        planner._last_decision_reasoning = reasoning
        return ["pass"]

    monkeypatch.setattr(planner, "_plan_decision_options", passed)
    return planner.plan_decision_options(decision, game)


def test_windows_payable_recast_overrides_inferred_tax(monkeypatch):
    game = state()
    decision = menu(cast(87291, 537), cast(83081, 690))
    reason = "With no creatures and only 6 mana (Laelia's taxed recast is 7), hold Discovery."
    assert selected(monkeypatch, decision, game, reason) == ["idx:1"]
    assert selected(monkeypatch, menu(cast(83081, 690, False)), game, reason) == ["pass"]
    assert selected(monkeypatch, decision, game, "Laelia is payable but hold up removal.") == ["pass"]


@pytest.mark.parametrize(
    "gid,name", [(87291, "Daring Discovery"), (87301, "Geological Appraiser"), (87300, "Etali's Favor")]
)
def test_exile_through_mountains_precombat_only(monkeypatch, gid, name):
    game = state()
    game["battlefield"] = game.pop("command")
    game["hand"][0].update(grp_id=gid, name=name)
    game["command"] = []
    decision = menu(cast(gid, 537, targets=[{"targets": [{"targetInstanceId": 690}]}]))
    assert "each card exiled" in priority_note(decision, game)
    reason = "Pass because discover would only hit Mountains."
    assert selected(monkeypatch, decision, game, reason) == ["idx:0"]
    game["turn"]["phase"] = "Main2"
    assert selected(monkeypatch, decision, game, reason) == ["pass"]


def test_no_override_for_other_deck_or_unavailable_aura_target(monkeypatch):
    game = state()
    game["battlefield"] = game.pop("command")
    game["hand"][0].update(grp_id=87300, name="Etali's Favor")
    reason = "Discover would only hit Mountains."
    assert selected(monkeypatch, menu(cast(87300, 537)), game, reason) == ["pass"]
    game["deck_cards"].pop()
    decision = menu(cast(87300, 537, targets=[{"targets": [{"targetInstanceId": 690}]}]))
    assert not priority_note(decision, game)
    assert selected(monkeypatch, decision, game, reason) == ["pass"]


def test_analysis_counts_land_copies_not_distinct_names():
    game = state()
    catalog = {
        gid: {"name": str(gid), "type_line": "Land" if gid in {65385, 87443} else "Creature"}
        for gid in game["deck_cards"]
    }
    assert "99 cards, 92 lands (92.9%), 7 nonlands" in analysis_reference(game, catalog)


def test_group_with_generic_source_and_partial_hand_uses_offered_instances(monkeypatch):
    game = state()
    game["hand"] = []
    ids = list(range(739, 746))
    names = dict.fromkeys(ids, "Mountain")
    names[742] = "Geological Appraiser"
    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "Group",
            "source_card": "opening hand",
            "group_instance_ids": ids,
            "group_specs": [
                {"zoneType": "Hand", "subZoneType": "Top"},
                {"zoneType": "Library", "subZoneType": "Bottom", "lowerBound": 4},
            ],
        },
        resolve_instance=names.get,
    )
    planner = ActionPlanner(backend=Mock())
    monkeypatch.setattr(
        planner, "_llm_decision_options", lambda *a: pytest.fail("London bottom sent to model")
    )
    selected_ids = planner.plan_decision_options(decision, game)
    assert len(selected_ids) == 4 and "grp:742" not in selected_ids
    assert planner.get_last_decision_trace()["policy"] == "mulligan_bottom"


def test_generated_false_claims_rejected_without_touching_saved_playbooks():
    from types import SimpleNamespace

    from arenamcp.deck_strategy import deck_identity
    from arenamcp.laelia_strategy import validate_generated_plan

    for claim in ["~10% land density", "Cascade revealing five cards is one trigger = one counter"]:
        book = SimpleNamespace(identity=deck_identity(state()), data={"primary_plan": claim})
        with pytest.raises(ValueError, match="92/99 lands"):
            validate_generated_plan(book)


def test_engine_submits_confirmed_recast_to_bridge(monkeypatch):
    from tests.fakes import FakeBridge, make_engine

    game = state()
    game.update(_bridge_connected=True, _bridge_has_pending=True, pending_decision="ActionsAvailable")
    poll = {
        "ok": True,
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "can_pass": True,
        "actions": [cast(87291, 537), cast(83081, 690)],
    }
    bridge = FakeBridge(poll)
    submit = Mock(return_value=True)
    monkeypatch.setattr(bridge, "submit_action_by_index", submit)
    planner = ActionPlanner(backend=Mock())

    def false_pass(*args):
        planner._last_decision_trace = {}
        planner._last_decision_reasoning = "With only 6 mana, Laelia's taxed recast costs 7."
        return ["pass"]

    monkeypatch.setattr(planner, "_plan_decision_options", false_pass)
    engine = make_engine(bridge, planner, game_state=game, post_action_delay=0.0)
    assert engine.process_trigger(game, "decision_required") is True
    assert submit.call_count == 1 and submit.call_args.args[0] == 1


def test_hidden_volcano_only_uses_confirmed_discover_activation(monkeypatch):
    game = state()
    game["battlefield"] = game.pop("command")
    option = {
        "actionType": "ActionType_Activate",
        "instanceId": 800,
        "grpId": 87443,
        "abilityGrpId": 450,
        "hasAutoTap": True,
    }
    monkeypatch.setattr(
        "arenamcp.decisions._default_ability_description", lambda _: ("", "Sacrifice this land: Discover 4.")
    )
    decision = menu(option)
    reason = "Passing because discover would only find Mountains."
    assert selected(monkeypatch, decision, game, reason) == ["idx:0"]
    monkeypatch.setattr(
        "arenamcp.decisions._default_ability_description", lambda _: ("", "Add one red mana.")
    )
    assert selected(monkeypatch, decision, game, reason) == ["pass"]
    game["stack"] = [{"name": "Discover 4"}]
    assert selected(monkeypatch, decision, game, reason) == ["pass"]


def test_mulligan_with_only_card_names_keeps_exact_deck_enabler():
    from arenamcp.mulligan_policy import mulligan_verdict, situation

    game = state()
    game["hand"] = [{"name": "Mountain", "type_line": "Basic Land"} for _ in range(6)]
    game["hand"].append({"name": "Hidden Volcano", "type_line": "Land"})
    assert mulligan_verdict(situation(game, 0))[0] == "keep"
    game["hand"][-1]["name"] = "Mountain"
    assert mulligan_verdict(situation(game, 0))[0] == "mulligan"
