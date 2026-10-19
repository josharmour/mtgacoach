"""Public card rules and uncertainty must survive tactical prompt formatting."""

from collections import Counter

import pytest

from arenamcp.coach import CoachEngine


def _coach():
    return CoachEngine.__new__(CoachEngine)


def _card(name="Engine", **overrides):
    return {
        "name": name,
        "instance_id": 10,
        "owner_seat_id": 1,
        "controller_seat_id": 1,
        "type_line": "Creature — Human",
        "power": 2,
        "toughness": 3,
        "turn_entered_battlefield": 1,
        "oracle_text": "Whenever you attack, draw a card.",
        **overrides,
    }


def _state(**overrides):
    return {
        "turn": {
            "turn_number": 8,
            "active_player": 1,
            "priority_player": 1,
            "phase": "Phase_Main1",
        },
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 18},
        ],
        "battlefield": [],
        "hand": [],
        "legal_actions": ["Pass"],
        **overrides,
    }


def _board(card):
    return "\n".join(
        _coach()._format_board_card(card, 1, 8, {}, Counter({card["name"]: 1}), {}, True, for_planner=True)
    )


@pytest.mark.parametrize(
    "oracle",
    [
        "Whenever you attack, draw a card.",
        "Creatures can't attack you unless their controller pays {2} for each creature.",
        "Spells your opponents cast cost {1} more to cast.",
        "Lifelink, indestructible",
    ],
)
def test_old_permanents_retain_rules_for_tactical_planner(oracle):
    assert oracle in _board(_card(oracle_text=oracle))


def test_board_reports_unknown_and_modified_stats_without_inventing_zero():
    assert "Engine ?/3" in _board(_card(power=None))
    assert "Engine 0/3" in _board(_card(power=0))
    assert "Engine 7/8" in _board(_card(modified_power=7, modified_toughness=8))


def test_board_retains_granted_removed_and_phasing_state():
    rendered = _board(_card(granted_abilities=["Haste"], removed_abilities=["Flying"], is_phased_out=True))
    assert "Granted: Haste" in rendered
    assert "Removed: Flying" in rendered
    assert "PHASED OUT" in rendered


def test_graveyard_and_exile_keep_every_identity_and_rules_with_copy_counts():
    cards = [_card(name=f"Card {number}", instance_id=number) for number in range(15)]
    cards.append(dict(cards[14]))
    state = _state(
        graveyard=cards,
        exile=[
            _card(name="Escaped Spell", owner_seat_id=2, oracle_text="You may cast this card from exile.")
        ],
    )
    rendered = "\n".join(_coach()._format_zones_and_events(state, 1, 2))
    for number in range(15):
        assert f"Card {number}" in rendered
    assert "Card 14 x2" in rendered
    assert "OPP: Escaped Spell" in rendered
    assert "You may cast this card from exile." in rendered


def test_stack_rules_survive_alongside_target_and_order():
    state = _state(
        battlefield=[_card()],
        stack=[
            _card(
                name="Removal",
                instance_id=20,
                owner_seat_id=2,
                controller_seat_id=2,
                targeting=[10],
                oracle_text="Exile target creature.",
            )
        ],
    )
    rendered = "\n".join(_coach()._format_stack_section(state, 1))
    assert "OPP Removal -> targets: Engine" in rendered
    assert "Exile target creature." in rendered


def test_live_context_includes_public_zone_counts_and_unknown_fallback():
    context = _coach()._format_game_context(
        _state(zones={"opponent_hand_count": 4, "library_count": 63, "opponent_library_count": 41}),
        for_planner=True,
    )
    assert "Opp hand: 4 card(s)" in context
    assert "Your library: 63 card(s)" in context
    assert "Opp library: 41 card(s)" in context
    unknown = _coach()._format_game_context(_state(), for_planner=True)
    assert "Opp hand: UNKNOWN" in unknown
    assert "Opp hand: 0" not in unknown


@pytest.mark.parametrize("source", [None, "unknown", "log_zone_membership", "bridge_total_card_count"])
def test_empty_library_requires_evidence_in_tactical_prompt(source):
    zones = {"library_count": 0}
    if source is not None:
        zones["library_count_source"] = source
    context = _coach()._format_game_context(_state(zones=zones), for_planner=True)
    if source in {"log_zone_membership", "bridge_total_card_count"}:
        assert "Your library: 0 card(s)" in context
    else:
        assert "Your library: UNKNOWN" in context
        assert "Your library: 0" not in context


def test_unknown_power_never_becomes_computed_zero_damage_or_dont_attack():
    context = _coach()._format_game_context(_state(battlefield=[_card(power=None)]), for_planner=True)
    assert "Engine ?/3" in context
    assert "Combat estimates unavailable" in context
    assert "Computed optimal attack:" not in context
    assert "/0pwr" not in context


def test_unknown_power_does_not_generate_zero_damage_block_estimate():
    unknown_attacker = _card(owner_seat_id=2, controller_seat_id=2, power=None, is_attacking=True)
    context = "\n".join(
        _coach()._format_block_combat([], [unknown_attacker], {"life_total": 20}, 8, "Combat", set())
    )
    assert "Engine ?/3" in context
    assert "Combat estimates unavailable" in context
    assert "No blocks" not in context


def test_missing_priority_is_unknown_not_opponent_priority():
    state = _state()
    state["turn"]["priority_player"] = 0
    assert "Pri:UNKNOWN" in _coach()._format_game_context(state, for_planner=True)


def test_shared_prefix_deduplicates_only_exact_rules_and_keeps_dynamic_abilities():
    card = _card(grp_id=101, granted_abilities=["Haste"])
    state = _state(battlefield=[card], deck_catalog={101: dict(card)})
    assert card["oracle_text"] in _coach()._format_game_context(state, for_planner=True)
    state["deck_reference"] = "Full rules prefix"
    context = _coach()._format_game_context(state, for_planner=True)
    assert card["oracle_text"] not in context
    assert "Granted: Haste" in context
    card["oracle_text"] += " Whenever this creature dies, draw a card."
    assert card["oracle_text"] in _coach()._format_game_context(state, for_planner=True)


def test_graveyard_rules_deduplicate_against_full_shared_reference():
    card = _card(grp_id=101)
    state = _state(graveyard=[card], deck_catalog={101: dict(card)}, deck_reference="Full rules prefix")
    context = "\n".join(_coach()._format_zones_and_events(state, 1, 2))
    assert "YOUR: Engine" in context
    assert card["oracle_text"] not in context


# --- castability tags on the Legal menu ----------------------------------------


class _PassBackend:
    def complete(self, *args, **kwargs) -> str:
        return "Pass."


def _engine() -> CoachEngine:
    return CoachEngine(backend=_PassBackend())


def test_format_game_context_filters_non_ok_command_zone_casts():
    coach = _engine()
    game_state = {
        "players": [
            {"seat_id": 2, "is_local": True, "life_total": 25, "lands_played": 1},
            {"seat_id": 1, "is_local": False, "life_total": 25, "lands_played": 0},
        ],
        "turn": {
            "turn_number": 6,
            "phase": "Phase_Main1",
            "step": "Step_PreCombatMain",
            "active_player": 2,
            "priority_player": 2,
        },
        "battlefield": [
            {
                "name": "Fortified Village",
                "type_line": "Land",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "is_tapped": False,
                "oracle_text": "{T}: Add {G} or {W}.",
                "turn_entered_battlefield": 2,
            },
            {
                "name": "Mystic Monastery",
                "type_line": "Land",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "is_tapped": False,
                "oracle_text": "{T}: Add {U}, {R}, or {W}.",
                "turn_entered_battlefield": 4,
            },
            {
                "name": "Swamp",
                "type_line": "Basic Land — Swamp",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "is_tapped": False,
                "oracle_text": "{T}: Add {B}.",
                "turn_entered_battlefield": 6,
            },
        ],
        "hand": [
            {
                "name": "Farseek",
                "grp_id": 96274,
                "mana_cost": "{1}{G}",
                "type_line": "Sorcery",
                "oracle_text": "Search your library for a Plains, Island, Swamp, or Mountain card, put it onto the battlefield tapped, then shuffle.",
            },
        ],
        "command": [
            {
                "name": "Hei Bai, Forest Guardian",
                "grp_id": 98284,
                "mana_cost": "{3}{G}",
                "type_line": "Legendary Creature",
                "oracle_text": "When Hei Bai enters, reveal cards from the top of your library until you reveal a Shrine card.",
            },
        ],
        "graveyard": [],
        "stack": [],
        "legal_actions": [
            "Cast Hei Bai, Forest Guardian",
            "Cast Farseek [OK]",
            "Action: Activate_Mana",
            "Pass",
        ],
        "legal_actions_raw": [
            {
                "actionType": "ActionType_Cast",
                "grpId": 98284,
                "instanceId": 245,
                "manaCost": [
                    {"color": ["ManaColor_Generic"], "count": 3},
                    {"color": ["ManaColor_Green"], "count": 1},
                ],
            },
            {
                "actionType": "ActionType_Cast",
                "grpId": 96274,
                "instanceId": 546,
                "autoTapSolution": {"autoTapActions": []},
                "manaCost": [
                    {"color": ["ManaColor_Generic"], "count": 1},
                    {"color": ["ManaColor_Green"], "count": 1},
                ],
            },
            {"actionType": "ActionType_Pass"},
        ],
    }

    context = coach._format_game_context(game_state)

    assert "Legal: Cast Farseek [OK]" in context
    assert "Legal: Cast Hei Bai, Forest Guardian" not in context
    assert '"grpId":98284' not in context


def test_buff_aura_with_only_enemy_creature_is_marked_no_targets():
    """Regression for issue #236: coach recommended casting Radiant Grace (a buff
    Aura) when the local player controlled no creatures and the only enchantable
    creature belonged to the opponent. Buffing the enemy's creature is never the
    play, so the card must be flagged [NO TARGETS] and dropped from Legal."""
    coach = _engine()
    game_state = {
        "players": [
            {"seat_id": 2, "is_local": True, "life_total": 25, "lands_played": 0},
            {"seat_id": 1, "is_local": False, "life_total": 23, "lands_played": 1},
        ],
        "turn": {
            "turn_number": 5,
            "phase": "Phase_Main1",
            "step": "Step_PreCombatMain",
            "active_player": 2,
            "priority_player": 2,
        },
        "battlefield": [
            {
                "name": "Plains",
                "type_line": "Basic Land — Plains",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "is_tapped": False,
                "oracle_text": "{T}: Add {W}.",
                "turn_entered_battlefield": 2,
            },
            {
                "name": "Plains",
                "type_line": "Basic Land — Plains",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "is_tapped": False,
                "oracle_text": "{T}: Add {W}.",
                "turn_entered_battlefield": 4,
            },
            # Opponent's creature — the only enchantable target on the board.
            {
                "name": "Fanatic of Rhonas",
                "type_line": "Creature — Snake",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "is_tapped": False,
                "power": 2,
                "toughness": 2,
                "card_types": ["CardType_Creature"],
                "turn_entered_battlefield": 3,
            },
        ],
        "hand": [
            {
                "name": "Radiant Grace",
                "grp_id": 78812,
                "mana_cost": "{W}",
                "type_line": "Enchantment — Aura",
                "oracle_text": "Enchant creature\nEnchanted creature gets +1/+0 and has vigilance.",
            },
        ],
        "graveyard": [],
        "stack": [],
        "legal_actions": [
            "Cast Radiant Grace [OK]",
            "Pass",
        ],
        "legal_actions_raw": [
            {
                "actionType": "ActionType_Cast",
                "grpId": 78812,
                "instanceId": 340,
                "manaCost": [{"color": ["ManaColor_White"], "count": 1}],
            },
            {"actionType": "ActionType_Pass"},
        ],
    }

    context = coach._format_game_context(game_state)

    assert "Radiant Grace (AURA) {W} [S,OK] [NO TARGETS]" in context
    assert "Cast Radiant Grace" not in context.split("Legal:", 1)[1].split("\n", 1)[0]


def test_debuff_aura_with_enemy_creature_is_castable():
    """Inverse of the buff-aura case: a removal Aura (Pacifism-style) targets the
    opponent's creature, so it must stay castable when the opponent has one."""
    coach = _engine()
    game_state = {
        "players": [
            {"seat_id": 2, "is_local": True, "life_total": 25, "lands_played": 0},
            {"seat_id": 1, "is_local": False, "life_total": 23, "lands_played": 1},
        ],
        "turn": {
            "turn_number": 5,
            "phase": "Phase_Main1",
            "step": "Step_PreCombatMain",
            "active_player": 2,
            "priority_player": 2,
        },
        "battlefield": [
            {
                "name": "Plains",
                "type_line": "Basic Land — Plains",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "is_tapped": False,
                "oracle_text": "{T}: Add {W}.",
                "turn_entered_battlefield": 2,
            },
            {
                "name": "Plains",
                "type_line": "Basic Land — Plains",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "is_tapped": False,
                "oracle_text": "{T}: Add {W}.",
                "turn_entered_battlefield": 4,
            },
            {
                "name": "Fanatic of Rhonas",
                "type_line": "Creature — Snake",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "is_tapped": False,
                "power": 2,
                "toughness": 2,
                "card_types": ["CardType_Creature"],
                "turn_entered_battlefield": 3,
            },
        ],
        "hand": [
            {
                "name": "Reprobation",
                "grp_id": 71364,
                "mana_cost": "{1}{W}",
                "type_line": "Enchantment — Aura",
                "oracle_text": "Enchant creature\nEnchanted creature loses all abilities and is a Coward creature with base power and toughness 0/1.",
            },
        ],
        "graveyard": [],
        "stack": [],
        "legal_actions": [
            "Cast Reprobation [OK]",
            "Pass",
        ],
        "legal_actions_raw": [
            {
                "actionType": "ActionType_Cast",
                "grpId": 71364,
                "instanceId": 440,
                "manaCost": [
                    {"color": ["ManaColor_Generic"], "count": 1},
                    {"color": ["ManaColor_White"], "count": 1},
                ],
            },
            {"actionType": "ActionType_Pass"},
        ],
    }

    context = coach._format_game_context(game_state)

    assert "[NO TARGETS]" not in context
    assert "Cast Reprobation" in context.split("Legal:", 1)[1].split("\n", 1)[0]
