"""Each card fact reaches a prompt once (2026-10-18 prompt-size audit).

Recorded boards (bug reports of 2026-10-10..18) sent decision prompts that
quoted the same rules several times: Arena's card data repeats abilities in
markup variants, so Yuriko's and Divining Duelist's triggers appeared three
times per zone and the cached Brawl playbooks quoted every "+1/+1 counter"
rule three times. The facts stay; only the repeats go.
"""

import json
from copy import deepcopy

from tests.deck_fakes import deck_case

from arenamcp.coach import CoachEngine
from arenamcp.deck_strategy import DeckPlaybook, commander_ids, deck_identity
from arenamcp.match_context import oracle_lines, prepare_match_context

# Divining Duelist as Arena's card data stores it (set primer FRA.json): the
# trigger in three renderings, the second with <nobr>/<indent> markup.
DUELIST_TEXT = (
    "Flash\nWhen this creature enters, choose one — \n•Tap target creature. \n•Untap target creature. \n•Draw a "
    "card, then discard a card.\nWhen this creature enters, choose one<nobr> —</nobr> \n•<indent=4%>Tap target "
    "creature. </indent>\n•<indent=4%>Untap target creature. </indent>\n•<indent=4%>Draw a card, then discard a "
    "card.</indent>\nWhen this creature enters, choose one — \n•Tap target creature. \n•Untap target creature. "
    "\n•Draw a card, then discard a card."
)
DUELIST_RULES = [
    "Flash",
    "When this creature enters, choose one —",
    "•Tap target creature.",
    "•Untap target creature.",
    "•Draw a card, then discard a card.",
]


def _duelist(instance_id, owner, **extra):
    return {
        "instance_id": instance_id,
        "grp_id": 106252,
        "name": "Divining Duelist",
        "type_line": "Creature — Merfolk Wizard",
        "mana_cost": "{1}{U}",
        "power": 1,
        "toughness": 3,
        "oracle_text": DUELIST_TEXT,
        "owner_seat_id": owner,
        "controller_seat_id": owner,
        "turn_entered_battlefield": 2,
        **extra,
    }


def _state(**zones):
    return {
        "match_id": "duplication-test",
        "local_seat_id": 1,
        "turn": {"turn_number": 6, "active_player": 1, "priority_player": 1, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "battlefield": [],
        "hand": [],
        "graveyard": [],
        "exile": [],
        "stack": [],
        "legal_actions": ["Pass"],
        **zones,
    }


def test_arena_renderings_collapse_while_distinct_abilities_keep_their_modes():
    assert oracle_lines(DUELIST_TEXT) == DUELIST_RULES
    two_abilities = (
        "When this creature enters, choose one —\n•Draw a card.\n•Scry 2.\n"
        "When this creature dies, choose one —\n•Draw a card.\n•You gain 3 life."
    )
    assert oracle_lines(two_abilities) == two_abilities.split("\n")


def test_board_and_graveyard_quote_a_repeated_trigger_once_each():
    # No deck list: the zones are the only place these rules are quoted.
    state = _state(battlefield=[_duelist(30, 1)], graveyard=[_duelist(41, 2)])
    context = CoachEngine.__new__(CoachEngine)._format_game_context(state, for_planner=True)
    # Before: three copies in each zone.
    for rule in DUELIST_RULES[1:]:
        assert context.count(rule) == 2, rule
    assert "<" not in context.replace("<-", "")


def test_deck_reference_quotes_rules_without_markup_or_repeats():
    state = _state(hand=[_duelist(30, 1)], deck_cards=[106252] * 4)
    card = {key: _duelist(0, 1)[key] for key in ("name", "type_line", "mana_cost", "power", "toughness")}
    prepared = prepare_match_context(state, card_lookup=lambda gid: {**card, "oracle_text": DUELIST_TEXT})
    quoted = [line for line in prepared["deck_reference"].splitlines() if "| Flash" in line]
    assert len(quoted) == 1
    assert quoted[0].endswith(" | " + " ".join(DUELIST_RULES))


def test_playbook_quotes_a_card_cited_by_two_mechanisms_once():
    state, catalog, response = deck_case("preserve")
    counter_rule = "Whenever you cast a creature spell, put a +1/+1 counter on this creature."
    catalog = deepcopy(catalog)
    catalog[1001]["oracle_text"] = (
        f"{counter_rule}\nWhenever you cast a creature spell, put a <nobr>+1/+1</nobr> counter on this creature."
    )
    second = {**response["mechanisms"][0], "id": "growth", "evidence": [{"card": 1001, "rule": 2}]}
    response["mechanisms"].append(second)
    response["decision_rules"][0]["mechanisms"].append("growth")
    book = DeckPlaybook.parse(json.dumps(response), catalog, commander_ids(state), deck_identity(state))
    rendered = book.render()
    # Before: quoted twice under the first mechanism and once more under the second.
    assert rendered.count(counter_rule) == 1
    assert "<nobr>" not in rendered
    assert "Fixture Growing Captain [grp:1001], rule 2: quoted above" in rendered
