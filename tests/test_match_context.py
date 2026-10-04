"""Shared deck context stays complete, cacheable, and honest about hidden state."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from arenamcp.match_context import prepare_match_context, with_deck_reference


def card_lookup(grp_id):
    return {
        "name": f"Card {grp_id}",
        "type_line": "Creature — Test",
        "mana_cost": "{2}{G}",
        "cmc": 3,
        "power": "2",
        "toughness": "3",
        "oracle_text": f"Rules for card {grp_id}.",
    }


def state(deck=None, **kwargs):
    return {
        "deck_cards": deck if deck is not None else [100, 100, 200, 300],
        "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2, "is_local": False}],
        **kwargs,
    }


def obj(grp_id, instance_id, **kwargs):
    return {"grp_id": grp_id, "instance_id": instance_id, "owner_seat_id": 1, **kwargs}


def prepare(snapshot):
    return prepare_match_context(snapshot, card_lookup=card_lookup)


def test_full_catalog_and_library_have_more_than_fifteen_distinct_cards():
    deck = list(range(100, 140)) + [100, 100]
    result = prepare(state(deck))
    for gid in range(100, 140):
        assert f"Rules for card {gid}." in result["deck_reference"]
        assert f"[grp:{gid}]" in result["library_summary"]
    assert "3x Card 100" in result["deck_reference"]
    assert "3x Card 100" in result["library_summary"]
    assert "{2}{G}" in result["deck_reference"]
    assert "printed 2/3" in result["deck_reference"]


def test_prefix_is_identical_as_board_hand_and_options_change():
    first = prepare(state(turn_info={"turn_number": 1}))
    second = prepare(
        state(
            hand=[obj(100, 10)],
            battlefield=[obj(200, 20, power=7)],
            turn_info={"turn_number": 9},
        )
    )
    assert first["deck_reference"] == second["deck_reference"]
    assert first["library_summary"] != second["library_summary"]
    prompts = [
        with_deck_reference("Decision: " + option, snapshot)
        for option, snapshot in [("play land", first), ("cast removal", second)]
    ]
    prefix = first["deck_reference"] + "\n\n"
    assert all(prompt.startswith(prefix) for prompt in prompts)
    assert prompts[0] != prompts[1]


def test_multiset_subtracts_our_stolen_cards_but_not_opponents_cards_we_control():
    result = prepare(
        state(
            hand=[obj(100, 10)],
            battlefield=[
                obj(100, 11, controller_seat_id=2),
                obj(200, 12, owner_seat_id=2, controller_seat_id=1),
            ],
            library_count=2,
        )
    )
    summary = result["library_summary"]
    assert "2 cards left" in summary
    assert "Card 100" not in summary
    assert "1x Card 200" in summary
    assert "1x Card 300" in summary
    assert "50.0% per random draw" in summary
    assert "Composition uncertain" not in summary


@pytest.mark.parametrize("kind", ["TOKEN", "ABILITY", "GameObjectType_Token", "GameObjectType_Ability"])
def test_tokens_and_abilities_do_not_consume_real_deck_cards(kind):
    result = prepare(
        state(
            battlefield=[obj(100, 10, object_kind=kind)],
            stack=[obj(200, 20, object_kind=kind)],
            library_count=4,
        )
    )
    assert "4 cards left" in result["library_summary"]
    assert "2x Card 100" in result["library_summary"]
    assert "Composition uncertain" not in result["library_summary"]


def test_is_token_flag_and_duplicate_instance_ids_do_not_double_subtract():
    card = obj(100, 10)
    result = prepare(
        state(
            battlefield=[card, obj(200, 20, is_token=True)],
            stack=[dict(card)],
            graveyard=[dict(card)],
            library_count=3,
        )
    )
    assert "3 cards left" in result["library_summary"]
    assert "1x Card 100" in result["library_summary"]
    assert "1x Card 200" in result["library_summary"]


def test_unknown_copy_identity_preserves_original_inventory_but_disables_exact_odds():
    result = prepare(state(battlefield=[obj(100, 10, copied_from_grp_id=100)]))
    assert "2x Card 100" in result["library_summary"]
    assert "Composition uncertain" in result["library_summary"]
    assert "per random draw" not in result["library_summary"]


@pytest.mark.parametrize("original_key", ["original_grp_id", "base_grp_id"])
def test_known_copy_original_identity_subtracts_original_card(original_key):
    card = obj(100, 10, copied_from_grp_id=100, **{original_key: 200})
    result = prepare(state(battlefield=[card], library_count=3))
    assert "2x Card 100" in result["library_summary"]
    assert "Card 200" not in result["library_summary"]
    assert "Composition uncertain" not in result["library_summary"]


def test_count_mismatch_removes_draw_odds_and_exposes_observed_count():
    result = prepare(state(library_count=2))
    assert "4 cards left" in result["library_summary"]
    assert "Arena library count 2" in result["library_summary"]
    assert "Composition uncertain" in result["library_summary"]
    assert "per random draw" not in result["library_summary"]
    assert "Actual search options are authoritative" in result["library_summary"]


def test_unknown_local_seat_never_claims_exact_odds():
    result = prepare(state(players=[], battlefield=[obj(100, 10)]))
    assert "Composition uncertain" in result["library_summary"]
    assert "2x Card 100" in result["library_summary"]
    assert "per random draw" not in result["library_summary"]


def test_explicit_local_seat_works_without_player_metadata():
    result = prepare(state(players=[], local_seat_id=1, hand=[obj(100, 10)], library_count=3))
    assert "3 cards left" in result["library_summary"]
    assert "1x Card 100" in result["library_summary"]
    assert "Composition uncertain" not in result["library_summary"]


def test_raw_snapshot_my_hand_is_subtracted():
    result = prepare(state(zones={"my_hand": [obj(100, 10)], "library_count": 3}))
    assert "3 cards left" in result["library_summary"]
    assert "1x Card 100" in result["library_summary"]
    assert "Composition uncertain" not in result["library_summary"]


def test_unknown_card_ownership_marks_inventory_uncertain():
    result = prepare(state(battlefield=[obj(100, 10, owner_seat_id=None)]))
    assert "Composition uncertain" in result["library_summary"]
    assert "per random draw" not in result["library_summary"]


@pytest.mark.parametrize("card", [obj(0, 10), obj(999, 10), obj(100, 10, face_down=True)])
def test_unknown_hidden_or_generated_owned_card_marks_inventory_uncertain(card):
    result = prepare(state(exile=[card]))
    assert "Composition uncertain" in result["library_summary"]
    assert "per random draw" not in result["library_summary"]


def test_missing_card_rules_are_explicit_and_lookup_failures_do_not_break_context():
    def missing(gid):
        if gid == 100:
            raise LookupError("unknown")
        return None

    result = prepare_match_context(state(), card_lookup=missing)
    assert "Unknown(100)" in result["deck_reference"]
    assert "Rules text unavailable; do not invent abilities." in result["deck_reference"]
    assert "Unknown(300)" in result["library_summary"]


def test_commander_reference_survives_moving_out_of_command_zone():
    before = prepare(state(commander_grp_ids=[400], command=[obj(400, 40)], library_count=4))
    after = prepare(state(commander_grp_ids=[400], command=[], battlefield=[obj(400, 40)], library_count=4))
    assert before["deck_reference"] == after["deck_reference"]
    assert "COMMANDER REFERENCE" in after["deck_reference"]
    assert "Rules for card 400." in after["deck_reference"]
    assert "Card 400" not in after["library_summary"]
    assert "Composition uncertain" not in after["library_summary"]


def test_commander_can_be_inferred_from_owned_command_zone():
    result = prepare(state(command=[obj(400, 40), obj(500, 50, owner_seat_id=2)]))
    assert "Rules for card 400." in result["deck_reference"]
    assert "Card 500" not in result["deck_reference"]


def test_other_face_rules_remain_in_catalog():
    def lookup(gid):
        return {
            **card_lookup(gid),
            "related_faces": [
                {
                    "name": "Other face",
                    "mana_cost": "{4}",
                    "type_line": "Land",
                    "oracle_text": "Back face rules.",
                },
            ],
        }

    result = prepare_match_context(state([100]), card_lookup=lookup)
    assert "Back face rules." in result["deck_reference"]


def test_preparation_does_not_mutate_state_or_require_deck_to_be_available():
    original = state(hand=[obj(100, 10)])
    saved = deepcopy(original)
    prepare(original)
    assert original == saved
    assert "count UNKNOWN" in prepare({})["library_summary"]
    assert with_deck_reference("decision", {}) == "decision"


def test_default_resolver_excludes_network_adapter_and_caches_local_reads(monkeypatch):
    from arenamcp import card_db, match_context

    local_reads = []
    network_reads = []

    class LocalSource:
        def get_card_by_arena_id(self, gid):
            local_reads.append(gid)
            return card_db.CardInfo(**card_lookup(gid), arena_id=gid)

        def get_card_by_name(self, name):
            return None

    def network_lookup(gid):
        network_reads.append(gid)
        raise AssertionError("Context rendering must not use a network resolver")

    network = card_db.ScryfallAdapter(SimpleNamespace(get_card_by_arena_id=network_lookup))
    database = SimpleNamespace(sources=[network, LocalSource()])
    monkeypatch.setattr(card_db, "get_card_database", lambda: database)
    monkeypatch.setattr(match_context.time, "monotonic", lambda: 100)
    match_context._local_card.cache_clear()
    try:
        first = prepare_match_context(state())
        second = prepare_match_context(state(hand=[obj(100, 10)]))
        assert "Rules for card 100." in first["deck_reference"]
        assert first["deck_reference"] == second["deck_reference"]
        monkeypatch.setattr(match_context.time, "monotonic", lambda: 200)
        assert prepare_match_context(state())["deck_reference"] == first["deck_reference"]
        assert local_reads == [100, 200, 300]
        assert network_reads == []
    finally:
        match_context._local_card.cache_clear()
