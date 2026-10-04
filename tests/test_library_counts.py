"""Hidden-library regression: unobserved cards must never imply decking."""

import pytest

from arenamcp.gamestate import GameState
from arenamcp.library_counts import observed_library_count
from arenamcp.match_context import prepare_match_context


def _library_state(members=None, *, include_members=True):
    state = GameState()
    state.local_seat_id = 2
    zone = {"zoneId": 10, "type": "ZoneType_Library", "ownerSeatId": 2}
    if include_members:
        zone["objectInstanceIds"] = members
    state.update_from_message({"zones": [zone]})
    return state


def test_hidden_library_counts_members_without_materialized_card_objects():
    state = _library_state(list(range(1000, 1053)))
    assert state.game_objects == {}
    snapshot = state.get_published_snapshot()
    assert observed_library_count(snapshot) == 53
    assert snapshot["zones"]["library_count_source"] == "log_zone_membership"
    state.update_from_message({"zones": [{"zoneId": 10, "ownerSeatId": 2}]})
    assert observed_library_count(state.get_published_snapshot()) == 53


@pytest.mark.parametrize("mode", ["absent_zone", "absent_members", "null_members"])
def test_unobserved_library_is_unknown(mode):
    state = GameState() if mode == "absent_zone" else _library_state(include_members=mode != "absent_members")
    state.local_seat_id = 2
    state.publish_snapshot()
    assert observed_library_count(state.get_published_snapshot()) is None
    assert state.get_published_snapshot()["zones"]["library_count"] == "?"


def test_explicit_empty_library_is_zero_and_checkpoint_preserves_observation():
    state = _library_state([])
    assert observed_library_count(state.get_published_snapshot()) == 0
    checkpoint = state.export_checkpoint()
    restored = GameState()
    assert restored.restore_checkpoint(checkpoint)
    assert observed_library_count(restored.get_published_snapshot()) == 0
    checkpoint["zones"][0].pop("object_ids_known")
    assert restored.restore_checkpoint(checkpoint)
    assert observed_library_count(restored.get_published_snapshot()) is None


@pytest.mark.parametrize("deck", [[], [101, 102]])
def test_old_report_zero_without_provenance_never_claims_empty(deck):
    # Shape of bug_20261004_000524: no deck and default-zero public count.
    state = {"local_seat_id": 2, "deck_cards": deck, "zones": {"library_count": 0}}
    context = prepare_match_context(state, card_lookup=lambda gid: {"name": str(gid)})
    assert "Arena library count 0" not in context["library_summary"]
    assert "UNKNOWN" in context["library_summary"]
    assert observed_library_count(state) is None
    assert context["zones"]["library_count"] == "?"
    assert state["zones"]["library_count"] == 0  # Original report remains immutable.


def test_actual_empty_library_is_retained_in_context():
    state = {"zones": {"library_count": 0, "library_count_source": "bridge_total_card_count"}}
    assert "Arena library count 0" in prepare_match_context(state)["library_summary"]


@pytest.mark.parametrize("members", [False, "not-a-list", [None], [1000, None]])
def test_unreadable_membership_never_claims_empty(members):
    assert observed_library_count(_library_state(members).get_published_snapshot()) is None


@pytest.mark.parametrize("count", [None, -1, True, "0"])
def test_invalid_counters_cannot_become_card_count(count):
    assert (
        observed_library_count({"library_count": count, "library_count_source": "log_zone_membership"})
        is None
    )
