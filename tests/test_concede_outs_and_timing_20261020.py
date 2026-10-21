"""Ledger #211 and #213 in the concede engine.

#211 (match cd673437, 19:04): Brave the Elements counted as an out against a
lethal attack - protection from a colour on OUR creatures stops neither their
attackers nor the damage they deal to us, so it defends nothing and the game
was lost at -31.

#213 (match a96ed16a, 16:28): the concede fired during OUR declare-attackers
step while the plan's post-block Sneak was still pending; this combat could
still have won first.
"""

from __future__ import annotations

from tests.bug_180436_states import ISLAND, bug_state

from arenamcp.concede import estimate_loss


BRAVE = {
    "instance_id": 910,
    "name": "Brave the Elements",
    "type_line": "Instant",
    "card_types": ["Instant"],
    "oracle_text": (
        "Choose a color. White creatures you control gain protection from "
        "the chosen color until end of turn."
    ),
    "mana_cost": "{oW}",
    "owner_seat_id": 1,
    "controller_seat_id": 1,
}


def _with_white_mana(state):
    state["battlefield"].append(
        {
            "instance_id": 470,
            "name": "Plains",
            "type_line": "Basic Land - Plains",
            "oracle_text": "({T}: Add {W}.)",
            "controller_seat_id": 1,
            "owner_seat_id": 1,
        }
    )
    return state


# --- #211: protection from a colour is no defence ----------------------------------


def test_brave_the_elements_is_not_an_out_against_a_lethal_attack():
    state = _with_white_mana(bug_state(hand=[dict(ISLAND), dict(BRAVE)]))
    estimate = estimate_loss(state)
    assert estimate.confidence >= 0.95, estimate.blockers
    assert not any("Brave" in out for out in estimate.facts["outs"])


def test_a_protection_activation_is_not_an_out_either():
    banner = {
        "instance_id": 911,
        "name": "Guardian Banner",
        "type_line": "Artifact",
        "card_types": ["Artifact"],
        "oracle_text": "{oW}: Creatures you control gain protection from white until end of turn.",
        "controller_seat_id": 1,
        "owner_seat_id": 1,
    }
    state = _with_white_mana(bug_state())
    state["battlefield"].append(banner)
    estimate = estimate_loss(state)
    assert estimate.confidence >= 0.95, estimate.blockers
    assert not any("Banner" in out for out in estimate.facts["outs"])


def test_protection_alongside_a_real_effect_still_counts():
    # A card that also prevents damage remains an out; only the bare grant is dismissed.
    mixed = dict(
        BRAVE,
        instance_id=912,
        name="Bulwark Blessing",
        oracle_text=(
            "Choose a color. Creatures you control gain protection from the chosen color "
            "until end of turn. Prevent all combat damage that would be dealt to you this turn."
        ),
    )
    state = _with_white_mana(bug_state(hand=[dict(ISLAND), mixed]))
    assert estimate_loss(state).confidence < 0.95


# --- #213: hold the concede while a pending Sneak could win this combat -------------


def _bear(instance_id):
    return {
        "instance_id": instance_id,
        "name": "Bear %d" % instance_id,
        "type_line": "Creature - Beast",
        "card_types": ["Creature"],
        "power": 2,
        "toughness": 2,
        "oracle_text": "",
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "is_tapped": False,
        "turn_entered_battlefield": 1,
    }


def _dragon(instance_id, flying=False):
    card = {
        "instance_id": instance_id,
        "name": "Foe %d" % instance_id,
        "type_line": "Creature - Dragon",
        "card_types": ["Creature"],
        "power": 5,
        "toughness": 5,
        "oracle_text": "",
        "controller_seat_id": 2,
        "owner_seat_id": 2,
        "is_tapped": False,
        "turn_entered_battlefield": 1,
    }
    if flying:
        card["keywords"] = ["flying"]
    return card


BIG_BROTHER = {
    "instance_id": 920,
    "name": "Big Brother",
    "type_line": "Creature - Giant",
    "card_types": ["Creature"],
    "power": 6,
    "toughness": 6,
    # Arena packs the Sneak cost as {oW}; the per-creature bonus scales with our board.
    "oracle_text": "\n".join([
        "Sneak {oW}",
        "~ gets +1/+0 for each other creature you control.",
    ]),
    "mana_cost": "{o4oW}",
    "owner_seat_id": 1,
    "controller_seat_id": 1,
}


def _sneak_duel(opp_life=7, declared=False, step="Step_DeclareAttack"):
    """Our declare-attackers step: three bears into a flier and a blocker, at 3 life.

    Their best blocks stop one bear (4 through vs their 7); returning one bear for
    the Sneak brings Big Brother in unblocked (8 total): lethal with the Sneak only.
    Their flying 5/5 kills us on their next attack whatever we do.
    """
    battlefield = [
        _bear(801),
        _bear(802),
        _bear(803),
        _dragon(811, flying=True),
        _dragon(812),
        {
            "instance_id": 821,
            "name": "Mountain",
            "type_line": "Basic Land - Mountain",
            "oracle_text": "({T}: Add {R}.)",
            "controller_seat_id": 1,
            "owner_seat_id": 1,
            "is_tapped": False,
        },
    ]
    if declared:
        for card in battlefield[:3]:
            card["is_attacking"] = True
    us = {"seat_id": 1, "life_total": 3, "is_local": True, "status": "InGame", "mana_pool": {}}
    them = {"seat_id": 2, "life_total": opp_life, "is_local": False, "status": "InGame", "mana_pool": {}}
    turn = {
        "turn_number": 10,
        "active_player": 1,
        "priority_player": 1,
        "phase": "Phase_Combat",
        "step": step,
    }
    state = {"match_id": "sneak-duel", "local_seat_id": 1, "opponent_seat_id": 2}
    state["turn"] = turn
    state["players"] = [us, them]
    state["battlefield"] = battlefield
    state["hand"] = [dict(BIG_BROTHER)]
    state["graveyard"] = []
    state["exile"] = []
    state["stack"] = []
    state["action_history"] = []
    # The pending Sneak decision is benign; nothing else is in flight.
    # See _BENIGN_DECISIONS in arenamcp.concede.
    state["pending_decision"] = None
    state["controlled_turn"] = {"opponent_controlled_by_you": False, "you_controlled_by_opponent": False}
    state["zones"] = {
        "library_count": 12,
        "library_count_source": "log_zone_membership",
        "opponent_hand_count": 1,
    }
    return state


def test_the_declare_attack_step_holds_while_a_sneak_could_win():
    estimate = estimate_loss(_sneak_duel())
    assert estimate.confidence < 0.95, estimate.blockers
    assert "Sneak" in estimate.reason, estimate.reason


def test_a_declared_all_in_holds_while_a_sneak_could_win():
    estimate = estimate_loss(_sneak_duel(declared=True))
    assert estimate.confidence < 0.95, estimate.blockers
    assert "Sneak" in estimate.reason, estimate.reason


def test_no_hold_when_the_sneak_line_is_not_lethal():
    # Their 30 life is out of reach even with the Sneak: the concede stands.
    estimate = estimate_loss(_sneak_duel(opp_life=30))
    assert estimate.confidence >= 0.95, estimate.blockers


def test_after_our_combat_the_concede_proceeds():
    # Once our combat is over (Main2) and we are still dead, nothing holds.
    estimate = estimate_loss(_sneak_duel(step="Phase_Main2"))
    assert estimate.confidence >= 0.95, estimate.blockers
