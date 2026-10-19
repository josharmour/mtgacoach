"""The attack search stops once every attack is doomed (ledger #188, 2026-10-19).

Match 9a6ea9be T14 (Brawl, Leonardo, Big Brother vs a Sultai graveyard deck; Player.log gsid 423,
16:03:44; ~/.arenamcp/standalone.log 16:03:47-16:04:04): we are at 7 with three Hare
Apparents and five Rabbits, all able; they are at 27 with sixteen creatures, thirteen of them
untapped (Cavalier of Night 5/6 lifelink, Icetill Explorer 3/5, Paradox Shaper 2/4, Rhino
Warrior 4/4, Badgermole Cub, four Zombie Druids, three Insects and an animated Forest; Titan of
Industry 8/8, Teval 5/5 flier and Skull Prophet tapped). "attack search hit its 2.0s deadline
with 8 attackers; best so far: attack with nobody; 0 through, crackback 44" four times on our
T14 (coach.py's computed attack and the sidebar's tactical line), 2 s each: holding and the
all-in were scored in under 20 ms, then each one-creature attack re-ran their sixteen-creature
crackback against our blockers (0.28 s each) until the deadline. Exactly eight attackers were
also never seeded (``n > 8``).

Card text is the MTGA card database's.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import arenamcp.combat_solver as solver

LOCAL, OPPONENT = 1, 2
TEXT = {
    "Hare Apparent": "When this creature enters, create a number of 1/1 white Rabbit creature tokens equal to "
    "the number of other creatures you control named Hare Apparent.\nA deck can have any number of cards named "
    "Hare Apparent.",
    "Cavalier of Night": "Lifelink\nWhen this creature enters, you may sacrifice another creature. When you do, "
    "destroy target creature an opponent controls.\nWhen this creature dies, return target creature card with "
    "mana value 3 or less from your graveyard to the battlefield.",
    "Icetill Explorer": "You may play an additional land on each of your turns.\nYou may play lands from your "
    "graveyard.\n<i>Landfall</i> — Whenever a land you control enters, mill a card.",
    "Paradox Shaper": "At the beginning of your upkeep, if this creature isn't prepared, it becomes prepared.\n"
    "{o2}: Put target card from your graveyard on the bottom of your library.",
    "Titan of Industry": "Reach\nTrample\nWhen this creature enters, choose two — ...",
    "Teval, the Balanced Scale": "Flying\nWhenever Teval attacks, mill three cards. Then you may return a land "
    "card from your graveyard to the battlefield tapped.",
    "Skull Prophet": "{oT}: Add {oB} or {oG}.\n{oT}: Mill two cards.",
    "Badgermole Cub": "When this creature enters, earthbend 1.\nWhenever you tap a creature for mana, add an "
    "additional {oG}.",
}
KEYWORDS = {
    "Cavalier of Night": ["lifelink"], "Titan of Industry": ["reach", "trample"],
    "Teval, the Balanced Scale": ["flying"],
}  # fmt: skip


def _card(iid: int, name: str, seat: int, power: int, toughness: int, **extra: Any) -> dict:
    card = {
        "instance_id": iid, "name": name, "owner_seat_id": seat, "controller_seat_id": seat,
        "power": power, "toughness": toughness, "oracle_text": TEXT.get(name, ""),
        "keywords": list(KEYWORDS.get(name, [])), "type_line": "Creature", "card_types": ["CardType_Creature"],
        "is_tapped": False, "turn_entered_battlefield": 10,
    }  # fmt: skip
    card.update(extra)
    return card


def board_t14() -> tuple[list[dict], list[dict], list[dict]]:
    """(our attackers, their untapped blockers, their creatures) at gsid 423."""
    ours = [_card(i, "Hare Apparent", LOCAL, 2, 2) for i in (542, 575, 582)]
    ours += [_card(i, "Rabbit", LOCAL, 1, 1, object_kind="TOKEN") for i in (581, 586, 587, 588, 589)]
    blockers = [
        _card(656, "Paradox Shaper", OPPONENT, 2, 4, turn_entered_battlefield=13),
        _card(610, "Cavalier of Night", OPPONENT, 5, 6, turn_entered_battlefield=13),
        _card(592, "Icetill Explorer", OPPONENT, 3, 5, turn_entered_battlefield=13),
        _card(451, "Badgermole Cub", OPPONENT, 2, 2),
        _card(450, "Forest", OPPONENT, 1, 1, card_types=["CardType_Creature", "CardType_Land"]),
        _card(572, "Rhino Warrior", OPPONENT, 4, 4, object_kind="TOKEN"),
        *[_card(i, "Insect", OPPONENT, 1, 1, object_kind="TOKEN") for i in (532, 551, 627)],
        *[_card(i, "Zombie Druid", OPPONENT, 2, 2, object_kind="TOKEN") for i in (608, 618, 639, 655)],
    ]
    tapped = [
        _card(560, "Titan of Industry", OPPONENT, 8, 8, is_tapped=True),
        _card(554, "Teval, the Balanced Scale", OPPONENT, 5, 5, is_tapped=True),
        _card(463, "Skull Prophet", OPPONENT, 3, 1, is_tapped=True),
    ]
    return ours, blockers, blockers + tapped


def _search(caplog, **kwargs) -> tuple[solver.AttackPlan, float]:
    ours, blockers, crackers = board_t14()
    solver._MEMO.clear()
    started = time.monotonic()
    with caplog.at_level(logging.DEBUG, logger="arenamcp.combat_solver"):
        plan = solver.optimal_attacks(ours, blockers, 27, 7, crackers, [], caller="test", **kwargs)
    return plan, time.monotonic() - started


def test_every_attack_is_doomed_so_the_search_stops_with_the_full_verdict(caplog, monkeypatch):
    scored: list[int] = []
    real = solver.evaluate_attack
    monkeypatch.setattr(solver, "evaluate_attack", lambda *a, **k: scored.append(len(a[0])) or real(*a, **k))
    plan, elapsed = _search(caplog)
    # Before: 2.17 s and ten subsets (nobody, all eight, then one-creature attacks at 0.28 s each).
    assert elapsed < 1.0, elapsed
    assert "deadline" not in caplog.text
    assert sorted(scored) == [0, 8]  # holding and the all-in, nothing else
    # Their thirteen blockers stop all eight: nothing gets through, so nobody; their crackback
    # (44) kills us at 7 whatever we keep home.
    assert plan.attacker_ids == [] and plan.score == -1000.0 and plan.worst_case_crackback >= 7


def test_the_bound_needs_every_attack_doomed():
    ours, blockers, crackers = board_t14()
    hold = solver.evaluate_attack([], ours, blockers, 27, 7, crackers)
    all_in = solver.evaluate_attack(ours, [], blockers, 27, 7, crackers)
    assert hold.score == all_in.score == -1000.0
    assert solver._every_attack_doomed(hold, all_in, ours, blockers, crackers, 7)
    # Their killable blockers (toughness 2 or less: the Cub, the Forest, the Insects, the Zombie
    # Druids) carry 14 power: at 44 that still kills us at 30 life, not at 31.
    assert hold.worst_case_crackback - 14 >= 30
    assert not solver._every_attack_doomed(
        hold, all_in, ours, blockers, crackers, hold.worst_case_crackback - 13
    )
    # A deathtouch attacker could kill any blocker (28 power of theirs): doomed up to 16 life only.
    touch = [dict(ours[0], keywords=["deathtouch"]), *ours[1:]]
    assert solver._every_attack_doomed(hold, all_in, touch, blockers, crackers, 16)
    assert not solver._every_attack_doomed(hold, all_in, touch, blockers, crackers, 17)
    assert solver._every_attack_doomed(hold, all_in, ours, blockers, crackers, 17)
    # Lifelink buys its power in life: a lifelink Hare's 2 against 44 - 14 = 30.
    drain = [dict(ours[0], keywords=["lifelink"]), *ours[1:]]
    assert solver._every_attack_doomed(hold, all_in, drain, blockers, crackers, 28)
    assert not solver._every_attack_doomed(hold, all_in, drain, blockers, crackers, 29)
    # Not doomed holding: the search goes on.
    safe = solver.AttackPlan(score=-5.0)
    assert not solver._every_attack_doomed(safe, all_in, ours, blockers, crackers, 7)


def test_eight_attackers_are_seeded():
    ours, _blockers, _crackers = board_t14()
    assert solver.SEED_ATTACKERS == 8 <= len(ours)
    order = list(solver._attack_order(ours, [tuple(range(len(ours)))]))
    assert order[0] == tuple(range(8)) and order[1] == ()
