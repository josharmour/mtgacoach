"""A2: reconstructed bug_20261109_21243421:24:11; no inference or real cache."""

import json
from pathlib import Path

import pytest

from arenamcp.card_facts import client
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.play_safety import card_facts_removal_reason, filter_play_options, unsafe_play_reason


@pytest.fixture
def case(monkeypatch):
    data = json.loads((Path(__file__).parent / "fixtures/a2_fury_212434.json").read_text())
    state = data["state"]
    card = next(c for c in state["hand"] if c["name"] == "Fury")
    record = {
        "name": card["name"],
        "rules_hash": client.hash_for_card(card),
        "type_line": card["type_line"],
        "mana_cost": card["mana_cost"],
        "effects": [
            {
                "trigger": "ENTERS_THE_BATTLEFIELD_OR_EVOKE",
                "effect": {
                    "effect_type": "Damage",
                    "amount": 4,
                    "target": "divided among any number of target creatures and/or planeswalkers",
                },
            }
        ],
        "modes": [
            {"name": "evoke", "cost_text": "Exile a red card from your hand.", "stays_on_battlefield": False}
        ],
    }
    monkeypatch.setattr(client, "facts_for", lambda cards: {client.hash_for_card(card): record})
    meta = {
        "actionType": "ActionType_Cast",
        "instanceId": card["instance_id"],
        "alternativeCostText": "Evoke—Exile a red card from your hand.",
    }
    return data, state, card, record, meta


def test_actual_offered_evoke_withheld_hard_cast_kept(case):
    data, state, card, _, meta = case
    decision = PendingDecision(
        (113, 179),
        "ActionsAvailable",
        (DecisionOption("evoke", data["options"][0], True, meta), DecisionOption("pass", "Pass")),
    )
    result = filter_play_options(decision, state)
    assert [o.option_id for o in result.options] == ["pass"]
    assert "leaves no permanent" in unsafe_play_reason(state, card, "cast", meta)
    # Counterfactual five-mana hard cast retains a real body and its legal development.
    assert unsafe_play_reason(state, card, "cast", {"manaCost": "{3}{R}{R}", "hasAutoTap": True}) == ""
    assert data["options"] == ["Cast Fury [alternative cost: Evoke—Exile a red card from your hand.]", "Pass"]


@pytest.mark.parametrize(
    "change", ["unknown", "conditional", "draw", "death_trigger", "stays", "other_mode", "hash", "name"]
)
def test_unsupported_or_additional_value_never_vetoes(case, change):
    _, state, card, record, meta = case
    if change == "unknown":
        record["effects"][0]["effect"] = {"effect_type": "Unknown", "text": "unmodelled"}
    if change == "conditional":
        record["effects"][0]["conditions"] = ["if a creature died"]
    if change == "draw":
        record["effects"].append({"effect": {"effect_type": "Draw", "count": 1}})
    if change == "death_trigger":
        record["effects"].append(
            {"trigger": "DIES", "effect": {"effect_type": "Damage", "amount": 1, "target": "each opponent"}}
        )
    if change == "stays":
        record["modes"][0]["stays_on_battlefield"] = True
    if change == "other_mode":
        meta["alternativeCostText"] = "Sneak—{R}"
    if change == "hash":
        record["rules_hash"] = "wrong"
    if change == "name":
        record["name"] = "Other card"
    assert card_facts_removal_reason(card, state, meta) == ""


@pytest.mark.parametrize(
    "change", ["kill", "marked", "combat", "unknown_toughness", "walker", "engine", "lifelink", "deathtouch"]
)
def test_existing_value_exceptions_remain(case, change):
    _, state, card, _, meta = case
    creature = next(c for c in state["battlefield"] if c["name"] == "Griselbrand")
    if change == "kill":
        creature["toughness"] = 4
    if change == "marked":
        creature.update(damage=3, damaged_this_turn=True)
    if change == "combat":
        creature["is_attacking"] = True
    if change == "unknown_toughness":
        creature["toughness"] = None
    if change == "walker":
        state["battlefield"].append({"type_line": "Planeswalker", "controller_seat_id": 1})
    if change == "engine":
        state["battlefield"].append(
            {"controller_seat_id": 2, "oracle_text": "Whenever a creature you control dies, draw a card."}
        )
    if change in ("lifelink", "deathtouch"):
        card["keywords"] = [change]
    assert card_facts_removal_reason(card, state, meta) == ""


def test_empty_board_still_wastes_ephemeral_damage(case):
    _, state, card, _, meta = case
    state["battlefield"] = []
    assert "leaves no permanent" in card_facts_removal_reason(card, state, meta)


@pytest.mark.parametrize("life", [4, None])
def test_player_lethal_or_unknown_life_never_withheld(case, life):
    _, state, card, record, meta = case
    record["effects"][0]["effect"]["target"] = "any target"
    next(p for p in state["players"] if not p["is_local"])["life_total"] = life
    assert card_facts_removal_reason(card, state, meta) == ""


def test_no_record_preserves_original_behavior(case, monkeypatch):
    _, state, card, _, meta = case
    monkeypatch.setattr(client, "facts_for", lambda cards: {})
    assert unsafe_play_reason(state, card, "cast", meta) == ""


def test_record_removal_target_and_generic_name(case, monkeypatch):
    _, state, card, record, meta = case
    card.update(name="Generic entrant", oracle_text="Unparsed oracle syntax")
    record.update(name=card["name"], rules_hash=client.hash_for_card(card))
    record["effects"] = [
        {"trigger": "ENTERS_THE_BATTLEFIELD", "effect": {"effect_type": "Destroy", "target": "enchantment"}}
    ]
    monkeypatch.setattr(client, "facts_for", lambda cards: {client.hash_for_card(card): record})
    assert "no opposing target" in card_facts_removal_reason(card, state, meta)
    state["battlefield"].append({"type_line": "Enchantment", "controller_seat_id": 1})
    assert card_facts_removal_reason(card, state, meta) == ""


@pytest.mark.parametrize(
    "change",
    [
        "mana",
        "record_lifelink",
        "stack",
        "unknown_target",
        "battle",
        "untyped_permanent",
        "missing_hash",
        "ambiguous_mode",
    ],
)
def test_incomplete_proof_or_other_resource_keeps_play(case, change):
    _, state, card, record, meta = case
    if change == "mana":
        record["mana"] = [{"mana": "{R}"}]
    if change == "record_lifelink":
        record["body"] = {"keywords": ["Lifelink"]}
    if change == "stack":
        state["stack"] = [{"name": "Pending effect"}]
    if change == "unknown_target":
        record["effects"][0]["effect"]["target"] = "special target"
    if change == "battle":
        record["effects"][0]["effect"]["target"] = "any target"
        state["battlefield"].append({"controller_seat_id": 1, "type_line": "Battle — Siege"})
    if change == "untyped_permanent":
        state["battlefield"].append({"controller_seat_id": 1})
    if change == "missing_hash":
        del record["rules_hash"]
    if change == "ambiguous_mode":
        record["modes"].append(dict(record["modes"][0]))
    assert card_facts_removal_reason(card, state, meta) == ""


def test_record_only_spell_and_generic_ephemeral_creature(case, monkeypatch):
    _, state, card, record, meta = case
    card.update(name="Generic damage source", oracle_text="Unparsed oracle syntax")
    record.update(name=card["name"], rules_hash=client.hash_for_card(card))
    monkeypatch.setattr(client, "facts_for", lambda cards: {client.hash_for_card(card): record})
    assert "leaves no permanent" in unsafe_play_reason(state, card, "cast", meta)
    card["type_line"] = "Sorcery"
    record.update(type_line="Sorcery", rules_hash=client.hash_for_card(card))
    assert "leaves no permanent" in unsafe_play_reason(state, card, "cast", {})


def test_two_damage_entries_use_optimistic_sum(case):
    _, state, card, record, meta = case
    record["effects"].append(dict(record["effects"][0]))
    assert card_facts_removal_reason(card, state, meta) == ""  # 8 total can kill the7/7


@pytest.mark.parametrize("controller", [1, 2, None])
def test_global_damage_modifier_never_certifies_no_kill(case, controller):
    """212434 reviewer perturbation: opponent Dictate makes four damage lethal to7/7."""
    _, state, card, _, meta = case
    state["battlefield"].append(
        {
            "name": "Dictate of the Twin Gods",
            "type_line": "Enchantment",
            "controller_seat_id": controller,
            "oracle_text": "If a source would deal damage to a permanent or player, it deals double that damage to that permanent or player instead.",
        }
    )
    assert unsafe_play_reason(state, card, "cast", meta) == ""
    # Generic replacement wording, not a named-card exception.
    state["battlefield"][-1].update(name="Global modifier", oracle_text="Sources deal triple damage instead.")
    assert unsafe_play_reason(state, card, "cast", meta) == ""
