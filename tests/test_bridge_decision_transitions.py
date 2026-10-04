"""A live request must not inherit choices from the decision just consumed."""

import pytest

from arenamcp.gre_bridge import enrich_snapshot_from_pending_response


def optional_state():
    return {
        "pending_decision": "Optional Action",
        "legal_actions": ["Accept (yes)", "Decline (no)"],
        "legal_actions_raw": [{"actionType": "OptionalAction"}],
        "decision_context": {
            "type": "optional_action",
            "requestType": "OptionalAction",
            "requestClass": "OptionalActionMessageRequest",
            "prompt_id": 1250,
            "min": 1,
            "max": 1,
            "recipient_names": ["Old recipient"],
            "commander_return": True,
            "source_card": "Lumbering Worldwagon",
            "source_oracle_text": "You may search your library for a basic land card.",
            "source_card_oracle_text": "Crew 2",
            "source_parent_instance_id": 460,
            "raw": {"optionalActionMessage": {"sourceId": 464}, "prompt": {"promptId": 1250}},
        },
    }


def search_poll(source_id=0, maximum=1):
    payload = {
        "sourceId": source_id,
        "prompt": {"promptId": 1065},
        "min": 0,
        "max": maximum,
        "options": [361, 423],
    }
    return {
        "has_pending": True,
        "request_type": "Search",
        "request_class": "SearchRequest",
        "request_payload": payload,
        "decision_context": {"type": "search", **payload},
        "search_candidates": [361, 423],
        "select_n_min": 0,
        "select_n_max": maximum,
    }


@pytest.mark.parametrize("maximum", [1, 2])
def test_optional_to_search_discards_consumed_prompt_and_keeps_live_limits(maximum):
    state = optional_state()
    old_context = state["decision_context"]
    enrich_snapshot_from_pending_response(state, search_poll(maximum=maximum), bridge_connected=True)

    context = state["decision_context"]
    assert state["pending_decision"] == "Search Library"
    assert context["type"] == "search"
    assert context["requestType"] == "Search"
    assert context["requestClass"] == "SearchRequest"
    assert (context["min"], context["max"]) == (0, maximum)
    assert context["options"] == [361, 423]
    assert context["prompt"] == {"promptId": 1065}
    assert all(
        key not in context
        for key in (
            "raw",
            "prompt_id",
            "recipient_names",
            "commander_return",
            "source_card",
            "source_oracle_text",
            "source_card_oracle_text",
            "source_parent_instance_id",
        )
    )
    assert state["legal_actions"] == []
    assert state["legal_actions_raw"] == []
    assert old_context["type"] == "optional_action"  # Do not mutate a shared log snapshot.
    assert old_context["raw"]["optionalActionMessage"]["sourceId"] == 464


@pytest.mark.parametrize("source_id, retains_source", [(464, True), (999, False), (0, False)])
def test_source_rules_survive_transition_only_with_matching_authoritative_source(source_id, retains_source):
    state = optional_state()
    enrich_snapshot_from_pending_response(state, search_poll(source_id), bridge_connected=True)
    context = state["decision_context"]
    assert ("source_card" in context) is retains_source
    assert ("source_oracle_text" in context) is retains_source
    assert ("source_card_oracle_text" in context) is retains_source
    if retains_source:
        assert context["source_card"] == "Lumbering Worldwagon"
        assert context["source_parent_instance_id"] == 460
    if source_id:
        assert context["source_id"] == source_id
    assert "raw" not in context
    assert "prompt_id" not in context


def test_family_change_without_plugin_context_still_clears_old_optional_fields():
    state = optional_state()
    poll = search_poll()
    del poll["decision_context"]
    enrich_snapshot_from_pending_response(state, poll, bridge_connected=True)
    assert state["decision_context"]["type"] == "search"
    assert state["decision_context"]["requestType"] == "Search"
    assert "raw" not in state["decision_context"]
    assert "max" not in state["decision_context"]  # Never keep the old optional limit.
    assert state["_bridge_request_payload"]["max"] == 1


def test_same_family_keeps_log_details_and_refreshes_request_aliases():
    state = optional_state()
    poll = {
        "has_pending": True,
        "request_type": "OptionalActionMessage",
        "request_class": "OptionalActionMessageRequest",
    }
    enrich_snapshot_from_pending_response(state, poll, bridge_connected=True)
    assert state["decision_context"]["raw"]["optionalActionMessage"]["sourceId"] == 464
    assert state["decision_context"]["prompt_id"] == 1250
    assert state["decision_context"]["requestType"] == "OptionalActionMessage"


def test_optional_action_without_request_class_is_recognized():
    state = {"decision_context": {"type": "actions_available"}}
    enrich_snapshot_from_pending_response(
        state, {"has_pending": True, "request_type": "OptionalAction"}, bridge_connected=True
    )
    assert state["pending_decision"] == "Optional Action"
    assert state["decision_context"]["type"] == "optional_action"


def test_log_selection_subtype_does_not_lose_its_current_prompt():
    state = {"decision_context": {"type": "scry", "promptText": "Scry 2", "count": 2}}
    enrich_snapshot_from_pending_response(
        state, {"has_pending": True, "request_type": "SelectN"}, bridge_connected=True
    )
    assert state["decision_context"]["type"] == "scry"
    assert state["decision_context"]["count"] == 2


@pytest.mark.parametrize(
    "kind",
    [
        "select_n",
        "discard",
        "sacrifice",
        "exile",
        "destroy",
        "return",
        "scry",
        "surveil",
        "mill",
        "explore",
        "choose_creature",
        "choose_land",
        "choose_enchantment",
        "choose_artifact",
        "choose_permanent",
        "choose",
    ],
)
def test_select_n_log_subtypes_retain_choice_details(kind):
    details = {
        "count": 2,
        "min": 1,
        "max": 2,
        "option_ids": [101, 102],
        "option_cards": ["Forest", "Island"],
        "context_raw": kind,
        "id_type": "InstanceId",
    }
    state = {"decision_context": {"type": kind, **details}, "legal_actions": ["Choose Forest"]}
    enrich_snapshot_from_pending_response(
        state, {"has_pending": True, "request_type": "SelectN"}, bridge_connected=True
    )
    assert all(state["decision_context"][key] == value for key, value in details.items())
    assert state["legal_actions"] == ["Choose Forest"]


def test_mulligan_bottom_retains_group_request_details():
    raw = {"groupSpecs": [{"zoneType": "ZoneType_Library", "lowerBound": 2}], "instanceIds": [101, 102]}
    state = {"decision_context": {"type": "mulligan_bottom", "raw": raw}}
    enrich_snapshot_from_pending_response(
        state, {"has_pending": True, "request_type": "Group"}, bridge_connected=True
    )
    assert state["decision_context"]["raw"] == raw


@pytest.mark.parametrize("kind", ["scry", "surveil"])
@pytest.mark.parametrize("provenance", ["request_tags", "raw_group", "effect_only"])
def test_group_scry_and_surveil_preserve_current_context(kind, provenance):
    context = {"type": kind, "promptText": f"{kind} 2", "count": 2, "option_ids": [101, 102]}
    if provenance == "request_tags":
        context.update(requestType="Group", requestClass="GroupRequest")
    elif provenance == "raw_group":
        context["raw"] = {"groupSpecs": [{"zoneType": "ZoneType_Library"}], "instanceIds": [101, 102]}
    state = {"decision_context": context}
    enrich_snapshot_from_pending_response(
        state, {"has_pending": True, "request_type": "Group"}, bridge_connected=True
    )
    assert state["decision_context"]["type"] == kind
    assert state["decision_context"]["option_ids"] == [101, 102]
    assert state["decision_context"]["count"] == 2
    if "raw" in context:
        assert state["decision_context"]["raw"] == context["raw"]


def test_known_group_to_select_n_transition_discards_old_group_raw_despite_same_effect():
    state = {
        "decision_context": {
            "type": "scry",
            "requestType": "Group",
            "promptText": "Scry 2",
            "raw": {"groupSpecs": [{"zoneType": "ZoneType_Library"}]},
        }
    }
    enrich_snapshot_from_pending_response(
        state, {"has_pending": True, "request_type": "SelectN"}, bridge_connected=True
    )
    assert "raw" not in state["decision_context"]
    assert "promptText" not in state["decision_context"]
