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


def test_fight_memo_distinguishes_bool_counters_from_one():
    """``is_creature_modified`` rejects bool counters, so True and 1 differ.

    The fight memo keys counters by value; Python treats ``True == 1``, so a
    bool counter must not alias an integer counter that turns on double strike.
    A blocker keeps this on the memoized path: unblocked fights bypass the memo.
    """
    from arenamcp.combat_solver import (
        _RESOLVE_CACHE,
        _resolve_attacker_damage,
        _resolve_attacker_damage_uncached,
    )

    attacker = {
        "instance_id": 811,
        "name": "Conditional",
        "type_line": "Creature — Test",
        "power": 3,
        "toughness": 3,
        "oracle_text": "This creature has double strike as long as it's modified.",
        "counters": {"test": True},
    }
    blocker = {
        "instance_id": 812,
        "name": "Wall",
        "type_line": "Creature — Test",
        "power": 1,
        "toughness": 4,
    }
    _RESOLVE_CACHE.clear()
    first = _resolve_attacker_damage(attacker, [blocker])
    assert _RESOLVE_CACHE, "blocked fight must exercise the memo"
    # Bool counter: not modified, no double strike; three damage leaves a 1/4.
    assert first.blockers_died == [] and first.damage_through == 0
    attacker["counters"]["test"] = 1
    second = _resolve_attacker_damage(attacker, [blocker])
    uncached = _resolve_attacker_damage_uncached(attacker, [blocker])
    # Integer counter: modified, double strike kills the 1/4 before it strikes back.
    assert second.blockers_died == uncached.blockers_died == [blocker]
    assert second.damage_through == uncached.damage_through == 0


def test_fight_memo_bypasses_unhashable_counter_values():
    """A list counter value is ignored by the resolver, not a memo key error."""
    from arenamcp.combat_solver import (
        _RESOLVE_CACHE,
        _resolve_attacker_damage,
        _resolve_attacker_damage_uncached,
    )

    attacker = {
        "instance_id": 813,
        "name": "Conditional",
        "type_line": "Creature — Test",
        "power": 3,
        "toughness": 3,
        "oracle_text": "This creature has double strike as long as it's modified.",
        "counters": {"test": []},
    }
    blocker = {
        "instance_id": 814,
        "name": "Wall",
        "type_line": "Creature — Test",
        "power": 1,
        "toughness": 4,
    }
    _RESOLVE_CACHE.clear()
    cached = _resolve_attacker_damage(attacker, [blocker])
    assert not _RESOLVE_CACHE, "unkeyable counters must bypass the memo"
    uncached = _resolve_attacker_damage_uncached(attacker, [blocker])
    assert cached.blockers_died == uncached.blockers_died == []
    assert cached.damage_through == uncached.damage_through == 0


def test_lifelink_damage_is_life_for_the_crackback():
    # Unblocked Dina gains 6: at 20 life a 24-power crackback is survivable (26).
    attackers_next_turn = [_creature(900 + i, f"Giant {i}", 8, 8) for i in range(3)]
    plan = evaluate_attack([DINA], [], [], 20, 20, attackers_next_turn)
    assert plan.life_gained == 6 and plan.score > -1_000
    vanilla = evaluate_attack([{**DINA, "keywords": []}], [], [], 20, 20, attackers_next_turn)
    assert vanilla.life_gained == 0 and vanilla.score == -1_000
