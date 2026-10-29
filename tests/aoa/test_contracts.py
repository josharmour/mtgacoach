import json
from pathlib import Path

import pytest

from arenamcp.commentary.contracts import Epoch, Evidence, SpeechCategory, Utterance
from arenamcp.commentary.observations import observation_from_snapshot


def snapshot():
    return json.loads((Path(__file__).parent / "fixtures/public_snapshot.json").read_text())


def project(state, **kwargs):
    return observation_from_snapshot(state, source_sequence=10, snapshot_id="s10", arrived_at=20, **kwargs)


def test_public_projection_is_allowlisted_and_detached():
    state = snapshot()
    result = project(state)
    assert "PRIVATE" not in json.dumps(result.to_wire())
    assert "pending_decision" not in result.public_state
    assert result.public_state["zones"]["battlefield"][1] == {
        "instance_id": 11,
        "face_down": True,
        "name": "Face-down card",
    }
    state["zones"]["stack"][0]["name"] = "Changed"
    assert result.public_state["zones"]["stack"][0]["name"] == "Lightning Bolt"


def test_private_nested_values_cannot_escape_scalar_fields():
    state = snapshot()
    state["players"][0]["life_total"] = {"secret": "PRIVATE"}
    assert "life_total" not in project(state).public_state["players"][0]


def test_unknown_life_is_not_fabricated_as_zero():
    state = snapshot()
    del state["players"][0]["life_total"]
    assert "life_total" not in project(state).public_state["players"][0]


def test_submission_and_stack_disappearance_do_not_invent_resolution():
    state = snapshot()
    event = Evidence("action1", "action_submitted", 9, "s9")
    first = project(state, events=[event])
    state["zones"]["stack"] = []
    second = project(state)
    assert first.events[0].kind == "action_submitted"
    assert second.events == ()
    resolved = Evidence("resolve1", "spell_resolved", 10, "s10")
    assert project(state, events=[resolved]).events == (resolved,)


def test_transient_events_keep_source_provenance_and_private_events_are_removed():
    events = [
        Evidence("cast", "action_observed", 8, "s8"),
        Evidence("resolve", "spell_resolved", 9, "s9"),
        Evidence("private", "action_observed", 10, "s10", "private"),
    ]
    result = project(snapshot(), events=events)
    assert [e.event_id for e in result.events] == ["cast", "resolve"]
    assert result.events[0].snapshot_id == "s8"
    with pytest.raises(ValueError):
        project(snapshot(), events=[Evidence("future", "combat", 11, "s11")])


@pytest.mark.parametrize("field,value", [("match_id", None), ("game_number", 0), ("game_number", True)])
def test_missing_or_invalid_epoch_is_rejected(field, value):
    state = snapshot()
    state[field] = value
    with pytest.raises(ValueError):
        project(state)


def test_game_boundary_changes_epoch():
    state = snapshot()
    first = project(state)
    state["game_number"] = 2
    assert first.epoch != project(state).epoch


def test_utterance_wire_round_trip_and_version_rejection():
    item = Utterance(
        "u1", "A spell is on the stack.", SpeechCategory.COMMENTARY, Epoch("m", 1), 28, evidence_ids=("cast",)
    )
    wire = json.loads(json.dumps(item.to_wire()))
    assert Utterance.from_wire(wire) == item
    wire["protocol_version"] = 99
    with pytest.raises(ValueError):
        Utterance.from_wire(wire)


def test_combat_is_observed_and_private_object_identity_is_redacted():
    state = snapshot()
    card = state["zones"]["battlefield"][0]
    card.update(is_attacking=True, is_blocked=False, attack_target_id=2, is_tapped=True)
    state["zones"]["exile"] = [
        {"instance_id": 14, "visibility": "Visibility_Private", "name": "PRIVATE_EXILE", "grp_id": 44}
    ]
    result = project(state, events=[Evidence("combat", "combat", 10, "s10")])
    public_card = result.public_state["zones"]["battlefield"][0]
    assert public_card["is_attacking"] and not public_card["is_blocked"]
    assert public_card["attack_target_id"] == 2 and public_card["is_tapped"]
    assert "PRIVATE" not in json.dumps(result.to_wire())
    assert result.public_state["zones"]["exile"][0]["name"] == "Unknown card"
