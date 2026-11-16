"""The attack and defense prompt must only describe possible one-creature blocks."""

from arenamcp.coach import CoachEngine


def creature(iid, name, power, toughness, seat, **extra):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Creature",
        "power": power,
        "toughness": toughness,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        **extra,
    }


def test_menace_does_not_get_single_block_examples_on_either_side():
    coach = CoachEngine.__new__(CoachEngine)
    attacker = creature(10, "Raider", 2, 2, 1, keywords=["menace"], is_attacking=True)
    wall = creature(20, "Wall", 4, 4, 2)
    scout = creature(21, "Scout", 1, 1, 2)
    p1, p2 = {"seat_id": 1, "life_total": 20}, {"seat_id": 2, "life_total": 20}
    for blockers in ([wall], [wall, scout]):
        attack = coach._format_attack_combat([attacker], blockers, p1, p2, 5, [attacker])
        defense = coach._format_block_combat(blockers, [attacker], p2, 5, "Phase_Combat", {10})
        for lines in (attack, defense):
            assert any("Raider has menace" in line for line in lines)
            assert not any("blocks Raider 2/2:" in line for line in lines)
    assert coach._compute_optimal_blocking_damage([attacker], [wall]) == 2
    assert coach._compute_optimal_blocking_damage([attacker], [wall, scout]) == 0


def test_block_examples_and_damage_use_the_native_blocker_targets():
    coach = CoachEngine.__new__(CoachEngine)
    a = creature(10, "Large", 5, 5, 1, is_attacking=True)
    b = creature(11, "Small", 1, 1, 1, is_attacking=True)
    wall = creature(
        20, "Wall", 2, 6, 2, oracle_text="This creature can block only creatures with power 2 or less."
    )
    ctx = {
        "type": "declare_blockers",
        "raw_blockers": [{"blockerInstanceId": 20, "attackerInstanceIds": [11]}],
    }
    lines = coach._format_block_combat(
        [wall], [a, b], {"seat_id": 2, "life_total": 20}, 5, "Phase_Combat", {10, 11}, ctx
    )
    text = "\n".join(lines)
    assert "Wall 2/6 blocks Small 1/1:" in text
    assert "Wall 2/6 blocks Large 5/5:" not in text
    assert "take 5 dmg" in text


def test_a_current_option_or_selection_proves_an_attacker_is_present():
    from arenamcp.combat_strategy import phantom_attackers

    large = creature(10, "Large", 5, 5, 1)
    small = creature(11, "Small", 1, 1, 1)
    wall = creature(20, "Wall", 2, 6, 2)
    scout = creature(21, "Scout", 1, 1, 2)
    state = {"battlefield": [large, small, wall, scout]}
    only_small = {"blockerInstanceId": 20, "attackerInstanceIds": [11]}
    for field in ("attackerInstanceIds", "selectedAttackerInstanceIds"):
        other = {"blockerInstanceId": 21, field: [10]}
        assert phantom_attackers(state, [10, 11], [only_small, other]) == set()
        assert phantom_attackers(state, [10, 11], [other, only_small]) == set()
    assert phantom_attackers(state, [10, 11], [only_small]) == {10}


def test_missing_attack_flag_and_unoffered_blocker_do_not_understate_damage():
    coach = CoachEngine.__new__(CoachEngine)
    raider = creature(10, "Raider", 5, 5, 1, is_attacking=True, keywords=["menace"])
    scout = creature(11, "Scout", 3, 3, 1)
    wall = creature(20, "Wall", 2, 6, 2)
    cat = creature(21, "Cat", 1, 1, 2)
    for target_field in ("attackerInstanceIds", "selectedAttackerInstanceIds"):
        ctx = {
            "type": "declare_blockers",
            "raw_blockers": [{"blockerInstanceId": 20, target_field: [10, 11]}],
        }
        text = "\n".join(
            coach._format_block_combat(
                [wall, cat], [raider, scout], {"seat_id": 2, "life_total": 20}, 5, "Phase_Combat", set(), ctx
            )
        )
        assert "No blocks → take 8 dmg" in text and "Best blocks → take 5 dmg" in text
        assert "Cat 1/1 blocks" not in text
    empty = {"type": "declare_blockers", "raw_blockers": [], "legal_blocker_ids": []}
    text = "\n".join(
        coach._format_block_combat(
            [wall, cat], [raider], {"seat_id": 2, "life_total": 20}, 5, "Phase_Combat", set(), empty
        )
    )
    assert "No blocks → take 5 dmg" in text
    assert "take 0 dmg" not in text
