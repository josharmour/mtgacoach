"""Synthetic shape controls, not real-case yield evidence."""

import copy

import pytest

from arenamcp import benchmark_schema as s


def mana():
    return {
        "abilityGrpId": 1,
        "color": "ManaColor_Red",
        "count": 1,
        "manaId": 2,
        "srcInstanceId": 3,
        "specs": [],
    }


def action_structures():
    payment = {"mana": [mana()]}
    tap = {"abilityGrpId": 1, "instanceId": 3, "manaPaymentOption": payment}
    return {
        "manaCost": [{"color": ["ManaColor_Red"], "count": 1}],
        "autoTapActions": [tap],
        "autoTapSolution": {"autoTapActions": [tap]},
        "manaPaymentOptions": [payment],
        "manaSelections": [{"abilityGrpId": 1, "instanceId": 3, "options": [payment]}],
        "targets": [{"targetIdx": 0, "targets": [{"targetInstanceId": 4, "highlight": "Hot"}]}],
    }


@pytest.mark.parametrize("key", list(action_structures()))
def test_action_shapes_keep_numeric_graph_and_reject_unknown_nested_fields(key):
    original = action_structures()[key]
    assert s.extended_field("meta", key, original)
    mutated = copy.deepcopy(original)
    target = mutated[0] if isinstance(mutated, list) else mutated
    target["customer"] = "private"
    assert not s.extended_field("meta", key, mutated)
    assert original == action_structures()[key]


@pytest.mark.parametrize("field", ["abilityGrpId", "manaId", "srcInstanceId", "count"])
def test_mana_references_do_not_accept_identity_strings(field):
    obj = mana()
    obj[field] = "Private Account"
    assert not s.MANA(obj)


def test_controlled_state_and_zone_path_types():
    assert s.state_field("controlled_turn", {"opponent_controlled_by_you": False}, lambda _: False)
    assert not s.state_field("controlled_turn", {"opponent_controlled_by_you": "customer"}, lambda _: False)
    assert s.state_field(
        "zones", {"library_count": 40, "library_count_source": "log_zone_membership"}, lambda _: False
    )
    assert not s.state_field("zones", {"library_count_source": "Private Customer"}, lambda _: False)
    assert not s.state_field("zones", {"customer": 123}, lambda _: False)


def test_nullable_player_and_exact_designations():
    assert s.extended_field("player", "has_enduring_story", None)
    assert s.extended_field("player", "designations", ["Commander", "PlayerSpeed"])
    assert not s.extended_field("player", "designations", ["Private Customer"])
    assert not s.extended_field("player", "has_enduring_story", {})


def test_action_context_rejects_unbounded_prompt_and_wrong_family():
    ctx = {
        "type": "ActionsAvailable",
        "requestType": "ActionsAvailable",
        "requestClass": "ActionsAvailableRequest",
        "prompt": {"promptId": 1, "parameters": []},
        "sourceId": 2,
        "_bridge_source": True,
        "num_actions": 1,
        "action_types": ["ActionType_Pass"],
    }
    assert s.ACTION_CONTEXT(ctx)
    assert not s.ACTION_CONTEXT({**ctx, "customer": "private"})
    assert not s.ACTION_CONTEXT({**ctx, "requestType": "Private Customer"})
    assert not s.ACTION_CONTEXT({**ctx, "prompt": {"promptId": "Private Customer"}})
    assert not s.ACTION_CONTEXT({**ctx, "prompt": {"parameters": [{"customer": "private"}]}})


def test_card_faces_do_not_permit_identity_fields_or_string_ids():
    face = {
        "grp_id": 42,
        "name": "Fury",
        "type_line": "Creature",
        "oracle_text": "Double strike",
        "mana_cost": "{3}{R}{R}",
    }
    assert s.extended_field("card", "related_faces", [face])
    assert not s.extended_field("card", "related_faces", [{**face, "grp_id": "Private Customer"}])
    assert not s.extended_field("card", "related_faces", [{**face, "account_id": "secret"}])
    assert not s.extended_field("meta", "name", "Private Customer")
    assert not s.extended_field("meta", "source_id", "Private Customer")


def test_legal_labels_require_renderer_grammar_and_existing_card_names():
    state = {"hand": [{"name": "Fury"}]}
    assert s.legal_labels(
        ["Cast Fury [OK]", "Pass", "Action: ActivateMana", "Activate Ability: Fury [NEED:{1}{R}]"], state
    )
    assert not s.legal_labels(["Cast Private Customer [OK]"], state)
    assert not s.legal_labels(["Action: PrivateCustomer"], state)
    assert not s.legal_labels(["Cast Fury [PRIVATE]"], state)


def test_exact_production_context_alias_not_arbitrary_case_or_identity():
    assert s.ACTION_CONTEXT({"type": "actions_available"})
    assert not s.ACTION_CONTEXT({"type": "actions_available_PrivateCustomer"})


def test_legacy_mana_action_alias_preserved_across_structural_paths():
    assert s.ACTION("ActionType_Activate_Mana")
    assert s.ACTION_CONTEXT({"action_types": ["ActionType_Activate_Mana"]})
    assert s.extended_field("meta", "actionType", "ActionType_Activate_Mana")
    assert s.legal_labels(["Action: Activate_Mana"], {})
    assert not s.ACTION("ActionType_Activate_Mana_PrivateCustomer")


def test_source_autotap_land_projection_nullable_reference_and_empty_mana_only():
    for value, ok in [
        ([{"instanceId": 42, "mana": ""}], True),
        ([{"instanceId": None, "mana": ""}], True),
        ([{"instanceId": "Private Customer", "mana": ""}], False),
        ([{"instanceId": 42, "mana": "Private Customer"}], False),
        ([{"instanceId": 42, "mana": "", "customer": "secret"}], False),
    ]:
        assert s.extended_field("meta", "_autotap_lands", value) is ok


def test_native_action_aliases_and_stringified_cost_colors_are_bounded():
    assert s.ACTION("ActivateMana") and s.ACTION("ActionType_PlayMdfc")
    assert not s.ACTION("ActivateMana_PrivateCustomer")
    assert s.MANA_COST([{"color": '[ "Red", "Green" ]', "count": 1}])
    assert s.MANA_COST([{"color": ["ManaColor_AnyColor"], "count": 1}])
    assert not s.MANA_COST([{"color": '["Private Customer"]', "count": 1}])
    assert not s.MANA_COST([{"color": '{"account":"secret"}', "count": 1}])
    assert not s.MANA_COST([{"color": [{"account": "secret"}], "count": 1}])
