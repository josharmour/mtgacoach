"""Ledger #88 (2026-10-19 match c8ea2b6c): stack abilities named through the CARD table.

Essence Channeler's trigger (ability grpId 92970, "Whenever you gain life,
put a +1/+1 counter on CARDNAME.", printed on Ajani's Pridemate and ~35 other
cards) shares its id with the card Deal Gone Bad ("Target creature gets
-3/-3 until end of turn. Target player mills three cards."). Every lifegain
opponent showed a phantom Deal Gone Bad on the stack, and the concede check
held its verdict for it (standalone.log 11:54:28: "their Deal Gone Bad on the
stack could change the combat ('gets -3')"). An ability's id is now read in
the ability table and the object is named after its source card.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tests.bug_180436_states import bug_state

import arenamcp.server as server
from arenamcp.concede import estimate_loss
from arenamcp.gamestate import GameObject, GameObjectKind, GameState, Zone, ZoneType
from arenamcp.gamestate_decisions import _resolve_request_source_context

TRIGGER_TEXT = "Whenever you gain life, put a +1/+1 counter on CARDNAME."
DEAL_GONE_BAD = "Target creature gets -3/-3 until end of turn. Target player mills three cards."


def _card(name: str, text: str, type_line: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name, oracle_text=text, type_line=type_line, mana_cost="", cmc=0, colors=[], power=None,
        toughness=None, rarity="common", related_faces=None, scryfall_uri=None,
    )  # fmt: skip


class _CardDB:
    """The MTGA database's two tables for the ids in the report (grpid_collision.txt)."""

    cards = {
        92970: _card("Deal Gone Bad", DEAL_GONE_BAD, "Instant"),
        91548: _card(
            "Essence Channeler", "As long as you've lost life this turn, ...", "Creature — Bat Cleric"
        ),
    }
    abilities = {92970: TRIGGER_TEXT}

    def get_card_by_arena_id(self, grp_id: int):
        return self.cards.get(int(grp_id))

    def get_ability_text(self, grp_id: int):
        return self.abilities.get(int(grp_id))


class _LiveState:
    def __init__(self, objects: dict):
        self.game_objects = objects


@pytest.fixture
def card_db(monkeypatch):
    monkeypatch.setattr(server, "_get_card_db", lambda: _CardDB())
    channeler = SimpleNamespace(instance_id=391, grp_id=91548)
    monkeypatch.setattr(server, "game_state", _LiveState({391: channeler}))


# gsid 438's stack object, as the published snapshot holds it (final_state in the match bundle).
TRIGGER_406 = {
    "instance_id": 406, "grp_id": 92970, "zone_id": 27, "owner_seat_id": 2, "controller_seat_id": 2,
    "visibility": "Visibility_Public", "card_types": [], "subtypes": [], "power": None, "toughness": None,
    "is_tapped": False, "turn_entered_battlefield": -1, "is_attacking": False, "is_blocking": False,
    "object_kind": "ABILITY", "parent_instance_id": 391,
}  # fmt: skip


def test_an_ability_id_is_read_in_the_ability_table(card_db):
    info = server.enrich_ability(92970, 91548)
    assert info["name"] == "Essence Channeler ability"
    assert info["oracle_text"] == TRIGGER_TEXT and info["type_line"] == "Ability"
    # Without a known source it stays an ability, never the card that shares its id.
    assert server.enrich_ability(92970)["name"] == "Ability (ID: 92970)"
    # A card id is still a card.
    assert server.enrich_with_oracle_text(92970)["name"] == "Deal Gone Bad"


def test_the_published_stack_names_the_trigger_after_its_source(card_db):
    item = server._serialize_snapshot_obj(dict(TRIGGER_406))
    assert item["name"] == "Essence Channeler ability"
    assert item["oracle_text"] == TRIGGER_TEXT and "Deal Gone Bad" not in str(item)
    # A card on the stack is unchanged.
    spell = server._serialize_snapshot_obj({**TRIGGER_406, "object_kind": "CARD", "parent_instance_id": None})
    assert spell["name"] == "Deal Gone Bad"


def test_the_bridge_stack_names_the_trigger_after_its_source(card_db):
    lookup = {391: {"instance_id": 391, "grp_id": 91548}}
    bridge = {"instance_id": 406, "grp_id": 92970, "owner_id": 2, "controller_id": 2, "object_type": "Ability",
              "parent_instance_id": 391}  # fmt: skip
    item = server._serialize_bridge_card(bridge, lookup)
    assert item["name"] == "Essence Channeler ability" and item["oracle_text"] == TRIGGER_TEXT


def test_the_request_source_context_reads_the_ability_text(card_db):
    state = GameState()
    state.zones[27] = Zone(
        zone_id=27, zone_type=ZoneType.STACK, owner_seat_id=None, object_instance_ids=[406]
    )
    state.game_objects[391] = GameObject(
        instance_id=391, grp_id=91548, zone_id=28, owner_seat_id=2, controller_seat_id=2,
        object_kind=GameObjectKind.CARD,
    )  # fmt: skip
    state.game_objects[406] = GameObject(
        instance_id=406, grp_id=92970, zone_id=27, owner_seat_id=2, controller_seat_id=2,
        object_kind=GameObjectKind.ABILITY, parent_instance_id=391,
    )  # fmt: skip
    context = _resolve_request_source_context(state, 406)
    assert context["source_oracle_text"] == TRIGGER_TEXT
    assert context["source_card"] == "Essence Channeler"
    # Without its parent the card table still never names it.
    del state.game_objects[391]
    assert _resolve_request_source_context(state, 406).get("source_card") is None


def test_the_concede_check_no_longer_waits_on_a_phantom_removal_spell(card_db):
    item = server._serialize_snapshot_obj(dict(TRIGGER_406))
    fixed = bug_state()
    fixed["stack"] = [item]
    estimate = estimate_loss(fixed)
    assert estimate.confidence >= 0.95, estimate.blockers
    assert not any("Deal Gone Bad" in why for why in estimate.blockers)
    # What the phantom did (SL 557423: "their Deal Gone Bad on the stack could change the combat").
    phantom = bug_state()
    phantom["stack"] = [{**item, "name": "Deal Gone Bad", "oracle_text": DEAL_GONE_BAD}]
    assert any("Deal Gone Bad on the stack" in why for why in estimate_loss(phantom).blockers)
