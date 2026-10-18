"""Double strike and lifelink in the combat solver (2026-10-18 pro game, match 2 turn 11).

Dina, Flesh Sower (6/6 lifelink) attacked into Tetsuko Umezawa, Pursuer
(2/4 double strike, prowess) with mana open; Blazing Crescendo (+3/+1) made
Tetsuko a 5/5 double striker and it killed Dina. The solver counted a
double-strike blocker's damage once and dropped lifelink entirely.
"""

from __future__ import annotations

from arenamcp.combat_solver import _resolve_attacker, evaluate_attack


def _creature(iid, name, power, toughness, *keywords):
    return {"instance_id": iid, "name": name, "type_line": "Creature — Test", "power": power,
            "toughness": toughness, "keywords": list(keywords)}  # fmt: skip


DINA = _creature(211, "Dina, Flesh Sower", 6, 6, "lifelink")
TETSUKO = _creature(254, "Tetsuko Umezawa, Pursuer", 2, 4, "double strike")


def test_a_double_strike_blocker_deals_damage_in_both_steps():
    bear = _creature(1, "Bear", 3, 3)
    outcome = _resolve_attacker(bear, [TETSUKO])
    assert outcome.attacker_died  # 2 first-strike + 2 regular
    assert outcome.blockers_died == []


def test_dina_beats_tetsuko_unless_it_is_pumped():
    plain = _resolve_attacker(DINA, [TETSUKO])
    assert not plain.attacker_died and plain.blockers_died == [TETSUKO]
    assert plain.life_gained == 6  # lifelink: all 6 is dealt, the excess on Tetsuko too
    crescendo = {**TETSUKO, "power": 5, "toughness": 5}  # Blazing Crescendo: +3/+1
    pumped = _resolve_attacker(DINA, [crescendo])
    assert pumped.attacker_died  # 5 first-strike + 5 regular


def test_a_double_strike_attackers_first_hit_counts_toward_lethal():
    striker = _creature(2, "Striker", 2, 2, "double strike")
    wall = _creature(3, "Wall", 0, 3)
    assert _resolve_attacker(striker, [wall]).blockers_died == [wall]
    assert _resolve_attacker(striker, []).damage_through == 4


def test_first_strike_blockers_strike_simultaneously_with_a_first_strike_attacker():
    knight = _creature(4, "Knight", 2, 2, "first strike")
    duelist = _creature(5, "Duelist", 2, 2, "first strike")
    outcome = _resolve_attacker(knight, [duelist])
    assert outcome.attacker_died and outcome.blockers_died == [duelist]


def test_lifelink_damage_is_life_for_the_crackback():
    # Unblocked Dina gains 6: at 20 life a 24-power crackback is survivable (26).
    attackers_next_turn = [_creature(900 + i, f"Giant {i}", 8, 8) for i in range(3)]
    plan = evaluate_attack([DINA], [], [], 20, 20, attackers_next_turn)
    assert plan.life_gained == 6 and plan.score > -1_000
    vanilla = evaluate_attack([{**DINA, "keywords": []}], [], [], 20, 20, attackers_next_turn)
    assert vanilla.life_gained == 0 and vanilla.score == -1_000
