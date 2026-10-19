"""A small deck and its model playbook answer, shared by the deck-learning tests."""

import json

from arenamcp.deck_strategy import DeckPlaybook, commander_ids, deck_identity


def deck_case(kind="replay"):
    """Small decks with opposing policies; fixture cards aren't real card claims."""
    card = {
        "name": "Fixture Returning Captain",
        "mana_cost": "{3}{G}{G}",
        "type_line": "Legendary Creature — Scout",
        "oracle_text": "When this creature enters, if it isn't a token, create two tokens that are copies of it.",
    }
    preference = "Trade the recoverable original, then recast it to rebuild token copies."
    condition = "Equal combat outcomes and a profitable recast payable from surviving sources after tax."
    if kind == "preserve":
        card["name"] = "Fixture Growing Captain"
        card["oracle_text"] = "Whenever you cast a creature spell, put a +1/+1 counter on this creature."
        preference = "Preserve the commander and its accumulated counters; spend a replaceable token."
        condition = "The commander retains counters and a token supplies the same safe block."
    elif kind == "spells":
        card["name"] = "Fixture Spell Engine"
        card["type_line"] = "Enchantment"
        card["oracle_text"] = "Whenever you cast your second spell each turn, draw a card."
        preference = "Sequence two affordable spells together to draw, preserving necessary interaction."
        condition = "The engine is present and both spells have useful legal effects and combined payment."
    catalog = {
        1001: card,
        1002: {
            "name": "Forest",
            "type_line": "Basic Land — Forest",
            "mana_cost": "",
            "oracle_text": "({T}: Add {G}.)",
        },
    }
    commanders = [] if kind == "spells" else [1001]
    state = {
        "match_id": "deck-test",
        "local_seat_id": 1,
        "deck_cards": [1002] * 20 + ([] if commanders else [1001]),
        "commander_grp_ids": commanders,
        "players": [{"seat_id": 1, "is_local": True, "commander_ids": [50] if commanders else []}],
        "turn": {"turn_number": 5, "active_player": 2, "phase": "Combat"},
        "battlefield": [
            {**card, "grp_id": 1001, "instance_id": 50, "owner_seat_id": 1, "controller_seat_id": 1}
        ],
        "decision_context": {"type": "declare_blockers"},
    }
    response = {
        "archetype": f"Fixture {kind} engine",
        "primary_plan": preference,
        "backup_plan": "Develop remaining resources after the engine is disrupted.",
        "card_roles": {
            "1001": "Conditional engine; preserve or replay according to its rules.",
            "1002": "Green mana and land drops.",
        },
        "commanders": [
            {
                "card": 1001,
                "deployment": "Deploy when it advances the engine.",
                "ongoing_value": "Compare remaining abilities with available substitutes.",
                "replay_value": "Re-entry can rebuild only what the cited trigger supplies; counters are lost.",
                "resource_comparison": {
                    "starting_resources": "Commander and a replaceable body.",
                    "preserve_original": "Spend substitute; retain commander and invested resources.",
                    "spend_and_recover": "Spend commander; pay recovery and add supported trigger value.",
                    "decision_test": condition,
                },
                "preserve_or_reuse": preference,
                "constraints": "Respect tax, timing, colored mana and lost counters.",
            }
        ]
        if commanders
        else [],
        "mechanisms": [
            {
                "id": "engine",
                "cards": [1001],
                "evidence": [{"card": 1001, "rule": 1}],
                "effect": preference,
                "requires": [condition],
                "avoid": ["Spending mana needed to survive."],
            }
        ],
        "decision_rules": [
            {
                "id": "resource_trade",
                "decisions": ["combat", "commander_zone"] if commanders else ["development"],
                "mechanisms": ["engine"],
                "baseline": "Preserve material and spend replaceable resources first.",
                "when": condition,
                "prefer": preference,
                "unless": "The alternative prevents lethal or recovery is unaffordable or too slow.",
            }
        ],
        "phases": {
            phase: f"{phase}: develop the supported engine." for phase in ("early", "mid", "late", "recovery")
        },
        "spoken_summary": preference,
    }
    return state, catalog, response


def playbook_for(kind="replay"):
    state, catalog, response = deck_case(kind)
    return DeckPlaybook.parse(json.dumps(response), catalog, commander_ids(state), deck_identity(state))
