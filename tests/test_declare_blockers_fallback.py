"""The postprocess fallback during declare blockers gives block guidance, never a menu action."""

from __future__ import annotations

from arenamcp.coach import CoachEngine


def _blockers_window():
    return {
        "pending_decision": "Declare Blockers",
        "legal_actions_raw": ["Activate Ability: Wooded Foothills", "Pass"],
        "players": [{"is_local": True, "seat_id": 1, "life_total": 7}],
        "turn": {"turn_number": 6, "phase": "combat", "step": "declareblockers"},
        "land_drop": {},
        "hand": [],
        "battlefield": [],
        "stack": [],
    }


def test_rejected_advice_during_blocks_falls_back_to_neutral_block_guidance():
    """With no verifiable block actions, the fallback must NOT pick a random
    legal action (e.g. 'Activate Ability: Wooded Foothills'); it gives the
    neutral block line instead (coach_postprocess, declare-blockers branch)."""
    out = CoachEngine()._postprocess_advice("Activate Wooded Foothills.", _blockers_window())
    assert out == "[LOCAL FALLBACK] Choose which creatures to block."


def test_block_advice_passes_through_untouched():
    advice = "Block with Cityscape Leveler and Arboreal Grazer to survive."
    assert CoachEngine()._postprocess_advice(advice, _blockers_window()) == advice
