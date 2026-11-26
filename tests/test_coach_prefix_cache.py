"""Tests for Manual Coach prefix-cache boundary stability across turns."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from tests.deck_fakes import deck_case

from arenamcp.coach import CoachEngine
from arenamcp.coach_prompts import DEFAULT_SYSTEM_PROMPT, situational_coach_addendum
from arenamcp.deck_strategy import DeckPlaybook, commander_ids, deck_identity
from arenamcp.match_context import prepare_match_context, reset_seen_rules


class _RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def complete(self, system_prompt: str, user_message: str, *args, **kwargs) -> str:
        self.calls.append({"system": system_prompt, "user": user_message})
        return "Cast Grizzly Bears."


def _base_state(deck_cards: list[int] | None = None) -> dict:
    return {
        "match_id": "coach-cache-test",
        "local_seat_id": 1,
        "turn": {"turn_number": 3, "active_player": 1, "priority_player": 1, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "deck_cards": deck_cards if deck_cards is not None else [1001, 1002],
        "hand": [
            {
                "instance_id": 10,
                "grp_id": 1001,
                "name": "Grizzly Bears",
                "type_line": "Creature — Bear",
                "mana_cost": "{1}{G}",
                "power": 2,
                "toughness": 2,
                "oracle_text": "",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
            }
        ],
        "battlefield": [
            {
                "instance_id": 11,
                "grp_id": 1002,
                "name": "Forest",
                "type_line": "Basic Land — Forest",
                "mana_cost": "",
                "oracle_text": "({T}: Add {G}.)",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
            }
        ],
        "graveyard": [],
        "exile": [],
        "stack": [],
        "legal_actions": ["Cast Grizzly Bears"],
    }


def _card_lookup(grp_id: int) -> dict:
    catalog = {
        1001: {
            "name": "Grizzly Bears",
            "type_line": "Creature — Bear",
            "mana_cost": "{1}{G}",
            "cmc": 2,
            "power": "2",
            "toughness": "2",
            "oracle_text": "",
        },
        1002: {
            "name": "Forest",
            "type_line": "Basic Land — Forest",
            "mana_cost": "",
            "cmc": 0,
            "oracle_text": "({T}: Add {G}.)",
        },
        2001: {
            "name": "Bonesplitter",
            "type_line": "Artifact — Equipment",
            "mana_cost": "{1}",
            "cmc": 1,
            "oracle_text": "Equipped creature gets +2/+0.\nEquip {1}",
        },
    }
    return catalog[grp_id]


def test_coach_system_prompt_stays_byte_identical_when_dynamic_subjects_appear():
    reset_seen_rules()
    backend = _RecordingBackend()
    coach = CoachEngine(backend=backend)
    coach._ensure_game_plan_mgr = lambda: None
    coach._rules_db = SimpleNamespace(get_rules_for_situation=lambda *args, **kwargs: [])
    state, catalog, response = deck_case("preserve")
    playbook = DeckPlaybook.parse(json.dumps(response), catalog, commander_ids(state), deck_identity(state))
    coach._deck_playbook = playbook

    t1 = _base_state()
    t2 = deepcopy(t1)
    t2["turn"]["turn_number"] = 4
    # Opponent plays a Secrets of Strixhaven card and we gain an Equipment not in deck_cards.
    t2["battlefield"].append(
        {
            "instance_id": 99,
            "grp_id": 2001,
            "name": "Bonesplitter",
            "type_line": "Artifact — Equipment",
            "mana_cost": "{1}",
            "oracle_text": "Equipped creature gets +2/+0.\nEquip {1}",
            "owner_seat_id": 1,
            "controller_seat_id": 1,
        }
    )
    t2["battlefield"].append(
        {
            "instance_id": 100,
            "grp_id": 2002,
            "name": "Hallway Heckler",
            "type_line": "Creature — Elemental Sorcerer",
            "mana_cost": "{2}{R}",
            "oracle_text": "This creature enters prepared.",
            "owner_seat_id": 2,
            "controller_seat_id": 2,
        }
    )

    with patch("arenamcp.match_context._local_card", lambda gid, _time: _card_lookup(gid)):
        prepare_match_context(t1, card_lookup=_card_lookup)
        coach.get_advice(t1, trigger="new_turn", style="quick")
        prepare_match_context(t2, card_lookup=_card_lookup)
        coach.get_advice(t2, trigger="new_turn", style="quick")

    assert len(backend.calls) == 2
    sys_t1 = backend.calls[0]["system"]
    sys_t2 = backend.calls[1]["system"]
    usr_t2 = backend.calls[1]["user"]

    assert sys_t1 == sys_t2
    assert "DECK REFERENCE" in sys_t1
    assert "Primary plan:" in sys_t1
    assert "- EQUIPMENT & TARGETING:" not in sys_t1
    assert "SECRETS OF STRIXHAVEN" not in sys_t1
    assert "- EQUIPMENT & TARGETING:" in usr_t2
    assert "SECRETS OF STRIXHAVEN" in usr_t2
    assert "Bonesplitter" in usr_t2


def test_coach_includes_equipment_in_system_prompt_only_when_our_deck_has_it():
    reset_seen_rules()
    backend = _RecordingBackend()
    coach = CoachEngine(backend=backend)
    coach._ensure_game_plan_mgr = lambda: None
    coach._rules_db = SimpleNamespace(get_rules_for_situation=lambda *args, **kwargs: [])

    own_equip_state = _base_state(deck_cards=[1001, 1002, 2001])
    own_equip_state["hand"].append(
        {
            "instance_id": 20,
            "grp_id": 2001,
            "name": "Bonesplitter",
            "type_line": "Artifact — Equipment",
            "mana_cost": "{1}",
            "oracle_text": "Equipped creature gets +2/+0.\nEquip {1}",
            "owner_seat_id": 1,
            "controller_seat_id": 1,
        }
    )

    with patch("arenamcp.match_context._local_card", lambda gid, _time: _card_lookup(gid)):
        prepare_match_context(own_equip_state, card_lookup=_card_lookup)
        coach.get_advice(own_equip_state, trigger="new_turn", style="quick")

    assert "- EQUIPMENT & TARGETING:" in backend.calls[0]["system"]
    assert "- EQUIPMENT & TARGETING:" not in backend.calls[0]["user"]
    assert "- EQUIPMENT & TARGETING:" in situational_coach_addendum(DEFAULT_SYSTEM_PROMPT, {"equipment"})
