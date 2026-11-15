"""The two public prints and spell/board instance IDs come from a native Nov 15 match."""

from dataclasses import replace

import pytest

from arenamcp.card_facts.source_identity import legacy_lookup_hash
from arenamcp.commentary.audited_facts import permanent_fact
from arenamcp.commentary.card_annotations import public_pair_refs
from arenamcp.commentary.contracts import Evidence


def observed_pair():
    common = {"owner_seat_id": 2, "controller_seat_id": 2, "object_kind": "CARD"}
    spell = dict(
        common,
        instance_id=824,
        grp_id=91888,
        name="Birds of Paradise",
        mana_cost="{G}",
        type_line="Creature — Bird",
        oracle_text="Flying\n{oT}: Add one mana of any color.",
    )
    payoff = dict(
        common,
        instance_id=798,
        grp_id=87082,
        name="Impact Tremors",
        mana_cost="{1}{R}",
        type_line="Enchantment",
        oracle_text="Whenever a creature you control enters, this enchantment deals 1 damage to each opponent.",
    )
    zones = {"stack": [spell], "battlefield": [payoff]}
    facts = {"state:permanent:798": permanent_fact(payoff)}
    for card in (spell, payoff):
        gid = card["grp_id"]
        card["public_native_print"] = f"{gid}:{legacy_lookup_hash(card)}"
        facts[f"card:{gid}"] = {
            "name": card["name"],
            "type_line": card["type_line"],
            "mana_cost": card["mana_cost"],
            "rules": card["oracle_text"],
            "source": "local_card_database",
        }
    event = Evidence(
        "cast",
        "action_observed",
        5,
        "position",
        seat=2,
        payload={k: spell[k] for k in ("name", "grp_id", "instance_id")},
    )
    return zones, facts, event


def test_cast_can_focus_attention_on_two_verified_public_prints():
    zones, facts, event = observed_pair()
    assert public_pair_refs(zones, (event,), event, facts) == [
        "card:91888",
        "state:permanent:798",
        "card:87082",
    ]
    assert not public_pair_refs(zones, (), event, facts)
    arrival = replace(
        event,
        event_id="arrival",
        kind="permanent_entered",
        payload={"to_zone": "battlefield", "instance_id": 798},
    )
    assert not public_pair_refs(zones, (event, arrival), event, facts)


@pytest.mark.parametrize(
    "change",
    [
        {"face_down": True},
        {"visibility": "private"},
        {"controller_seat_id": 1},
        {"owner_seat_id": 1},
        {"public_native_print": "changed"},
        {"generated": True},
    ],
)
def test_ineligible_partner_cannot_be_selected(change):
    zones, facts, event = observed_pair()
    zones["battlefield"][0].update(change)
    assert not public_pair_refs(zones, (event,), event, facts)


@pytest.mark.parametrize(
    "key, change",
    [
        ("card:91888", {"source": "unknown"}),
        ("card:87082", {"mana_cost": "{2}"}),
        ("state:permanent:798", {"instance_id": 824}),
        ("state:permanent:798", {"name": "Other"}),
    ],
)
def test_changed_source_or_board_fact_cannot_focus_the_analyst(key, change):
    zones, facts, event = observed_pair()
    facts[key].update(change)
    assert not public_pair_refs(zones, (event,), event, facts)
