"""Caller-scoped combat keys preserve outcomes and never survive a combat call."""

from copy import deepcopy

import pytest

from arenamcp import board_assessment as ba
from arenamcp import combat_solver as cs
from arenamcp.combat_loss import LossState


def body(identity, text="", power=3):
    return dict(
        instance_id=identity,
        name="Synthetic body",
        power=power,
        toughness=3,
        oracle_text=text,
        keywords=[],
        card_types=["Creature"],
        _can_block=True,
        _unblockable=False,
    )


def full_result(result):
    return (tuple(result), result.loss, result.terminal, result.counter_deltas, result.damage_deltas)


def uncached(monkeypatch):
    search, resolve = ba._search_blocks, ba._resolve_attacker

    def old_search(*args, **kwargs):
        kwargs.pop("_fight_keys", None)
        kwargs.pop("_only_assignments", None)
        return search(*args, **kwargs)

    def old_resolve(*args, **kwargs):
        kwargs.pop("keys", None)
        return resolve(*args, **kwargs)

    monkeypatch.setattr(ba, "_search_blocks", old_search)
    monkeypatch.setattr(ba, "_resolve_attacker", old_resolve)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Flying",
        "Double strike",
        "First strike",
        "Lifelink",
        "Infect",
        "Toxic 2",
        "Menace",
        "Trample",
        "Protection from red",
    ],
)
@pytest.mark.parametrize("life", [2, 20])
@pytest.mark.parametrize("defending", [True, False])
def test_scoped_keys_match_uncached_complete_outcomes(monkeypatch, text, life, defending):
    attackers = [body(1, text), body(2, "Lifelink", -1)]
    blockers = [body(3), body(4, "Deathtouch")]
    options = dict(detail=True, with_loss=True, defender=LossState(9, 0), defending_player=defending)
    before = deepcopy((attackers, blockers))
    got = full_result(ba._combat_life(attackers, blockers, life, **options))
    uncached(monkeypatch)
    expected = full_result(ba._combat_life(attackers, blockers, life, **options))
    assert got == expected
    assert (attackers, blockers) == before


def test_keys_reused_within_call_but_rebuilt_after_mutation(monkeypatch):
    attackers, blockers = [body(1)], [body(2)]
    calls = []
    original = cs._fight_key

    def recorded(card):
        calls.append(id(card))
        return original(card)

    monkeypatch.setattr(cs, "_fight_key", recorded)
    first = ba._combat_life(attackers, blockers, 20, detail=True)
    assert calls.count(id(attackers[0])) == 1
    assert calls.count(id(blockers[0])) == 1
    attackers[0]["power"] = 7
    blockers[0]["oracle_text"] = "First strike"
    second = ba._combat_life(attackers, blockers, 20, detail=True)
    assert calls.count(id(attackers[0])) == 2
    assert calls.count(id(blockers[0])) == 2
    uncached(monkeypatch)
    assert full_result(second) == full_result(ba._combat_life(attackers, blockers, 20, detail=True))
    assert first != second


@pytest.mark.parametrize(
    "backing",
    [
        None,
        {},
        {
            "modified_colors": ["Blue"],
            "controller_seat_id": 2,
            "protection_from_players": [1],
            "keywords": ["Flying"],
        },
    ],
)
@pytest.mark.parametrize("counters", [None, {}, {"+1/+1": True}, {"+1/+1": 1}, ["counter"], {"bad": []}])
def test_inlined_key_fields_retain_canonical_reader_values(backing, counters):
    card = body(1)
    card.update(_card=backing, counters=counters)
    key = cs._fight_key(card)
    expected_counters = cs._counters_key(card)
    if expected_counters is None:
        assert key is None
        return
    assert key[5:8] == tuple(
        cs._tuple_field(card, f) for f in ("keywords", "ability_grp_ids", "granted_abilities")
    )
    assert key[9] == cs._colors_key(card)
    assert key[11] == cs._players_key(card)
    assert key[12] == cs._controller_key(card)
    assert key[13] == expected_counters
    if backing:
        assert key[21:25] == tuple(
            cs._tuple_field(backing, f)
            for f in ("keywords", "ability_grp_ids", "granted_abilities", "removed_abilities")
        )
        assert key[25] == cs._counters_key(backing)
