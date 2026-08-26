"""Regression: a Mulligan decision must never emit normal-play casting advice.

bug_20260825_205102: during a KEEP/MULLIGAN decision the coach said
"Cast Vorinclex, Monstrous Raider." — the LLM (primed on the deck plan and
turn-1 legal actions) answered with a casting plan, and the mana-budget
stripper collapsed it to a bare legal action. A mulligan has exactly two valid
answers, so the postprocessor pins the output to KEEP/MULLIGAN.
"""

from __future__ import annotations

import pytest

from arenamcp.coach import CoachEngine
from arenamcp.coach_postprocess import _mulligan_hand_call, _mulligan_keep_or_mulligan

_MULLIGAN_HAND = [
    {"name": "Forest", "type_line": "Basic Land — Forest"},
    {"name": "Forest", "type_line": "Basic Land — Forest"},
    {"name": "Forest", "type_line": "Basic Land — Forest"},
    {"name": "Vorinclex, Monstrous Raider", "type_line": "Legendary Creature", "mana_cost": "{4}{G}{G}"},
    {"name": "Evolution Sage", "type_line": "Creature — Elf Druid", "mana_cost": "{2}{G}"},
    {"name": "Unnatural Restoration", "type_line": "Sorcery", "mana_cost": "{1}{G}"},
    {"name": "Strength of Will", "type_line": "Instant", "mana_cost": "{1}{G}"},
]


def _mulligan_gs():
    return {
        "pending_decision": "Mulligan",
        "hand": [dict(c) for c in _MULLIGAN_HAND],
        "players": [{"is_local": True, "seat_id": 1, "life_total": 25}],
        "turn": {"turn_number": 0, "phase": ""},
        "land_drop": {},
        "legal_actions_raw": ["KEEP", "MULLIGAN"],
    }


# ---- helper extraction / fallback ------------------------------------------


def test_casting_advice_becomes_a_mulligan_call_not_a_cast():
    assert _mulligan_keep_or_mulligan("Cast Vorinclex, Monstrous Raider.", _mulligan_gs()) == "KEEP"


def test_extract_mulligan_vs_keep():
    gs = _mulligan_gs()
    assert _mulligan_keep_or_mulligan("Mulligan this hand is too slow.", gs) == "MULLIGAN"
    assert _mulligan_keep_or_mulligan("Keep this hand, solid curve.", gs) == "KEEP"


def test_hand_call_bad_keeps():
    def gs(n_lands, n_creatures=0):
        hand = [{"type_line": "Basic Land"}] * n_lands + [{"type_line": "Creature"}] * n_creatures
        return {"hand": hand}

    assert _mulligan_hand_call(gs(1)) == "MULLIGAN"
    assert _mulligan_hand_call(gs(5)) == "MULLIGAN"
    assert _mulligan_hand_call(gs(2, 0)) == "MULLIGAN"
    assert _mulligan_hand_call(gs(3)) == "KEEP"
    assert _mulligan_hand_call(gs(2, 1)) == "KEEP"


# ---- full postprocess path (the actual bug) --------------------------------


@pytest.mark.parametrize(
    "bad_advice",
    [
        "Cast Vorinclex, Monstrous Raider.",
        "Play Forest then cast Vorinclex, Monstrous Raider and Evolution Sage.",
        "Cast Evolution Sage, then cast Vorinclex.",
    ],
)
def test_postprocess_never_leaks_cast_or_play_during_mulligan(bad_advice):
    out = CoachEngine()._postprocess_advice(bad_advice, _mulligan_gs())
    low = out.lower()
    assert "cast" not in low and "play " not in low, f"leaked: {out!r}"
    assert "keep" in low or "mulligan" in low, f"not a mulligan call: {out!r}"


def test_normal_play_advice_is_unchanged():
    normal = {
        "pending_decision": "Priority",
        "hand": [{"name": "Llanowar Elves", "type_line": "Creature", "mana_cost": "{G}"}],
        "players": [{"is_local": True, "seat_id": 1, "life_total": 20}],
        "turn": {"turn_number": 1, "phase": "main1"},
        "land_drop": {},
    }
    out = CoachEngine()._postprocess_advice("Cast Llanowar Elves.", normal)
    # The mulligan pin must be scoped to Mulligan decisions only — it must
    # never force a normal turn into a KEEP/MULLIGAN call.
    assert out not in ("KEEP", "MULLIGAN")
