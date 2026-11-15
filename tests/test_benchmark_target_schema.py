"""Synthetic target requests covering source identity, option order and privacy boundaries."""

from copy import deepcopy

import pytest
from tools.decision_benchmark import UnsafeCase, assert_case_schema, sanitize_semantics, typed_decision

from arenamcp.benchmark_schema import target_context


def synthetic_request(role):
    # These fictional cards and identifiers are constructed for this test.
    source_id = 83005 if role == "spell" else 83003
    origin_id = source_id if role == "spell" else 83001
    labels = ("Fixture Amber Gear", "Fixture Violet Gear", "Fixture Silver Gear")
    target_ids = [94633, 91826, 9918261]
    shades = ("Cold", "Tepid", "Hot")
    pieces = [
        {"instance_id": iid, "grp_id": 810001 + i, "object_kind": "CARD", "name": name}
        for i, (iid, name) in enumerate(zip(target_ids, labels, strict=True))
    ]
    source = {
        "instance_id": source_id,
        "grp_id": 820009,
        "object_kind": "CARD",
        "name": "Fixture Dial",
        "oracle_text": "Untap target artifact.",
    }
    origin = source
    extra = {}
    if role == "ability":
        origin = {
            "instance_id": origin_id,
            "grp_id": 820010,
            "object_kind": "CARD",
            "name": "Fixture Switch",
            "oracle_text": "{T}: Untap target artifact.",
        }
        pieces.append(origin)
        source.update(
            grp_id=821772,
            object_kind="ABILITY",
            name="Fixture Switch test ability",
            parent_instance_id=origin_id,
        )
        extra = {"source_parent_instance_id": origin_id, "source_card_oracle_text": origin["oracle_text"]}
    origin["ability_grp_ids"] = [821772]
    candidates = [
        {"targetInstanceId": iid, "legalAction": "SelectAction_Select", "highlight": "HighlightType_" + shade}
        for iid, shade in zip(target_ids, shades, strict=True)
    ]
    slot = {
        "targetIdx": 1,
        "minTargets": 1,
        "maxTargets": 1,
        "targets": candidates,
        "targetingPlayer": 2,
        "targetingAbilityGrpId": 821772,
        "prompt": {
            "promptId": 1330,
            "parameters": [
                {"parameterName": "CardId", "type": "ParameterType_Number", "numberValue": origin_id}
            ],
        },
    }
    raw = {"sourceId": source_id, "abilityGrpId": source["grp_id"], "targets": [slot]}
    context = {
        **deepcopy(raw),
        **extra,
        "type": "target_selection",
        "source_id": source_id,
        "source_card": origin["name"],
        "source_oracle_text": source["oracle_text"],
        "raw": raw,
        "requestType": "SelectTargets",
        "requestClass": "SelectTargetsRequest",
        "prompt": {"promptId": 10, "parameters": []},
        "_bridge_source": True,
    }
    state = {
        "_bridge_request_type": "SelectTargets",
        "_bridge_request_class": "SelectTargetsRequest",
        "_bridge_can_cancel": True,
        "_bridge_can_pass": False,
        "decision_context": context,
        "local_seat_id": 2,
        "decision_seat_id": 2,
        "battlefield": pieces,
        "stack": [source],
    }
    options = [
        {
            "option_id": f"tgt:{iid}",
            "label": name,
            "payable": None,
            "meta": {"instanceId": iid, "targetIdx": 1, "grpId": 0, "highlight": shade},
        }
        for iid, name, shade in zip(target_ids, labels, shades, strict=True)
    ]
    pending = {
        "request_id": [850030, 921007],
        "request_type": "SelectTargets",
        "min_select": 1,
        "max_select": 1,
        "min_weight": None,
        "max_weight": None,
        "can_pass": False,
        "can_cancel": True,
        "options": options,
        "source_label": source["name"],
        "slots": [
            {"target_idx": 1, "min_targets": 1, "max_targets": 1, "selected": 0, "candidate_ids": target_ids}
        ],
    }
    return state, pending


@pytest.mark.parametrize("role", ["ability", "spell"])
def test_public_target_request_keeps_exact_options_and_slot(role):
    state, pending = synthetic_request(role)
    snapshot = {"version": 1, "state": state}
    assert_case_schema(snapshot, pending)
    assert sanitize_semantics(snapshot, set()) == snapshot
    assert sanitize_semantics(pending, set()) == pending
    decision = typed_decision(pending)
    assert [o.option_id for o in decision.options] == ["tgt:94633", "tgt:91826", "tgt:9918261"]
    assert list(decision.slots[0].candidate_ids) == [94633, 91826, 9918261]
    assert all(decision.selection_is_valid([o.option_id]) for o in decision.options)
    assert not decision.selection_is_valid(["tgt:94633", "tgt:91826", "tgt:9918261"])
    with pytest.raises(UnsafeCase):
        sanitize_semantics(snapshot, {"Fixture Violet Gear"})


@pytest.mark.parametrize(
    "path,value",
    [
        ("state._bridge_request_type", "Mulligan"),
        ("state._bridge_request_class", "SelectNRequest"),
        ("state._bridge_can_cancel", False),
        ("state._bridge_can_pass", True),
        ("pending.can_cancel", False),
        ("pending.can_pass", True),
        ("state.decision_seat_id", 1),
        ("state.decision_context._bridge_source", 1),
        ("state.decision_context.sourceId", True),
        ("state.decision_context.source_oracle_text", "private text"),
        ("state.decision_context.source_parent_instance_id", 91826),
        ("state.decision_context.source_card", "Other"),
        ("state.decision_context.source_card_oracle_text", "private text"),
        ("state.decision_context.targets.0.targetingPlayer", 1),
        ("state.decision_context.targets.0.prompt.parameters.0.numberValue", "94633"),
        ("state.decision_context.raw.targets.0.targetIdx", True),
        ("state.decision_context.raw.targets.0.targets.0.legalAction", "SelectAction_Deselect"),
        ("state.decision_context.prompt.parameters", [{"parameterName": "Player", "value": "private text"}]),
        ("pending.request_type", "OptionalAction"),
        ("pending.source_label", "private name"),
        ("pending.prompt_text", "unrelated request canary"),
        ("pending.source_oracle_text", "unrelated rules canary"),
        ("pending.source_instance_id", 988000),
        ("pending.selection_context", "unrelated context canary"),
        ("pending.request_id", [True, 42000]),
        ("pending.request_id", [12000, "7"]),
        ("pending.options.0.meta.targetIdx", True),
        ("pending.options.0.label", "private text"),
        ("pending.options.1.meta.highlight", "None"),
        ("pending.slots.0.candidate_ids", [93041, 91826, 94633]),
        ("pending.min_select", True),
        ("pending.max_select", 2),
        ("pending.slots.0.selected", 1),
        ("pending.options.1.option_id", "tgt:94633"),
    ],
)
def test_cross_request_menu_drift_and_free_text_fail_closed(path, value):
    state, pending = synthetic_request("ability")
    node = {"state": state, "pending": pending}
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    node[parts[-1]] = value
    assert not target_context(state, pending)
    with pytest.raises(UnsafeCase):
        assert_case_schema({"version": 1, "state": state}, pending)


@pytest.mark.parametrize(
    "change", ["hidden", "facedown", "private_zone", "duplicate", "missing_parent", "missing_context"]
)
def test_target_objects_must_be_unambiguous_public_prints(change):
    state, pending = synthetic_request("ability")
    card = next(c for c in state["battlefield"] if c["instance_id"] == 91826)
    if change == "hidden":
        card["visibility"] = "private"
    elif change == "facedown":
        card["face_down"] = True
    elif change == "private_zone":
        state["battlefield"].remove(card)
        state["hand"] = [card]
    elif change == "duplicate":
        state["graveyard"] = [card]
    elif change == "missing_parent":
        state["battlefield"] = [c for c in state["battlefield"] if c["instance_id"] != 83001]
    else:
        del state["decision_context"]
    with pytest.raises(UnsafeCase):
        assert_case_schema({"version": 1, "state": state}, pending)


@pytest.mark.parametrize("role", ["ability", "spell"])
@pytest.mark.parametrize("location", ["hand", "pending_selection_objects", "zones"])
def test_conflicting_private_or_nested_instance_cannot_witness_a_target(role, location):
    state, pending = synthetic_request(role)
    copy = dict(state["battlefield"][0])
    state[location] = {"local_graveyard": [copy]} if location == "zones" else [copy]
    assert not target_context(state, pending)


@pytest.mark.parametrize("witness", ["source", "parent", "target"])
def test_face_down_print_needs_no_visibility_flag(witness):
    state, pending = synthetic_request("ability")
    card = {
        "source": state["stack"][0],
        "parent": state["battlefield"][-1],
        "target": state["battlefield"][0],
    }[witness]
    card["grp_id"] = 3
    assert not target_context(state, pending)


def test_battlefield_ability_object_cannot_supply_a_card_target():
    state, pending = synthetic_request("ability")
    state["battlefield"][0]["object_kind"] = "ABILITY"
    assert not target_context(state, pending)


@pytest.mark.parametrize("role", ["ability", "spell"])
@pytest.mark.parametrize("drift", ["source_group", "targeting_group", "exiled_candidate", "nonstack_source"])
def test_native_groups_and_public_zones_require_independent_support(role, drift):
    state, pending = synthetic_request(role)
    if drift in {"source_group", "targeting_group"}:
        ctx = state["decision_context"]
        for mirror in (ctx, ctx["raw"]):
            if drift == "source_group":
                mirror["abilityGrpId"] = 833331
            if drift == "targeting_group" or role == "ability":
                mirror["targets"][0]["targetingAbilityGrpId"] = 833331
    elif drift == "exiled_candidate":
        state["exile"] = [state["battlefield"].pop(0)]
    else:
        state["battlefield"].extend(state.pop("stack"))
    assert not target_context(state, pending)
