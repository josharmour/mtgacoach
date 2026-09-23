"""The legality sanitizer must not rewrite sound LLM advice into other actions.

Field evidence (standalone.log, 2026-09-01..20): ~13% of advice was
"Replaced illegal advice", often into a different or opposite action:
- "Cast Flare of Cultivation — it's your win condition" had its head stripped
  by the local mana estimate although GRE tagged the cast [OK];
- "Attack with all seven creatures" in Main 1 became "Cast Kogla";
- "Done — keep both back to block" became "Declare Attackers: Chomping Changeling".
"""

from __future__ import annotations

from typing import Any

from arenamcp.coach import CoachEngine


def _make_coach() -> CoachEngine:
    class _Stub:
        timeout_s = 5.0

        def complete(self, *a, **k):
            return ""

    return CoachEngine(backend=_Stub())


def _creature(name: str, seat: int = 1) -> dict[str, Any]:
    return {
        "name": name,
        "type_line": "Creature — Beast",
        "power": 4,
        "toughness": 4,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "is_tapped": False,
        "turns_on_battlefield": 2,
    }


def _state(legal_actions, *, phase="Phase_Main1", active=1, hand=None, battlefield=None, decision=None):
    state: dict[str, Any] = {
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "turn": {
            "active_player": active,
            "priority_player": 1,
            "turn_number": 6,
            "phase": phase,
            "step": "Step_Main",
        },
        "hand": hand or [],
        "battlefield": battlefield or [],
        "graveyard": [],
        "stack": [],
        "exile": [],
        "legal_actions": legal_actions,
    }
    if decision:
        state["decision_context"] = decision
    return state


def test_gre_ok_cast_is_not_stripped_by_local_mana_estimate():
    coach = _make_coach()
    # No lands on battlefield: the local pool estimate says 0 mana, but GRE
    # found an autotap solution (alternative cost / mana creatures).
    state = _state(
        ["Cast Flare of Cultivation [OK]", "Cast Woodfall Primus", "Pass"],
        hand=[
            {"name": "Flare of Cultivation", "type_line": "Sorcery", "mana_cost": "{1}{G}{G}"},
            {"name": "Woodfall Primus", "type_line": "Creature", "mana_cost": "{5}{G}{G}{G}"},
        ],
    )
    out = coach._postprocess_advice("Cast Flare of Cultivation — it's your win condition.", state)
    assert out.startswith("Cast Flare of Cultivation")
    assert "win condition" in out
    assert "LOCAL FALLBACK" not in out


def test_uncastable_cast_without_gre_ok_is_still_stripped():
    coach = _make_coach()
    state = _state(
        ["Cast Woodfall Primus", "Pass"],
        hand=[{"name": "Woodfall Primus", "type_line": "Creature", "mana_cost": "{5}{G}{G}{G}"}],
    )
    out = coach._postprocess_advice("Cast Woodfall Primus.", state)
    assert not out.startswith("Cast Woodfall Primus")


def test_precombat_attack_plan_survives_in_main1():
    coach = _make_coach()
    state = _state(
        ["Cast Kogla, the Titan Ape [OK]", "Play Land: Forest", "Pass"],
        hand=[{"name": "Kogla, the Titan Ape", "type_line": "Creature", "mana_cost": "{3}{G}{G}{G}"}],
        battlefield=[_creature("Emrakul, the Aeons Torn"), _creature("Thorn Mammoth")],
    )
    advice = "Attack with Emrakul and Thorn Mammoth — 26 damage is lethal through Kefnet."
    out = coach._postprocess_advice(advice, state)
    assert "Attack with Emrakul" in out
    assert "Kogla" not in out
    assert "LOCAL FALLBACK" not in out


def test_keep_back_at_declare_attackers_is_not_inverted():
    coach = _make_coach()
    state = _state(
        ["Declare Attackers: Chomping Changeling", "Done (confirm attackers)"],
        phase="Phase_Combat",
        battlefield=[_creature("Chomping Changeling")],
        decision={"type": "declare_attackers"},
    )
    out = coach._postprocess_advice("Done — keep both back to block the 13-power board.", state)
    assert "Declare Attackers" not in out
    assert out.lower().startswith("don't attack")
    assert "keep both back" in out


def test_block_plan_on_opponent_main1_survives():
    coach = _make_coach()
    state = _state(
        ["Pass"],
        active=2,
        battlefield=[_creature("Loot, the Pathfinder"), _creature("Elder Gargaroth", seat=2)],
    )
    advice = "Block Elder Gargaroth with Loot if it attacks."
    out = coach._postprocess_advice(advice, state)
    assert "Block Elder Gargaroth" in out
