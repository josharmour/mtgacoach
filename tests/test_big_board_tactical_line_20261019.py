"""The attack search on big boards outside the declaration (ledger #162/#182, 2026-10-19).

~/.arenamcp/standalone.log 15:50:04-15:50:24 (Brawl, Leonardo, Big Brother vs a white Soldier
swarm, our T21 main 1): "attack search hit its 2.0s deadline with 13 attackers; best so far:
attack with nobody; 0 through, crackback 25" five times in 20 s (13, 13, 14, 14, 17 attackers),
each one 2 s on the coaching thread. The first came between "Captured 12 legal actions"
(15:50:02.78) and "Bridge detected decision" (15:50:04.99): the decision waited behind it. The
caller was ``standalone._emit_pipe_snapshots`` -> ``MCTSEvaluator.evaluate`` (the sidebar's
tactical line, re-run at every state change of our main 1), whose optimal_attacks call had the
declaration's full 2 s; it also counted the Hare and Rabbits cast at 15:50:12 as attackers (it
read "has_summoning_sickness", which snapshots do not carry), hence 14 and 17. "Nobody" won by
the no-attack tie-break: holding (crackback 25 against our 25) and the all-in (0 through) were
both -1000.

The board is Player.log gsid 534 (15:50:02.77): we are at 25 with three Hare Apparents, a Human
and nine Rabbits, all able; they are at 5 with 31 untapped creatures (23 Soldier artifact
tokens, three Warriors, Mondrak, Myrel, Herald of Anafenza, Survivor of Korlis, Descendant of
Storms). Card text is the MTGA card database's.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from tests.test_big_board_combat_20261019 import board_t10

import arenamcp.combat_solver as solver
import arenamcp.mcts_evaluator as mcts

LOCAL, OPPONENT = 2, 1
TURN = 21
TEXT = {
    "Hare Apparent": "When this creature enters, create a number of 1/1 white Rabbit creature tokens equal to "
    "the number of other creatures you control named Hare Apparent.\nA deck can have any number of cards named "
    "Hare Apparent.",
    "Myrel, Shield of Argive": "During your turn, your opponents can't cast spells or activate abilities of "
    "artifacts, creatures, or enchantments.\nWhenever Myrel attacks, create X 1/1 colorless Soldier artifact "
    "creature tokens, where X is the number of Soldiers you control.",
    "Descendant of Storms": "Whenever this creature attacks, you may pay {o1oW}. If you do, it endures 1.",
    "Survivor of Korlis": "First strike\n{o1oW}, Exile this card from your graveyard: Scry 2.",
    "Herald of Anafenza": "Outlast {o2oW}\nWhenever you activate this creature's outlast ability, create a 1/1 "
    "white Warrior creature token.",
    "Mondrak, Glory Dominus": "If one or more tokens would be created under your control, twice that many of "
    "those tokens are created instead.\n{o1o(W/P)o(W/P)}, Sacrifice two other artifacts and/or creatures: Put an "
    "indestructible counter on Mondrak.",
}
TYPES = {
    "Hare Apparent": "Creature — Rabbit Noble", "Human": "Token Creature — Human",
    "Rabbit": "Token Creature — Rabbit", "Soldier": "Token Artifact Creature — Soldier",
    "Warrior": "Token Creature — Warrior", "Myrel, Shield of Argive": "Legendary Creature — Human Soldier",
    "Descendant of Storms": "Creature — Human Soldier", "Survivor of Korlis": "Creature — Human Soldier",
    "Herald of Anafenza": "Creature — Human Soldier", "Mondrak, Glory Dominus": "Legendary Creature — Phyrexian Horror",
}  # fmt: skip


def _card(iid: int, name: str, seat: int, power: int, toughness: int, entered: int = 18, **extra) -> dict:
    card: dict[str, Any] = {
        "instance_id": iid, "name": name, "owner_seat_id": seat, "controller_seat_id": seat,
        "type_line": TYPES[name], "oracle_text": TEXT.get(name, ""), "power": power, "toughness": toughness,
        "card_types": ["CardType_Creature"], "is_tapped": False, "turn_entered_battlefield": entered,
    }  # fmt: skip
    if "Token" in TYPES[name]:
        card["object_kind"] = "TOKEN"
    if name == "Soldier":
        card["card_types"] = ["CardType_Artifact", "CardType_Creature"]
    card.update(extra)
    return card


def board_1550() -> tuple[list[dict], list[dict]]:
    """(ours, theirs) at gsid 534."""
    ours = [_card(i, "Hare Apparent", LOCAL, 2, 2) for i in (660, 610, 526)]
    ours += [_card(466, "Human", LOCAL, 1, 1)]
    ours += [_card(i, "Rabbit", LOCAL, 1, 1) for i in (504, 505, 530, 531, 573, 614, 615, 664, 665)]
    soldiers = (561, 562, 563, 564, 565, *range(631, 649))
    theirs = [_card(i, "Soldier", OPPONENT, 1, 1) for i in soldiers]
    theirs += [_card(i, "Warrior", OPPONENT, 1, 1) for i in (586, 657, 658)]
    theirs += [
        _card(589, "Descendant of Storms", OPPONENT, 2, 1),
        _card(514, "Herald of Anafenza", OPPONENT, 3, 4),
        _card(509, "Mondrak, Glory Dominus", OPPONENT, 4, 4),
        _card(668, "Myrel, Shield of Argive", OPPONENT, 3, 4, entered=TURN),
        _card(587, "Survivor of Korlis", OPPONENT, 1, 1),
    ]
    return ours, theirs


def state_1550(*, extra_ours: list[dict] = ()) -> dict[str, Any]:
    ours, theirs = board_1550()
    return {
        "local_seat_id": LOCAL,
        "turn": {"turn_number": TURN, "active_player": LOCAL, "priority_player": LOCAL, "phase": "Phase_Main1",
                 "step": ""},
        "players": [
            {"seat_id": LOCAL, "life_total": 25, "is_local": True, "lands_played": 0},
            {"seat_id": OPPONENT, "life_total": 5, "is_local": False},
        ],
        "battlefield": ours + list(extra_ours) + theirs,
        "hand": [], "graveyard": [], "stack": [], "command": [],
    }  # fmt: skip


def test_a_display_caller_gets_the_short_deadline_on_a_big_board():
    assert solver.display_deadline(12) == 2.0
    assert solver.display_deadline(13) == solver.QUICK_DEADLINE_S < 1.0


def test_the_real_board_answers_within_the_short_deadline_and_not_with_nobody(caplog):
    solver._MEMO.clear()
    ours, theirs = board_1550()
    started = time.monotonic()
    with caplog.at_level(logging.DEBUG, logger="arenamcp.combat_solver"):
        plan = solver.optimal_attacks(
            ours, theirs, 5, 25, theirs, [], deadline_s=solver.display_deadline(len(ours)), caller="test"
        )
    elapsed = time.monotonic() - started
    # Before: 2.0 s and more (the crackback reserve alone ran 0.55-0.7 s past any deadline).
    assert elapsed < 1.2, elapsed
    # Holding is -1000 (their crackback 25 meets our 25), so is the all-in: the seed, not the
    # tie-break's "nobody", is what a cut search returns; a deeper search may find better.
    assert plan.attacker_ids
    assert "attack search (test) hit its 0.25s deadline" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]  # the caller's own cap


def test_the_deadline_returns_the_all_in_seed_over_a_tied_nobody(monkeypatch):
    """Match 2b77f14d T10: "nobody" is the verdict (ties go to no attack;
    test_big_board_combat_20261019). After the all-in seed and "nobody" (both -1000, 0 through)
    every attack is provably doomed (#188, ``combat_solver._every_attack_doomed``): the search
    stops there with that verdict, deadline or not. A search the deadline cuts where "nobody"
    leads the scored seeds only by the no-attack tie-break returns the best seed."""
    ours, blockers, crackers = board_t10()
    solver._MEMO.clear()
    full = solver.optimal_attacks(ours, blockers, 19, 6, crackers, [])
    assert full.attacker_ids == [] and full.score == -1000.0
    scored: list[int] = []
    real = solver.evaluate_attack
    monkeypatch.setattr(solver, "evaluate_attack", lambda *a, **k: scored.append(1) or real(*a, **k))
    monkeypatch.setattr(solver.time, "monotonic", lambda: 0.0 if len(scored) < 2 else 100.0)
    solver._MEMO.clear()
    cut = solver.optimal_attacks(ours, blockers, 19, 6, crackers, [])
    assert len(scored) == 2  # the all-in seed, then nobody
    assert cut.attacker_ids == [] and cut.score == -1000.0
    nobody = solver.AttackPlan(attacker_ids=[], score=-1000.0)
    seed = solver.AttackPlan(attacker_ids=[c["instance_id"] for c in ours], score=-1000.0)
    everyone = (1 << len(ours)) - 1
    assert solver._deadline_pick(nobody, 0, [(seed, everyone)]) == (seed, everyone)


def test_a_cut_search_keeps_nobody_when_it_is_strictly_better():
    plan = solver.AttackPlan(attacker_ids=[], score=-5.0)
    seed = solver.AttackPlan(attacker_ids=[1, 2], score=-1000.0)
    assert solver._deadline_pick(plan, 0, [(seed, 3)]) == (plan, 0)
    assert solver._deadline_pick(plan, 0, []) == (plan, 0)


def test_the_reserve_stops_at_the_deadline(monkeypatch):
    ours, _blockers, crackers = board_t10()
    calls: list[int] = []
    import arenamcp.combat_strategy as strategy

    real = strategy.guarded_block_plan
    monkeypatch.setattr(strategy, "guarded_block_plan", lambda *a, **k: calls.append(1) or real(*a, **k))
    assert solver._crackback_reserve(ours, [], crackers, 6, deadline=time.monotonic() - 1.0) is None
    assert calls == []


def test_the_tactical_line_skips_sick_creatures_and_uses_the_short_deadline(monkeypatch):
    """15:50:24: the Hare cast at 15:50:12 and its three Rabbits made it "17 attackers"."""
    fresh = [_card(678, "Hare Apparent", LOCAL, 2, 2, entered=TURN, summoning_sickness=True)]
    fresh += [_card(i, "Rabbit", LOCAL, 1, 1, entered=TURN) for i in (679, 680, 681)]
    seen: list[dict] = []
    real = mcts.optimal_attacks

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(mcts, "optimal_attacks", spy)
    mcts.MCTSEvaluator.reset_cache()
    solver._MEMO.clear()
    started = time.monotonic()
    payload = mcts.MCTSEvaluator.evaluate(state_1550(extra_ours=fresh), force=True)
    elapsed = time.monotonic() - started
    assert len(seen) == 1
    ready = {c["instance_id"] for c in seen[0]["candidate_attackers"]}
    assert ready == {c["instance_id"] for c in board_1550()[0]}  # 13: none of the four fresh ones
    assert seen[0]["deadline_s"] == solver.QUICK_DEADLINE_S and seen[0]["caller"] == "tactical line"
    assert elapsed < 2.0, elapsed  # was 2 s for the attack search alone
    assert any(b.action.startswith("Declare Attacks") for b in payload.branches)
