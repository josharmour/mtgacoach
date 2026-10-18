"""``RulesEngine._card_db`` was referenced but never defined (2026-10-11
duplication audit): the target-selection fallback (no oracle text on the
decision, look the source card up by name) and four ``coach_postprocess``
paths raised AttributeError instead of resolving the card."""

import pytest

from arenamcp import card_db as card_db_module
from arenamcp.card_db import CardInfo
from arenamcp.rules_engine import RulesEngine


class _FakeDb:
    def __init__(self, cards):
        self._cards = cards

    def get_card_by_arena_id(self, arena_id):
        return next((c for c in self._cards if c.arena_id == arena_id), None)

    def get_card_by_name(self, name):
        return next((c for c in self._cards if c.name.lower() == name.lower()), None)


@pytest.fixture
def fake_db():
    murder = CardInfo(
        name="Murder",
        oracle_text="Destroy target creature.",
        mana_cost="{1}{B}{B}",
        type_line="Instant",
        arena_id=4242,
    )
    previous = card_db_module._card_db
    card_db_module.set_card_database(_FakeDb([murder]))
    yield
    card_db_module._card_db = previous


def _state():
    return {
        "battlefield": [
            {
                "instance_id": 10,
                "name": "Grizzly Bears",
                "type_line": "Creature — Bear",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "power": 2,
                "toughness": 2,
            }
        ],
        "local_seat_id": 2,
        "players": [{"seat_id": 2, "is_local": True}, {"seat_id": 1}],
        "decision_context": {"type": "target_selection", "source_card": "Murder"},
    }


def test_card_db_view_is_defined_and_dict_shaped(fake_db):
    assert RulesEngine._card_db.get(4242)["mana_cost"] == "{1}{B}{B}"
    assert RulesEngine._card_db.get(1, {}) == {}
    assert RulesEngine._card_db.get(None) is None
    assert RulesEngine._card_db.find_by_name("murder")["oracle_text"] == "Destroy target creature."


def test_target_selection_resolves_oracle_by_name(fake_db):
    actions = RulesEngine._get_target_selection_actions(_state())
    assert actions and "Grizzly Bears (OPP)" in actions[0], actions


def test_lookups_never_raise_when_db_is_broken(monkeypatch):
    def boom():
        raise RuntimeError("no db")

    monkeypatch.setattr(card_db_module, "get_card_database", boom)
    assert RulesEngine._card_db.get(4242) is None
    assert RulesEngine._card_db.find_by_name("Murder") is None
