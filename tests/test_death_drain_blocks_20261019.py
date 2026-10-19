"""Ledger #184: the block solver charges the opponent's death drain (match 4d934402 T11).

2026-10-19 16:07:42, Brawl: Vein Ripper ("Whenever a creature dies, target opponent
loses 2 life and you gain 2 life.") had just entered on their side; Lagomos's
Elemental and Urabrask's Forge's Phyrexian Horror (2/1 trample haste, sacrificed at
the next end step) attacked into our two untapped Hare Apparents and five Rabbits at
22 life (them at 4). The solver said "Rabbit blocks Elemental, Rabbit blocks
Phyrexian Horror (2 dmg through; kills Elemental, Phyrexian Horror; loses Rabbit,
Rabbit)"; the four deaths drained us 8 and gained them 8: 22-4 became 12-12.
"""

from __future__ import annotations

from arenamcp.combat_solver import evaluate_attack, optimal_blocks
from arenamcp.combat_strategy import free_blocks
from arenamcp.death_triggers import annotate_death_swings, card_death_triggers, death_trigger_notes

US, THEM = 2, 1
VEIN_RIPPER = "Flying\nWard—Sacrifice a creature.\nWhenever a creature dies, target opponent loses 2 life and you gain 2 life."
HARE = (
    "When this creature enters, create a number of 1/1 white Rabbit creature tokens equal to the number of "
    "other creatures you control named Hare Apparent.\nA deck can have any number of cards named Hare Apparent."
)
LAGOMOS = (
    "At the beginning of combat on your turn, create a 2/1 red Elemental creature token with trample and "
    "haste. Sacrifice it at the beginning of the next end step.\n{oT}: Search your library for a card, put it "
    "into your hand, then shuffle. Activate only if five or more creatures died this turn."
)
ZULAPORT = "Whenever this creature or another creature you control dies, each opponent loses 1 life and you gain 1 life."


def creature(iid, name, power, toughness, seat, text="", *, token=False, **extra):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Token Creature" if token else "Creature",
        "card_types": ["CardType_Creature"],
        "object_kind": "TOKEN" if token else "CARD",
        "is_token": token,
        "power": power,
        "toughness": toughness,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "oracle_text": text,
        "is_tapped": False,
        **extra,
    }


def t11_board(*, creator_marks: bool = True) -> list[dict]:
    """The T11 declare-blockers board (Player.log gsid 298); ``creator_marks``: the tokens carry the
    snapshot's ``token_expires`` (#185) — without it only the drain decides."""
    expires = {"token_expires": "sacrifice"} if creator_marks else {}
    return [
        creature(534, "Vein Ripper", 6, 5, THEM, VEIN_RIPPER, keywords=["flying"]),
        creature(451, "Lagomos, Hand of Hatred", 1, 3, THEM, LAGOMOS),
        creature(542, "Elemental", 2, 1, THEM, "Trample\nHaste", token=True, is_attacking=True, **expires),
        creature(
            544, "Phyrexian Horror", 2, 1, THEM, "Trample\nHaste", token=True, is_attacking=True, **expires
        ),
        creature(519, "Hare Apparent", 2, 2, US, HARE),
        creature(525, "Hare Apparent", 2, 2, US, HARE),
        *[creature(i, "Rabbit", 1, 1, US, token=True) for i in (523, 524, 529, 530, 531)],
    ]


def solve(board, life=22):
    attackers = [c for c in board if c.get("is_attacking")]
    blockers = [c for c in board if c["controller_seat_id"] == US]
    return optimal_blocks(attackers, blockers, life)


def test_vein_ripper_is_read_as_a_two_point_drain_on_every_death():
    [trigger] = card_death_triggers(creature(534, "Vein Ripper", 6, 5, THEM, VEIN_RIPPER))
    assert (trigger.loss, trigger.gain, trigger.self_loss) == (2, 2, 0)
    assert trigger.includes_self and not trigger.own_only and trigger.whose == ""


def test_every_creature_carries_its_death_swing():
    board = t11_board(creator_marks=False)
    annotate_death_swings(board)
    swings = {card["instance_id"]: card.get("death_swing") for card in board}
    # Ours: we lose 2, they gain 2; theirs: they gain 2, we lose 2.
    assert swings[523] == [-2, 2] and swings[519] == [-2, 2]
    assert swings[542] == [2, -2] and swings[534] == [2, -2]


def test_a_token_sacrificed_at_end_step_dies_anyway_and_carries_no_swing():
    board = t11_board()
    annotate_death_swings(board)
    swings = {card["instance_id"]: card.get("death_swing") for card in board}
    assert swings[542] is None and swings[544] is None
    assert swings[523] == [-2, 2]


def test_the_recorded_rabbit_trades_without_the_drain():
    """The pre-fix answer: without death triggers on the board the solver trades (solve.py)."""
    board = [card for card in t11_board(creator_marks=False) if card["name"] != "Vein Ripper"]
    plan = solve(board)
    rabbits = {523, 524, 529, 530, 531}
    assert sorted(plan.assignments.values()) == [542, 544]
    assert set(plan.assignments) <= rabbits
    assert "loses Rabbit, Rabbit" in plan.explanation


def test_the_t11_blocks_change_once_the_drain_is_charged():
    board = t11_board(creator_marks=False)
    annotate_death_swings(board)
    plan = solve(board)
    assert plan.assignments == {}, plan.explanation
    assert plan.explanation == "no blocks (4 dmg through)"


def test_a_trade_into_the_drain_is_priced_and_explained():
    board = t11_board(creator_marks=False)
    annotate_death_swings(board)
    attackers = [c for c in board if c.get("is_attacking")]
    rabbits = [c for c in board if c["instance_id"] in (523, 524)]
    plan = optimal_blocks(attackers, rabbits, 22, blocker_allowed_attackers={523: {542}, 524: {544}})
    # Forced into the recorded blocks it would still decline them; the explanation of a trade says why.
    from arenamcp.combat_solver import _rebuild_block_plan

    traded = _rebuild_block_plan(attackers, rabbits, (0, 1), 22)
    assert (traded.death_drain, traded.attacker_death_life) == (8, 8)
    assert "death triggers drain you 8 and gain them 8" in traded.explanation
    assert traded.score < plan.score


def test_the_drain_can_be_lethal_by_itself():
    """At 2 life a chump on a 1/1 stops 1 damage, but the chump's death drains 2: dead."""
    board = [
        creature(534, "Vein Ripper", 6, 5, THEM, VEIN_RIPPER),
        creature(600, "Goblin", 1, 1, THEM, is_attacking=True),
        creature(601, "Wall", 0, 1, US),
    ]
    assert solve(board, life=2).assignments == {601: 600}  # blind to the drain: chump
    annotate_death_swings(board)
    plan = solve(board, life=2)
    assert plan.assignments == {}
    assert plan.damage_through == 1 and plan.death_drain == 0


def test_our_own_death_payoff_is_symmetric():
    zulaport = creature(700, "Zulaport Cutthroat", 1, 2, US, ZULAPORT)
    rabbit = creature(701, "Rabbit", 1, 1, US, token=True)
    goblin = creature(702, "Goblin", 1, 1, THEM)
    board = [zulaport, rabbit, goblin]
    annotate_death_swings(board)
    assert rabbit["death_swing"] == [1, -1]  # we gain 1, they lose 1
    assert zulaport["death_swing"] == [1, -1]
    assert "death_swing" not in goblin


def test_the_attack_search_charges_their_drain_on_our_trades():
    """Our 2/2 attacking into their 2/2 with Vein Ripper home: both die, we lose 4, they gain 4."""
    bear = creature(800, "Bear", 2, 2, US)
    their_bear = creature(801, "Bear", 2, 2, THEM)
    ripper = creature(534, "Vein Ripper", 6, 5, THEM, VEIN_RIPPER)
    annotate_death_swings([bear, their_bear, ripper])
    plan = evaluate_attack([bear], [], [their_bear], 20, 20, [])
    assert plan.opponent_death_loss == -4
    assert plan.life_gained == -4
    assert "death triggers: them +4, us -4 life" in plan.explanation


def test_no_free_block_when_the_kill_drains_more_than_it_saves():
    """A 3/3 kills an attacking 1/1 and lives, but the 1/1's death drains us 2 to save 1."""
    board = {
        "turn": {"turn_number": 9, "active_player": THEM, "phase": "Phase_Combat"},
        "players": [
            {"seat_id": THEM, "life_total": 20, "is_local": False, "mana_pool": {}},
            {"seat_id": US, "life_total": 20, "is_local": True, "mana_pool": {}},
        ],
        "battlefield": [
            creature(534, "Vein Ripper", 6, 5, THEM, VEIN_RIPPER),
            creature(900, "Goblin", 1, 1, THEM, is_attacking=True),
            creature(901, "Ogre", 3, 3, US),
        ],
        "hand": [],
        "graveyard": [],
        "decision_context": {
            "type": "declare_blockers",
            "legal_blocker_ids": [901],
            "raw_blockers": [{"blockerInstanceId": 901, "attackerInstanceIds": [900], "maxAttackers": 1}],
            "attacker_ids": [900],
        },
    }
    assert free_blocks(board) == {}
    board["battlefield"][0]["oracle_text"] = "Flying"
    board["battlefield"][1].pop("death_swing", None)
    assert set(free_blocks(board)) == {901}


def test_the_block_prompt_names_the_drain():
    from arenamcp.coach import CoachEngine

    board = t11_board()
    state = {
        "players": [
            {"seat_id": THEM, "life_total": 4, "is_local": False},
            {"seat_id": US, "life_total": 22, "is_local": True},
        ],
        "battlefield": board,
        "decision_context": {
            "type": "declare_blockers",
            "legal_blocker_ids": [519, 525, 523, 524, 529, 530, 531],
            "attacker_ids": [542, 544],
        },
    }
    annotate_death_swings(board)
    ours = [card for card in board if card["controller_seat_id"] == US]
    theirs = [card for card in board if card["controller_seat_id"] == THEM]
    local = state["players"][1]
    lines = CoachEngine.__new__(CoachEngine)._format_block_combat(
        ours, theirs, local, 11, "Combat", set(), state["decision_context"], state
    )
    prompt = "\n".join(lines)
    assert "Death trigger: Each creature death drains you 2 and gains them 2 (Vein Ripper, theirs)" in prompt
    assert "Computed optimal blocks: no blocks (4 dmg through)" in prompt
    assert death_trigger_notes(board, US) == [
        "Each creature death drains you 2 and gains them 2 (Vein Ripper, theirs)"
    ]
