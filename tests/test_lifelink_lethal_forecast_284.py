"""BUG-284: an exactly-lethal attack forecast must count their blockers and lifelink (#284).

The declaration forecast used to read our attackers' power against their life total,
so a swing that was exactly lethal on paper was called lethal even when an untapped
lifelink blocker ate part of it and gained the life back. These tests pin the forecast
against the combat resolver on reconstructed boards: ``assess`` must not claim lethal
when their best blocks leave us short once their lifelink is counted, and its
damage-through / lifelink totals must equal what ``_resolve_attacker`` deals under
those blocks.
"""

import pytest

from arenamcp.board_assessment import assess
from arenamcp.combat_solver import _resolve_attacker, optimal_blocks

LOCAL_SEAT = 1
OPPONENT_SEAT = 2


def creature(instance_id: int, seat: int, power: int, toughness: int, text: str = "") -> dict:
    return {
        "instance_id": instance_id,
        "name": f"Creature {instance_id}",
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "grp_id": instance_id,
        "type_line": "Creature",
        "card_types": ["CardType_Creature"],
        "power": power,
        "toughness": toughness,
        "oracle_text": text,
        "is_tapped": False,
    }


def land(instance_id: int, seat: int) -> dict:
    return {
        "instance_id": instance_id,
        "name": "Forest",
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "grp_id": instance_id,
        "type_line": "Basic Land — Forest",
        "card_types": ["CardType_Land"],
        "oracle_text": "({T}: Add {G}.)",
    }


def board(opponent_life: int, ours: list[dict], theirs: list[dict]) -> dict:
    battlefield = list(ours) + [land(200 + n, LOCAL_SEAT) for n in range(3)]
    battlefield += list(theirs) + [land(400 + n, OPPONENT_SEAT) for n in range(3)]
    return {
        "match_id": "bug-284-reconstruction",
        "local_seat_id": LOCAL_SEAT,
        "opponent_seat_id": OPPONENT_SEAT,
        "turn": {
            "turn_number": 8,
            "active_player": LOCAL_SEAT,
            "priority_player": LOCAL_SEAT,
            "phase": "Phase_Combat",
            "step": "Step_DeclareAttack",
        },
        "players": [
            {"seat_id": LOCAL_SEAT, "life_total": 20, "is_local": True},
            {"seat_id": OPPONENT_SEAT, "life_total": opponent_life, "is_local": False},
        ],
        "battlefield": battlefield,
    }


def assigned_blockers(plan, attacker: dict, blockers: list[dict]) -> list[dict]:
    return [b for b in blockers if plan.assignments.get(b["instance_id"]) == attacker["instance_id"]]


def test_lifelink_blocker_turns_exact_lethal_into_a_nonlethal_attack():
    """Six trample damage against four life is only two once their lifelink gains two."""
    attacker = creature(100, LOCAL_SEAT, 6, 6, "Trample")
    lifelink_blocker = creature(300, OPPONENT_SEAT, 2, 2, "Lifelink")
    facts = assess(board(4, [attacker], [lifelink_blocker]))
    assert facts.lethal_now is False
    assert facts.posture != "lethal"

    plain_blocker = creature(300, OPPONENT_SEAT, 2, 2)
    control = assess(board(4, [attacker], [plain_blocker]))
    assert control.lethal_now is True
    assert control.posture == "lethal"


@pytest.mark.parametrize("attacker_text", ["", "Trample", "First strike", "Deathtouch"])
@pytest.mark.parametrize("blocker_text", ["", "Lifelink"])
@pytest.mark.parametrize("blocker_power_toughness", [(2, 2), (3, 5)])
def test_single_blocker_totals_match_the_resolver(
    attacker_text: str, blocker_text: str, blocker_power_toughness: tuple[int, int]
):
    """The forecast's damage through and their lifelink equal what combat actually deals."""
    attacker = creature(100, LOCAL_SEAT, 6, 6, attacker_text)
    blocker = creature(300, OPPONENT_SEAT, *blocker_power_toughness, blocker_text)
    plan = optimal_blocks([attacker], [blocker], 5)
    outcome = _resolve_attacker(attacker, assigned_blockers(plan, attacker, [blocker]))
    assert plan.damage_through == outcome.damage_through
    assert plan.blocker_life_gained == outcome.blocker_life_gained


@pytest.mark.parametrize(
    ("attacker_power_toughness", "attacker_text", "blockers", "opponent_life"),
    [
        ((6, 6), "Trample", [(2, 2, "Lifelink"), (3, 3, "")], 5),
        ((5, 5), "Deathtouch", [(4, 4, ""), (4, 4, "")], 5),
        ((7, 7), "Trample", [(2, 2, "Lifelink"), (2, 2, "Lifelink")], 6),
        ((4, 4), "First strike", [(2, 2, "Lifelink"), (1, 1, "")], 4),
    ],
)
def test_multi_blocker_damage_order_totals_match_the_resolver(
    attacker_power_toughness: tuple[int, int],
    attacker_text: str,
    blockers: list[tuple[int, int, str]],
    opponent_life: int,
):
    """Damage order across several blockers keeps the forecast equal to the resolver."""
    attacker = creature(100, LOCAL_SEAT, *attacker_power_toughness, attacker_text)
    their_blockers = [
        creature(300 + n, OPPONENT_SEAT, power, toughness, text)
        for n, (power, toughness, text) in enumerate(blockers)
    ]
    plan = optimal_blocks([attacker], their_blockers, opponent_life)
    outcome = _resolve_attacker(attacker, assigned_blockers(plan, attacker, their_blockers))
    assert plan.damage_through == outcome.damage_through
    assert plan.blocker_life_gained == outcome.blocker_life_gained


@pytest.mark.parametrize(
    ("attacker_power_toughness", "attacker_text", "blocker_power_toughness", "blocker_text", "opponent_life"),
    [
        ((6, 6), "", (2, 2), "", 4),
        ((6, 6), "", (2, 2), "Lifelink", 4),
        ((6, 6), "Trample", (2, 2), "", 4),
        ((6, 6), "Trample", (2, 2), "Lifelink", 4),
        ((4, 4), "", (2, 6), "Lifelink", 3),
        ((3, 3), "", (1, 1), "Lifelink", 3),
    ],
)
def test_lethal_now_matches_the_defender_best_net_life_loss(
    attacker_power_toughness: tuple[int, int],
    attacker_text: str,
    blocker_power_toughness: tuple[int, int],
    blocker_text: str,
    opponent_life: int,
):
    """The lethal claim is exactly the defender's best blocks losing them their life total."""
    attacker = creature(100, LOCAL_SEAT, *attacker_power_toughness, attacker_text)
    blocker = creature(300, OPPONENT_SEAT, *blocker_power_toughness, blocker_text)
    plan = optimal_blocks([attacker], [blocker], opponent_life)
    facts = assess(board(opponent_life, [attacker], [blocker]))
    assert facts.lethal_now == (plan.net_life_lost >= opponent_life)
