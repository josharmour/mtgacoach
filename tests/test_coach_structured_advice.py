"""Structured coach answers: numbered CHOICES in, {"action": N, "say": ...} out."""

from __future__ import annotations

from typing import Any

import pytest

from arenamcp.coach import CoachEngine
from arenamcp.coach_structured import (
    build_choices,
    is_verified,
    parse_structured_advice,
)

CHOICES = ["Cast Kogla, the Titan Ape [OK]", "Play Land: Forest", "Pass"]


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("MTGACOACH_STRUCTURED_ADVICE", "1")


def test_parse_plain_json():
    p = parse_structured_advice('{"action": 2, "say": "Play Forest first."}', CHOICES)
    assert p is not None and p.index == 2 and p.action == "Play Land: Forest"
    assert p.say == "Play Forest first."


def test_parse_fenced_and_chatter():
    text = 'Sure!\n```json\n{"action": "1", "say": "Cast Kogla — it eats their artifact."}\n```'
    p = parse_structured_advice(text, CHOICES)
    assert p is not None and p.action == CHOICES[0]


def test_parse_truncated_json_salvages_say():
    p = parse_structured_advice('{"action": 3, "say": "Pass and hold up the trick', CHOICES)
    assert p is not None and p.action == "Pass"
    assert p.say.startswith("Pass and hold up")


def test_parse_out_of_range_index_becomes_plan():
    p = parse_structured_advice('{"action": 9, "say": "Attack with everything."}', CHOICES)
    assert p is not None and p.index == 0 and p.action is None


def test_parse_free_text_returns_none():
    assert parse_structured_advice("Cast Kogla, the Titan Ape.", CHOICES) is None


def test_empty_say_uses_action_text():
    p = parse_structured_advice('{"action": 1, "say": ""}', CHOICES)
    assert p is not None and p.say == "Cast Kogla, the Titan Ape."


def test_unpayable_gre_cast_is_not_verified():
    gs = {"legal_actions": ["Cast Big Thing", "Pass"]}
    p = parse_structured_advice('{"action": 1, "say": "Cast Big Thing."}', ["Cast Big Thing", "Pass"])
    assert p is not None and not is_verified(p, gs)
    p2 = parse_structured_advice('{"action": 2, "say": "Pass."}', ["Cast Big Thing", "Pass"])
    assert p2 is not None and is_verified(p2, gs)


def test_mulligan_has_no_choices():
    assert build_choices({"pending_decision": "Mulligan", "legal_actions": ["KEEP"]}) == []


# ── end to end through get_advice ─────────────────────────────────────────


class _Backend:
    timeout_s = 5.0

    def __init__(self, reply: str):
        self.reply = reply
        self.last_user = ""

    def complete(self, system_prompt, user_message, *a, **k):
        self.last_user = user_message
        return self.reply


def _state(legal_actions: list[str]) -> dict[str, Any]:
    return {
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "turn": {"active_player": 1, "priority_player": 1, "turn_number": 6,
                 "phase": "Phase_Main1", "step": "Step_Main"},
        "hand": [{"name": "Kogla, the Titan Ape", "type_line": "Creature — Ape",
                  "mana_cost": "{3}{G}{G}{G}"}],
        "battlefield": [], "graveyard": [], "stack": [], "exile": [],
        "legal_actions": legal_actions,
    }


def test_get_advice_verified_pick_is_not_replaced():
    be = _Backend('{"action": 1, "say": "Cast Kogla to fight their best creature."}')
    coach = CoachEngine(backend=be)
    out = coach.get_advice(_state(CHOICES), trigger="new_turn", style="quick")
    assert "CHOICES" in be.last_user and "1. Cast Kogla, the Titan Ape [OK]" in be.last_user
    assert out.startswith("Cast Kogla")
    assert "LOCAL FALLBACK" not in out
    assert coach.last_structured_choice == {
        "index": 1, "action": CHOICES[0], "verified": True, "trigger": "new_turn",
    }


def test_get_advice_verified_pick_survives_even_if_text_is_loose():
    # The spoken line doesn't literally contain the legal-action string; the
    # old matcher would have replaced it with a guessed action.
    be = _Backend('{"action": 2, "say": "Make your land drop and keep the Ape for next turn."}')
    coach = CoachEngine(backend=be)
    out = coach.get_advice(_state(CHOICES), trigger="new_turn", style="quick")
    assert out.startswith("Make your land drop")
    assert coach.last_structured_choice["action"] == "Play Land: Forest"


def test_get_advice_plan_choice_zero_goes_through_text_path():
    be = _Backend('{"action": 0, "say": "Attack with everything this turn."}')
    coach = CoachEngine(backend=be)
    out = coach.get_advice(_state(CHOICES), trigger="new_turn", style="quick")
    assert "Attack with everything" in out
    assert coach.last_structured_choice["verified"] is False


def test_get_advice_free_text_reply_still_works():
    be = _Backend("Cast Kogla, the Titan Ape.")
    coach = CoachEngine(backend=be)
    out = coach.get_advice(_state(CHOICES), trigger="new_turn", style="quick")
    assert "Kogla" in out
    assert coach.last_structured_choice is None


def test_get_advice_broken_json_is_never_spoken_raw():
    be = _Backend('{"action": }')
    coach = CoachEngine(backend=be)
    out = coach.get_advice(_state(CHOICES), trigger="new_turn", style="quick")
    assert "{" not in out and '"' not in out


def test_questions_stay_free_text():
    be = _Backend("Kogla fights on entry.")
    coach = CoachEngine(backend=be)
    coach.get_advice(_state(CHOICES), question="What does Kogla do?", style="quick")
    assert "CHOICES" not in be.last_user


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("MTGACOACH_STRUCTURED_ADVICE", "0")
    be = _Backend("Cast Kogla, the Titan Ape.")
    coach = CoachEngine(backend=be)
    coach.get_advice(_state(CHOICES), trigger="new_turn", style="quick")
    assert "CHOICES" not in be.last_user
