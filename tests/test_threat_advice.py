from arenamcp.coach import CoachEngine


class _DummyBackend:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []
        self.model = "dummy-threat-model"

    def complete(self, system_prompt: str, user_message: str, *args, **kwargs) -> str:
        self.calls.append((system_prompt, user_message))
        return self.response


def _sample_game_state() -> dict:
    return {
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 14},
            {"seat_id": 2, "is_local": False, "life_total": 18},
        ],
        "turn": {"turn_number": 6, "phase": "Phase_Main1", "step": "Step_PreCombatMain"},
        "hand": [
            {
                "name": "Go for the Throat",
                "mana_cost": "{1}{B}",
                "type_line": "Instant",
                "oracle_text": "Destroy target nonartifact creature.",
            },
            {
                "name": "Cut Down",
                "mana_cost": "{B}",
                "type_line": "Instant",
                "oracle_text": "Destroy target creature with total power and toughness 5 or less.",
            },
        ],
        "battlefield": [
            {
                "name": "Swamp",
                "type_line": "Basic Land — Swamp",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "is_tapped": False,
            },
            {
                "name": "Restless Cottage",
                "type_line": "Land",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "is_tapped": False,
            },
            {
                "name": "Mosswood Dreadknight",
                "type_line": "Creature — Human Knight",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "is_tapped": False,
                "power": 3,
                "toughness": 2,
            },
        ],
        "graveyard": [],
        "stack": [],
    }


def test_threat_prompt_uses_specific_card_and_available_answers(monkeypatch):
    backend = _DummyBackend("Use Go for the Throat now before it snowballs.")
    coach = CoachEngine(backend=backend)
    coach._deck_strategy = "Trade resources early, then win with recursive threats."
    coach._rules_db = type("RulesStub", (), {"get_rules_for_situation": lambda *args, **kwargs: []})()
    monkeypatch.setattr(coach, "_format_game_context", lambda state: "CTX")

    threat = {
        "name": "Sheoldred, the Apocalypse",
        "warning": "Drains 2 on your draws, heals on theirs!",
        "card": {
            "name": "Sheoldred, the Apocalypse",
            "type_line": "Legendary Creature — Phyrexian Praetor",
            "oracle_text": "Deathtouch. Whenever you draw a card, you lose 2 life.",
            "power": 4,
            "toughness": 5,
        },
    }

    advice = coach.get_advice(
        _sample_game_state(),
        trigger="threat_detected",
        style="concise",
        threat=threat,
    )

    assert advice.startswith("Sheoldred, the Apocalypse is the key threat.")
    assert backend.calls, "expected backend.complete to be called"
    _system_prompt, user_message = backend.calls[0]
    assert "THREAT ALERT: Sheoldred, the Apocalypse" in user_message
    assert "Available answers now: Go for the Throat" in user_message
    assert "Name Sheoldred, the Apocalypse explicitly in the first sentence." in user_message


def test_threat_fallback_is_specific_when_backend_errors(monkeypatch):
    backend = _DummyBackend("Error: LLM timed out")
    coach = CoachEngine(backend=backend)
    coach._rules_db = type("RulesStub", (), {"get_rules_for_situation": lambda *args, **kwargs: []})()
    monkeypatch.setattr(coach, "_format_game_context", lambda state: "CTX")

    threat = {
        "name": "The Wandering Emperor",
        "warning": "Flash! Can exile or make blockers anytime.",
        "card": {
            "name": "The Wandering Emperor",
            "type_line": "Legendary Planeswalker",
            "oracle_text": "+1: Put a +1/+1 counter on up to one target creature. It gains first strike until end of turn.",
            "counters": {"Loyalty": 3},
        },
    }

    advice = coach.get_advice(
        _sample_game_state(),
        trigger="threat_detected",
        style="concise",
        threat=threat,
    )

    assert "The Wandering Emperor" in advice
    assert "Attack it this turn" in advice or "key threat" in advice


def test_format_game_context_filters_non_ok_command_zone_casts():
    coach = CoachEngine(backend=_DummyBackend("Pass."))
    game_state = {
        "players": [
            {"seat_id": 2, "is_local": True, "life_total": 25, "lands_played": 1},
            {"seat_id": 1, "is_local": False, "life_total": 25, "lands_played": 0},
        ],
        "turn": {"turn_number": 6, "phase": "Phase_Main1", "step": "Step_PreCombatMain", "active_player": 2, "priority_player": 2},
        "battlefield": [
            {"name": "Fortified Village", "type_line": "Land", "owner_seat_id": 2, "controller_seat_id": 2, "is_tapped": False, "oracle_text": "{T}: Add {G} or {W}.", "turn_entered_battlefield": 2},
            {"name": "Mystic Monastery", "type_line": "Land", "owner_seat_id": 2, "controller_seat_id": 2, "is_tapped": False, "oracle_text": "{T}: Add {U}, {R}, or {W}.", "turn_entered_battlefield": 4},
            {"name": "Swamp", "type_line": "Basic Land — Swamp", "owner_seat_id": 2, "controller_seat_id": 2, "is_tapped": False, "oracle_text": "{T}: Add {B}.", "turn_entered_battlefield": 6},
        ],
        "hand": [
            {"name": "Farseek", "grp_id": 96274, "mana_cost": "{1}{G}", "type_line": "Sorcery", "oracle_text": "Search your library for a Plains, Island, Swamp, or Mountain card, put it onto the battlefield tapped, then shuffle."},
        ],
        "command": [
            {"name": "Hei Bai, Forest Guardian", "grp_id": 98284, "mana_cost": "{3}{G}", "type_line": "Legendary Creature", "oracle_text": "When Hei Bai enters, reveal cards from the top of your library until you reveal a Shrine card."},
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
            {"actionType": "ActionType_Cast", "grpId": 98284, "instanceId": 245, "manaCost": [{"color": ["ManaColor_Generic"], "count": 3}, {"color": ["ManaColor_Green"], "count": 1}]},
            {"actionType": "ActionType_Cast", "grpId": 96274, "instanceId": 546, "autoTapSolution": {"autoTapActions": []}, "manaCost": [{"color": ["ManaColor_Generic"], "count": 1}, {"color": ["ManaColor_Green"], "count": 1}]},
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
    coach = CoachEngine(backend=_DummyBackend("Pass."))
    game_state = {
        "players": [
            {"seat_id": 2, "is_local": True, "life_total": 25, "lands_played": 0},
            {"seat_id": 1, "is_local": False, "life_total": 23, "lands_played": 1},
        ],
        "turn": {"turn_number": 5, "phase": "Phase_Main1", "step": "Step_PreCombatMain", "active_player": 2, "priority_player": 2},
        "battlefield": [
            {"name": "Plains", "type_line": "Basic Land — Plains", "owner_seat_id": 2, "controller_seat_id": 2, "is_tapped": False, "oracle_text": "{T}: Add {W}.", "turn_entered_battlefield": 2},
            {"name": "Plains", "type_line": "Basic Land — Plains", "owner_seat_id": 2, "controller_seat_id": 2, "is_tapped": False, "oracle_text": "{T}: Add {W}.", "turn_entered_battlefield": 4},
            # Opponent's creature — the only enchantable target on the board.
            {"name": "Fanatic of Rhonas", "type_line": "Creature — Snake", "owner_seat_id": 1, "controller_seat_id": 1, "is_tapped": False, "power": 2, "toughness": 2, "card_types": ["CardType_Creature"], "turn_entered_battlefield": 3},
        ],
        "hand": [
            {"name": "Radiant Grace", "grp_id": 78812, "mana_cost": "{W}", "type_line": "Enchantment — Aura", "oracle_text": "Enchant creature\nEnchanted creature gets +1/+0 and has vigilance."},
        ],
        "graveyard": [],
        "stack": [],
        "legal_actions": [
            "Cast Radiant Grace [OK]",
            "Pass",
        ],
        "legal_actions_raw": [
            {"actionType": "ActionType_Cast", "grpId": 78812, "instanceId": 340, "manaCost": [{"color": ["ManaColor_White"], "count": 1}]},
            {"actionType": "ActionType_Pass"},
        ],
    }

    context = coach._format_game_context(game_state)

    assert "Radiant Grace (AURA) {W} [S,OK] [NO TARGETS]" in context
    assert "Cast Radiant Grace" not in context.split("Legal:", 1)[1].split("\n", 1)[0]


def test_debuff_aura_with_enemy_creature_is_castable():
    """Inverse of the buff-aura case: a removal Aura (Pacifism-style) targets the
    opponent's creature, so it must stay castable when the opponent has one."""
    coach = CoachEngine(backend=_DummyBackend("Pass."))
    game_state = {
        "players": [
            {"seat_id": 2, "is_local": True, "life_total": 25, "lands_played": 0},
            {"seat_id": 1, "is_local": False, "life_total": 23, "lands_played": 1},
        ],
        "turn": {"turn_number": 5, "phase": "Phase_Main1", "step": "Step_PreCombatMain", "active_player": 2, "priority_player": 2},
        "battlefield": [
            {"name": "Plains", "type_line": "Basic Land — Plains", "owner_seat_id": 2, "controller_seat_id": 2, "is_tapped": False, "oracle_text": "{T}: Add {W}.", "turn_entered_battlefield": 2},
            {"name": "Plains", "type_line": "Basic Land — Plains", "owner_seat_id": 2, "controller_seat_id": 2, "is_tapped": False, "oracle_text": "{T}: Add {W}.", "turn_entered_battlefield": 4},
            {"name": "Fanatic of Rhonas", "type_line": "Creature — Snake", "owner_seat_id": 1, "controller_seat_id": 1, "is_tapped": False, "power": 2, "toughness": 2, "card_types": ["CardType_Creature"], "turn_entered_battlefield": 3},
        ],
        "hand": [
            {"name": "Reprobation", "grp_id": 71364, "mana_cost": "{1}{W}", "type_line": "Enchantment — Aura", "oracle_text": "Enchant creature\nEnchanted creature loses all abilities and is a Coward creature with base power and toughness 0/1."},
        ],
        "graveyard": [],
        "stack": [],
        "legal_actions": [
            "Cast Reprobation [OK]",
            "Pass",
        ],
        "legal_actions_raw": [
            {"actionType": "ActionType_Cast", "grpId": 71364, "instanceId": 440, "manaCost": [{"color": ["ManaColor_Generic"], "count": 1}, {"color": ["ManaColor_White"], "count": 1}]},
            {"actionType": "ActionType_Pass"},
        ],
    }

    context = coach._format_game_context(game_state)

    assert "[NO TARGETS]" not in context
    assert "Cast Reprobation" in context.split("Legal:", 1)[1].split("\n", 1)[0]


def test_system_prompts_warn_against_pointless_protective_abilities():
    """Regression for the Adanto Vanguard report: the coach told the user to
    activate a 'Pay 4 life: indestructible' ability with no blockers and no
    removal on the stack — pure life loss. Both system prompts must carry an
    explicit rule against paying life for protection without a concrete threat."""
    from arenamcp.coach import DEFAULT_SYSTEM_PROMPT, CONCISE_SYSTEM_PROMPT

    for prompt in (DEFAULT_SYSTEM_PROMPT, CONCISE_SYSTEM_PROMPT):
        low = prompt.lower()
        assert "indestructible" in low
        assert "protection" in low or "hexproof" in low
        # The rule must tie protection to an actual threat, not blanket activation.
        assert "threat" in low
