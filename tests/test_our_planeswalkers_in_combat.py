"""Our planeswalkers in combat math (2026-10-18 pro game, match 1, GW Garruk deck).

Turn 10: Saheeli (5/5) and a Thopter attacked Garruk, Curse Breaker; the pro
flashed Teyo (1/1) and chump-blocked Saheeli to keep Garruk alive. Turn 13:
the pro declared the Beast as an attacker, then took it back to protect Garruk
(two loyalty) from the crackback. Crackback used to count only our life.
"""

from __future__ import annotations

from arenamcp.combat_solver import evaluate_attack, optimal_attacks, walker_crackback_cost
from arenamcp.combat_strategy import walker_saving_blocks


def _creature(iid, name, power, toughness, *keywords, seat=1, **extra):
    return {"instance_id": iid, "name": name, "type_line": "Creature — Test", "power": power,
            "toughness": toughness, "keywords": list(keywords), "controller_seat_id": seat,
            "owner_seat_id": seat, **extra}  # fmt: skip


BEAST = _creature(276, "Beast", 4, 4, "trample")
TRAXOS = _creature(266, "Traxos, Academy Guardian", 2, 6, seat=2)
SAGE = _creature(305, "Asymmetry Sage", 3, 2, seat=2)


def test_crackback_that_finishes_our_walker_costs_the_walker():
    assert walker_crackback_cost(5, 19, (3,)) > 9
    assert walker_crackback_cost(2, 19, (3,)) == 0
    assert walker_crackback_cost(5, 19, ()) == 0


def test_the_beast_stays_home_to_keep_garruk_alive():
    # Their blockers are tapped; next turn Traxos and the Sage attack. Attacking with the
    # Beast lets 5 through to Garruk (3 loyalty); holding it blocks the Sage: 2 through.
    attack = evaluate_attack([BEAST], [], [], 12, 19, [TRAXOS, SAGE], our_walker_loyalties=(3,))
    hold = evaluate_attack([], [BEAST], [], 12, 19, [TRAXOS, SAGE], our_walker_loyalties=(3,))
    assert attack.walker_crackback_cost > 0 and hold.walker_crackback_cost == 0
    assert hold.score > attack.score
    best = optimal_attacks([BEAST], [], 12, 19, [TRAXOS, SAGE], [], our_walker_loyalties=(3,))
    assert best is not None and best.attacker_names == []
    # Without a walker to protect, the Beast attacks.
    assert optimal_attacks([BEAST], [], 12, 19, [TRAXOS, SAGE], []).attacker_names == ["Beast"]


def test_a_chump_block_that_saves_the_walker_is_generated():
    garruk = {"instance_id": 251, "name": "Garruk, Curse Breaker", "type_line": "Legendary Planeswalker — Garruk",
              "controller_seat_id": 1, "owner_seat_id": 1, "counters": {"Loyalty": 3}}  # fmt: skip
    saheeli = _creature(
        244, "Saheeli, Jewel of Avishkar", 5, 5, seat=2, attack_target_id=251, is_attacking=True
    )
    thopter = _creature(263, "Thopter", 1, 1, "flying", seat=2, attack_target_id=251, is_attacking=True)
    teyo = _creature(269, "Teyo, Lightshield Expert", 1, 1)
    cards = {c["instance_id"]: c for c in (garruk, saheeli, thopter, teyo)}
    added = walker_saving_blocks([saheeli, thopter], [teyo], {}, {269: {244}}, cards, 1)
    assert added == {269: 244}
    # Attacking us instead: no walker to save, no block added.
    at_us = {**saheeli, "attack_target_id": 2}
    assert walker_saving_blocks([at_us], [teyo], {}, {269: {244}}, {**cards, 244: at_us}, 1) == {}
