"""#247: the attack search must see poison and mill wins, not only life damage.

``combat_solver.evaluate_attack`` already scores a tenth poison counter and an
emptied library as a win (tests/test_combat_loss_281.py). The declaration search
in ``combat_strategy._search`` scored life damage only, so a toxic attacker at
nine poison or a miller with three cards left was valued at one damage and the
line search could hold it. These controls pin the lethal/nonlethal boundary on
both sides of the race, plus the abstentions: an unknown library count and a
planeswalker recipient must not be scored as a player loss.
"""

from arenamcp.combat_strategy import combat_choice


def creature(iid, power=1, toughness=1, text="", seat=1, **kw):
    return {
        "instance_id": iid,
        "name": f"Creature {iid}",
        "power": power,
        "toughness": toughness,
        "oracle_text": text,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "type_line": "Creature",
        "card_types": ["Creature"],
        "turn_entered_battlefield": 1,
        **kw,
    }


def state(
    ours, theirs, *, our_life=30, opp_life=20, our_poison=0, opp_poison=0, library=None, opp_library=None
):
    return {
        "match_id": "test-247",
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "turn": {
            "turn_number": 8,
            "active_player": 1,
            "phase": "Phase_Combat",
            "step": "Step_DeclareAttackers",
        },
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": our_life, "counters": {"Poison": our_poison}},
            {"seat_id": 2, "life_total": opp_life, "counters": {"Poison": opp_poison}},
        ],
        "battlefield": [*ours, *theirs],
        "hand": [],
        "graveyard": [],
        "stack": [],
        "zones": {
            "library_count": library,
            "library_count_source": "log_zone_membership",
            "opponent_library_count": opp_library,
            "opponent_library_count_source": "bridge_total_card_count",
        },
        "decision_context": {
            "raw_attackers": [
                {
                    "attackerInstanceId": card["instance_id"],
                    "legalDamageRecipients": [{"playerSystemSeatId": 2}],
                }
                for card in ours
            ]
        },
    }


MILL_THREE = "Whenever this creature deals combat damage to a player, that player mills three cards."


def test_tenth_poison_counter_wins_and_ninth_does_not():
    toxic = creature(1, text="Toxic 1")
    # Tapped: it cannot block this combat, so the attack is unblocked.
    wall = creature(2, power=30, toughness=30, seat=2, is_tapped=True)
    lethal = combat_choice(state([toxic], [wall], opp_poison=9))
    assert lethal.assignments == {1: {"playerSystemSeatId": 2}}
    assert lethal.score == 10000 and lethal.poison_given == 1
    assert "1 poison" in lethal.explanation
    # Nonlethal control: the same attack at eight poison is one damage, not a win.
    safe = combat_choice(state([toxic], [wall], opp_poison=8))
    assert safe.assignments == {} and safe.score < 1000


def test_mill_emptying_the_library_wins_and_one_card_left_does_not():
    miller = creature(3, text=MILL_THREE)
    wall = creature(2, power=30, toughness=30, seat=2, is_tapped=True)
    lethal = combat_choice(state([miller], [wall], opp_library=3))
    assert lethal.assignments == {3: {"playerSystemSeatId": 2}}
    assert lethal.score == 10000 and lethal.cards_milled == 3
    assert "3 milled" in lethal.explanation
    # Nonlethal control: four cards left survives the three-card mill.
    safe = combat_choice(state([miller], [wall], opp_library=4))
    assert safe.assignments == {} and safe.score < 1000


def test_crackback_poison_death_is_not_a_free_attack():
    ours = creature(1, text="Toxic 1")
    their_toxic = creature(2, text="Toxic 1", seat=2, is_tapped=True)
    # Attacking leaves no blocker: their toxic hit is our tenth counter.
    assert combat_choice(state([ours], [their_toxic], our_poison=9)).assignments == {}
    # Nonlethal control: at eight poison the same attack is still worth taking.
    assert combat_choice(state([ours], [their_toxic], our_poison=8)).assignments


def test_crackback_mill_death_is_not_a_free_attack():
    ours = creature(1)
    their_miller = creature(4, text=MILL_THREE, seat=2, is_tapped=True)
    assert combat_choice(state([ours], [their_miller], library=2)).assignments == {}
    # Nonlethal control: five cards survive the three-card mill.
    assert combat_choice(state([ours], [their_miller], library=5)).assignments


def test_library_count_change_invalidates_the_cached_answer():
    miller = creature(3, text=MILL_THREE)
    wall = creature(2, power=30, toughness=30, seat=2, is_tapped=True)
    # Same board twice: only the library count differs. The memo key must see it.
    assert combat_choice(state([miller], [wall], opp_library=3)).score == 10000
    assert combat_choice(state([miller], [wall], opp_library=4)).score < 1000


def test_unknown_library_count_abstains_from_a_mill_win():
    miller = creature(3, text=MILL_THREE)
    wall = creature(2, power=30, toughness=30, seat=2, is_tapped=True)
    choice = combat_choice(state([miller], [wall]))
    assert choice.score < 1000


def test_damage_to_a_planeswalker_is_not_a_player_hit():
    toxic = creature(1, text="Toxic 1")
    walker = {
        "instance_id": 9,
        "name": "Walker",
        "type_line": "Planeswalker",
        "card_types": ["Planeswalker"],
        "controller_seat_id": 2,
        "owner_seat_id": 2,
        "counters": {"Loyalty": 6},
        "turn_entered_battlefield": 1,
    }
    board = state([toxic], [walker], opp_poison=9)
    # Only the walker is a legal recipient: no player hit, so no tenth counter.
    board["decision_context"]["raw_attackers"][0]["legalDamageRecipients"] = [
        {"planeswalkerInstanceId": 9},
    ]
    choice = combat_choice(board)
    assert choice.assignments == {1: {"planeswalkerInstanceId": 9}}
    assert choice.poison_given == 0 and choice.score < 1000
