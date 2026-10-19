"""Decision memory, reversal detection and critical-decision effort (2026-10-10 FRA game 1)."""

from __future__ import annotations

from arenamcp.decision_memory import DecisionMemory, board_signature, critical_decision
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.game_plan import GamePlan, plan_card_losses


def card(instance_id, name, zone_types=("Creature",), oracle="", owner=1):
    return {
        "instance_id": instance_id,
        "name": name,
        "card_types": list(zone_types),
        "oracle_text": oracle,
        "owner_seat_id": owner,
        "controller_seat_id": owner,
    }


PROFT = card(
    123,
    "Proft, Sinister Mastermind",
    oracle="Threshold — ... {B}, Discard this card: Target creature gets -3/-1 until end of turn.",
)
GASP = card(140, "Last Gasp", ("Instant",), "Target creature gets -3/-3 until end of turn.")


def state(turn=5, hand=(), battlefield=(), graveyard=(), stack=(), life=(20, 20), match="m1"):
    return {
        "match_id": match,
        "turn": {"turn_number": turn},
        "local_seat_id": 1,
        "players": [{"seat_id": 1, "life": life[0], "is_local": True}, {"seat_id": 2, "life": life[1]}],
        "hand": list(hand),
        "battlefield": list(battlefield),
        "graveyard": list(graveyard),
        "exile": [],
        "stack": list(stack),
    }


def actions(*options, can_pass=True):
    opts = list(options)
    if can_pass:
        opts.append(DecisionOption("pass", "Pass"))
    return PendingDecision(
        request_id=(1, 1), request_type="ActionsAvailable", options=tuple(opts), can_pass=can_pass
    )


PROFT_ACTIVATE = DecisionOption(
    "idx:2",
    "Activate Ability: Proft, Sinister Mastermind",
    payable=True,
    meta={"actionType": "ActionType_Activate", "instanceId": 123},
)
LAND = DecisionOption("idx:4", "Play Land: Island", meta={"actionType": "ActionType_Play", "instanceId": 124})


def test_signature_ignores_land_drops_but_not_creatures_or_life():
    base = state(battlefield=[card(1, "Ghoul", owner=2)], hand=[PROFT])
    after_land = state(battlefield=[card(1, "Ghoul", owner=2), card(124, "Island", ("Land",))], hand=[PROFT])
    assert board_signature(base) == board_signature(after_land)
    assert board_signature(base) != board_signature(
        state(battlefield=[card(1, "Ghoul", owner=2), card(9, "Rat", owner=2)])
    )
    assert board_signature(base) != board_signature(
        state(battlefield=[card(1, "Ghoul", owner=2)], life=(19, 20))
    )


def test_reversal_of_an_earlier_pass_on_the_same_board_is_detected():
    memory = DecisionMemory()
    board = state(battlefield=[card(1, "Master of Barbs", owner=2)], hand=[PROFT])
    first = actions(PROFT_ACTIVATE, DecisionOption("idx:0", "Cast Multiply by Zero"))
    memory.record(
        board,
        first,
        ["pass"],
        "Discarding Proft (our threshold closer) to kill a 2/1 is a bad trade with only 1 mana up",
    )
    later = actions(PROFT_ACTIVATE, LAND)
    record = memory.reversal(board, later, ["idx:2"])
    assert record is not None and record.chosen == ["pass"]
    # A land drop does not change the board for this purpose; a new creature does.
    assert (
        memory.reversal(
            state(battlefield=[card(1, "Master of Barbs", owner=2), card(2, "Rat", owner=2)]),
            later,
            ["idx:2"],
        )
        is None
    )
    assert memory.reversal(board, later, ["idx:4"]) is None
    assert memory.reversal(board, later, ["pass"]) is None


def test_prompt_block_lists_this_turns_decisions_and_resets_on_a_new_match():
    memory = DecisionMemory()
    board = state(turn=5)
    memory.record(board, actions(LAND), ["idx:4"], "Turn 5 land drop")
    memory.record(board, actions(PROFT_ACTIVATE), ["pass"], "Hold Proft")
    block = memory.prompt_block(board)
    assert "DECISIONS YOU ALREADY MADE THIS TURN" in block
    assert "Play Land: Island — Turn 5 land drop" in block and "Pass — Hold Proft" in block
    assert memory.prompt_block(state(turn=6)) == ""  # a new turn starts clean
    assert "T5 ActionsAvailable: Pass — Hold Proft" in memory.recent_actions_text()
    memory.record(state(turn=1, match="m2"), actions(LAND), ["idx:4"], "new game")
    assert "Hold Proft" not in memory.recent_actions_text()


def test_critical_decisions_are_removal_plan_card_spends_and_removal_targets():
    plan = "Win condition(s): Proft at threshold as closer"
    board = state(hand=[PROFT, GASP], battlefield=[card(1, "Ghoul", owner=2)])
    gasp = DecisionOption(
        "idx:1", "Cast Last Gasp", payable=True, meta={"actionType": "ActionType_Cast", "instanceId": 140}
    )
    assert critical_decision(actions(gasp), board, "").startswith("removal available: Last Gasp")
    assert (
        critical_decision(actions(PROFT_ACTIVATE), board, plan)
        == "spends a plan card from hand: Proft, Sinister Mastermind"
    )
    assert critical_decision(actions(PROFT_ACTIVATE), board, "").startswith(
        "removal available"
    )  # its ability is removal too
    assert critical_decision(actions(LAND), board, plan) == ""
    unpayable = DecisionOption(
        "idx:1", "Cast Last Gasp", payable=False, meta={"actionType": "ActionType_Cast", "instanceId": 140}
    )
    assert critical_decision(actions(unpayable), board, "") == ""
    targets = PendingDecision(
        request_id=(1, 2),
        request_type="SelectTargets",
        options=(DecisionOption("tgt:1", "Ghoul"),),
        source_label="Last Gasp",
    )
    assert critical_decision(targets, state(stack=[GASP]), "") == "removal target: Last Gasp"
    discard = PendingDecision(
        request_id=(1, 3),
        request_type="SelectN",
        options=(DecisionOption("sel:123", "Proft, Sinister Mastermind", meta={"instanceId": 123}),),
        source_label="Divining Duelist",
    )
    assert critical_decision(discard, board, plan).startswith("may discard a plan card: Proft")


def test_plan_card_losses_names_a_closer_that_left_hand():
    plan = GamePlan(
        win_conditions=["Evasive beats", "Proft at threshold as closer"],
        path="surveil to 7, cast Proft",
        turn_formed=4,
    )
    gone = state(graveyard=[PROFT], hand=[GASP])
    text = plan_card_losses(plan, gone)
    assert text.startswith("  PLAN CARDS NO LONGER AVAILABLE: Proft, Sinister Mastermind (in our graveyard)")
    # A second copy still in hand, or an opponent's copy in their graveyard, is not a loss.
    assert plan_card_losses(plan, state(graveyard=[PROFT], hand=[PROFT])) == ""
    assert plan_card_losses(plan, state(graveyard=[dict(PROFT, owner_seat_id=2)])) == ""
    assert plan_card_losses(plan, state(graveyard=[GASP])) == ""


def test_removal_without_a_legal_target_does_not_escalate():
    changeling = card(
        150,
        "Chomping Changeling",
        ("Creature",),
        "Changeling. When this creature enters, destroy target artifact or enchantment an opponent controls.",
    )
    cast = DecisionOption(
        "idx:1",
        "Cast Chomping Changeling",
        payable=True,
        meta={"actionType": "ActionType_Cast", "instanceId": 150},
    )
    board = state(hand=[changeling], battlefield=[card(1, "Ghoul", owner=2)])
    assert critical_decision(actions(cast), board, "") == ""
    with_artifact = state(hand=[changeling], battlefield=[card(2, "Sol Ring", ("Artifact",), owner=2)])
    assert critical_decision(actions(cast), with_artifact, "").startswith("removal available")
    # Creature removal with no opposing creature is not a critical window either.
    gasp = DecisionOption(
        "idx:2", "Cast Last Gasp", payable=True, meta={"actionType": "ActionType_Cast", "instanceId": 140}
    )
    assert critical_decision(actions(gasp), state(hand=[GASP], battlefield=[]), "") == ""


# 2026-10-19 Mono-Red Burn (07:15-09:08): burn in hand sent 151 typed decisions
# to the high-effort hedge, most of them against bodies it could not kill.
SHOCK = card(201, "Shock", ("Instant",), "Shock deals 2 damage to any target.")
BURST = card(
    202,
    "Burst Lightning",
    ("Instant",),
    "Kicker {4}\nBurst Lightning deals 2 damage to any target. If this spell was kicked, "
    "it deals 4 damage instead.",
) | {"mana_cost": "{R}"}
STRIKE = card(203, "Lightning Strike", ("Instant",), "Lightning Strike deals 3 damage to any target.")


def cast_option(option_id, source):
    return DecisionOption(
        option_id,
        f"Cast {source['name']}",
        payable=True,
        meta={"actionType": "ActionType_Cast", "instanceId": source["instance_id"]},
    )


def body(instance_id, name, toughness, owner=2, **flags):
    return dict(card(instance_id, name, owner=owner), toughness=toughness, power=toughness, **flags)


BILL = body(301, "Bristly Bill", 4)


def test_burn_that_can_kill_nothing_runs_at_low_effort():
    shock = actions(cast_option("idx:0", SHOCK))
    skipped: list[str] = []
    assert critical_decision(shock, state(hand=[SHOCK], battlefield=[BILL]), "", skipped) == ""
    assert skipped == ["Shock can kill nothing on their side"]
    # An empty opposing board, or only our own 2/2: Shock only goes face.
    assert critical_decision(shock, state(hand=[SHOCK]), "") == ""
    assert (
        critical_decision(shock, state(hand=[SHOCK], battlefield=[BILL, body(5, "Ours", 2, owner=1)]), "")
        == ""
    )
    # A body it kills, one damaged into range, one in combat, a planeswalker, or an
    # unknown toughness: removal.
    for target in (
        body(302, "Elf", 2),
        body(303, "Marshal", 4, damage=2),
        dict(BILL, is_blocking=True),
        card(304, "Jace", ("Planeswalker",), owner=2),
        card(305, "Ghoul", owner=2),
    ):
        board = state(hand=[SHOCK], battlefield=[BILL, target])
        assert critical_decision(shock, board, "") == "removal available: Shock"
    # Burst Lightning's kicked 4 damage counts only with the mana for {R} plus kicker {4};
    # on the stack it may have been kicked, so it counts there.
    burst = actions(cast_option("idx:1", BURST))
    mountains = [card(400 + i, "Mountain", ("Land",)) for i in range(5)]
    kicked = state(hand=[BURST], battlefield=[BILL, *mountains])
    assert critical_decision(burst, kicked, "") == "removal available: Burst Lightning"
    unkicked = state(hand=[BURST], battlefield=[BILL, *mountains[:2]])
    assert critical_decision(burst, unkicked, "") == ""
    tapped = state(
        hand=[BURST], battlefield=[BILL, *mountains[:3], *(dict(m, is_tapped=True) for m in mountains[3:])]
    )
    assert critical_decision(burst, tapped, "") == ""
    # Our main phase counts the land drop still to come.
    basics = [dict(m, oracle_text="({T}: Add {R}.)") for m in mountains]
    main = state(hand=[BURST, basics[4]], battlefield=[BILL, *basics[:4]])
    main["turn"].update(phase="Phase_Main1", active_player=1)
    assert critical_decision(burst, main, "") == "removal available: Burst Lightning"
    main["battlefield"] = [BILL, *basics[:3]]
    assert critical_decision(burst, main, "") == ""
    targets = PendingDecision(
        request_id=(1, 2), request_type="SelectTargets", options=(), source_label="Burst Lightning"
    )
    assert (
        critical_decision(targets, state(stack=[BURST], battlefield=[BILL]), "")
        == "removal target: Burst Lightning"
    )
    strike = actions(cast_option("idx:2", STRIKE))
    assert critical_decision(strike, state(hand=[STRIKE], battlefield=[BILL]), "") == ""
    # Real removal next to the burn still escalates.
    gasp = DecisionOption(
        "idx:3", "Cast Last Gasp", payable=True, meta={"actionType": "ActionType_Cast", "instanceId": 140}
    )
    both = actions(cast_option("idx:0", SHOCK), gasp)
    assert (
        critical_decision(both, state(hand=[SHOCK, GASP], battlefield=[BILL]), "")
        == "removal available: Last Gasp"
    )


def test_targeting_burn_that_can_kill_nothing_runs_at_low_effort():
    targets = PendingDecision(
        request_id=(1, 2),
        request_type="SelectTargets",
        options=(DecisionOption("tgt:2", "Opponent"), DecisionOption("tgt:301", "Bristly Bill")),
        source_label="Shock",
    )
    skipped: list[str] = []
    assert critical_decision(targets, state(stack=[SHOCK], battlefield=[BILL]), "", skipped) == ""
    assert skipped == ["Shock can kill nothing on their side"]
    assert critical_decision(targets, state(hand=[SHOCK], battlefield=[BILL]), "") == ""
    elf = body(302, "Elf", 2)
    assert (
        critical_decision(targets, state(stack=[SHOCK], battlefield=[BILL, elf]), "")
        == "removal target: Shock"
    )


def combat_state(step, **kw):
    board = state(**kw)
    board["turn"].update(phase="Phase_Combat", step=step, active_player=1)
    return board


def test_a_pass_that_kept_the_card_for_later_is_carried_out_not_reversed():
    """2026-10-19 08:04:11 'holding Shock as post-combat reach', then Shock once blockers were declared."""
    from arenamcp.decision_memory import turn_window

    memory = DecisionMemory()
    window = actions(cast_option("idx:0", SHOCK))
    passed_at = combat_state("Step_DeclareAttack", hand=[SHOCK], battlefield=[BILL])
    record = memory.record(
        passed_at,
        window,
        ["pass"],
        "Attacking both into a lone 4/4 blocker guarantees one connector, and holding Shock as "
        "post-combat reach closes out lethal this turn",
    )
    later = combat_state("Step_DeclareBlock", hand=[SHOCK], battlefield=[BILL])
    assert memory.reversal(later, window, ["idx:0"]) is record  # the board signature alone calls it one
    assert memory.stated_plan_followed(record, later, window, ["idx:0"]) == "as post-combat reach"
    # 08:03:58's wording of the same plan.
    held = memory.record(
        passed_at,
        window,
        ["pass"],
        "Hold Shock as instant-speed reach: attack both creatures (Bill blocks one, the other connects "
        "for 3), then Shock face for exact lethal.",
    )
    assert memory.stated_plan_followed(held, later, window, ["idx:0"]) == "as instant-speed reach"
    # The same step is not later; a pass that rejected the card names no later point.
    assert memory.stated_plan_followed(record, passed_at, window, ["idx:0"]) == ""
    rejected = memory.record(passed_at, window, ["pass"], "Shock on Bill is a bad trade; it survives")
    assert memory.stated_plan_followed(rejected, later, window, ["idx:0"]) == ""
    # A card the pass did not keep is no part of its plan; an unknown step never counts.
    strike = actions(cast_option("idx:2", STRIKE))
    assert memory.stated_plan_followed(record, later, strike, ["idx:2"]) == ""
    assert (
        memory.stated_plan_followed(record, state(hand=[SHOCK], battlefield=[BILL]), window, ["idx:0"]) == ""
    )
    # Turn order: a main phase ignores a lingering step; combat steps run in order.
    main2 = turn_window({"turn": {"phase": "Phase_Main2", "step": "Step_EndCombat"}})
    damage = turn_window({"turn": {"phase": "Phase_Combat", "step": "Step_CombatDamage"}})
    main1 = turn_window({"turn": {"phase": "Phase_Main1", "step": ""}})
    assert main2 > damage > main1
    assert turn_window({"turn": {}}) is None
