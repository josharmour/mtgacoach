"""74ffd931 T14: the guard held a legal five-point lifelink attack at four life.

Arena GSID342 / request472 records only Spider412 as a legal attacker;
newly cast Vendor435 remains available to block. Opposing creatures are all
tapped. This replay covers the visible combat, not unknown cards or a win.
"""

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.board_assessment import _CACHE
from arenamcp.combat_solver import evaluate_attack


def _creature(iid, name, seat, power, toughness, keywords=(), *, tapped=False):
    return {
        "instance_id": iid,
        "grp_id": iid,
        "name": name,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "card_types": ["Creature"],
        "type_line": "Creature",
        "power": power,
        "toughness": toughness,
        "keywords": list(keywords),
        "oracle_text": " ".join(keywords),
        "is_tapped": tapped,
        "summoning_sickness": False,
        "turn_entered_battlefield": 1,
    }


def recorded_board():
    spider = _creature(412, "Skyward Spider", 2, 5, 3, ("flying", "lifelink", "ward"))
    vendor = _creature(435, "Spellbook Vendor", 2, 2, 2, ("vigilance",))
    vendor.update(summoning_sickness=True, turn_entered_battlefield=14)
    gisa = _creature(404, "Gisa, the Hellraiser", 1, 4, 4, tapped=True)
    zombies = [_creature(iid, "Zombie Rogue", 1, 3, 3, ("menace",), tapped=True) for iid in (432, 433)]
    state = {
        "match_id": "74ffd931-crackback-replay",
        "local_seat_id": 2,
        "opponent_seat_id": 1,
        "turn": {
            "turn_number": 14,
            "active_player": 2,
            "priority_player": 2,
            "phase": "Combat",
            "step": "DeclareAttack",
        },
        "players": [
            {"seat_id": 2, "life_total": 4, "is_local": True},
            {"seat_id": 1, "life_total": 20, "is_local": False},
        ],
        "battlefield": [spider, vendor, gisa, *zombies],
        "hand": [],
        "graveyard": [],
        "exile": [],
        "stack": [],
        "command": [],
        "zones": {"opponent_hand_count": 0, "opponent_library_count": 20, "library_count": 20},
        "legal_actions": ["Attack with: Skyward Spider (5/3)"],
        "decision_context": {"type": "declare_attackers", "legal_attacker_ids": [412]},
    }
    return state


def _plan():
    return ActionPlan(
        actions=[
            GameAction(
                ActionType.DECLARE_ATTACKERS,
                attacker_names=["Skyward Spider"],
                attacker_instance_ids=[412],
                attacker_targets={"Skyward Spider": "Opponent"},
                reasoning="Attack with the flying lifelink Spider while their creatures are tapped.",
            )
        ],
        overall_strategy="Race with lifelink, keeping the new Vendor available to block.",
    )


def test_crackback_block_search_uses_life_gained_before_their_attack():
    state = recorded_board()
    spider, vendor, *theirs = state["battlefield"]
    result = evaluate_attack([spider], [vendor], [], 20, 4, theirs, state=state)
    assert result.life_gained == result.damage_through == 5
    # At 9 life, Vendor can chump Gisa; the two menace Zombies deal six.
    # Searching blocks at the pre-lifelink four incorrectly deemed all blocks
    # futile and returned ten, then the declaration guard forgot the gain too.
    assert result.worst_case_crackback == 6
    assert 4 + result.life_gained - result.worst_case_crackback == 3


def test_declaration_guard_keeps_the_recorded_surviving_lifelink_attack():
    _CACHE.clear()
    state, plan = recorded_board(), _plan()
    ActionPlanner(backend=None)._check_attack_crackback(plan, state, state["decision_context"])
    assert plan.actions[0].attacker_instance_ids == [412]
    assert plan.fallback_reason != "planner_crackback_guard"


def test_same_attack_without_lifelink_does_not_invent_survival():
    state = recorded_board()
    state["battlefield"][0]["keywords"].remove("lifelink")
    state["battlefield"][0]["oracle_text"] = "flying ward"
    spider, vendor, *theirs = state["battlefield"]
    result = evaluate_attack([spider], [vendor], [], 20, 4, theirs, state=state)
    assert result.damage_through == 5
    assert result.life_gained == 0
    assert 4 + result.life_gained - result.worst_case_crackback <= 0
