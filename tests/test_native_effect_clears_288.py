"""Native live facts must clear stale log effects without treating truncation as empty."""

import pytest
from tests.test_mac_game_state import card, enum, listing

from arenamcp.mac_game_state import _card_entry
from arenamcp.server import _serialize_bridge_card


@pytest.fixture
def enrich(monkeypatch):
    monkeypatch.setattr(
        "arenamcp.server.card_object_info",
        lambda _: {
            "name": "Rabbit",
            "type_line": "Creature",
            "oracle_text": "",
        },
    )


def test_native_zero_false_and_empty_facts_clear_log_state(enrich):
    live = _card_entry(card(123))
    expected = {
        "is_attacking": False,
        "is_blocking": False,
        "damage": 0,
        "damaged_this_turn": False,
        "class_level": 0,
        "counters": {},
        "color_production": [],
    }
    stale = {
        "is_attacking": True,
        "is_blocking": True,
        "damage": 3,
        "damaged_this_turn": True,
        "class_level": 3,
        "counters": {"Menace": 1},
        "color_production": ["Green"],
    }
    for key, value in expected.items():
        assert live.get(key) == value
    actual = _serialize_bridge_card(live, {123: stale})
    for key, value in expected.items():
        assert actual.get(key) == value


def test_native_omitted_facts_remain_unknown(enrich):
    node = card(123)
    for key in (
        "attackState_",
        "blockState_",
        "damage",
        "isDamagedThisTurn",
        "classLevel",
        "counterDatas_",
        "colorProduction_",
    ):
        node.pop(key)
    live = _card_entry(node)
    for key in (
        "is_attacking",
        "is_blocking",
        "damage",
        "damaged_this_turn",
        "class_level",
        "counters",
        "color_production",
    ):
        assert key not in live
    actual = _serialize_bridge_card(live, {123: {"is_attacking": True, "counters": {"Menace": 1}}})
    assert actual["is_attacking"] and actual["counters"] == {"Menace": 1}


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {"$ref": True, "$h": 99},
        {"$n": 2, "$items": []},
        {"$n": 2, "$items": [{"type_": enum("Menace"), "count_": 1}]},
    ],
)
def test_truncated_or_unknown_counter_collection_does_not_override_log(raw, enrich):
    live = _card_entry(card(123, counterDatas_=raw))
    assert "counters" not in live
    actual = _serialize_bridge_card(live, {123: {"counters": {"Flying": 1}}})
    assert actual["counters"] == {"Flying": 1}


def test_live_counters_replace_earlier_keyword_counters(enrich):
    live = _card_entry(card(123, counterDatas_=listing({"type_": enum("Flying"), "count_": 1})))
    actual = _serialize_bridge_card(live, {123: {"counters": {"Menace": 1}}})
    assert actual["counters"] == {"Flying": 1}
