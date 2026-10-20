"""Regression tests for autopilot bugs #131, #150, #175, #195, #196, and #198."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from arenamcp import board_assessment as ba
from arenamcp.action_planner import (
    ActionPlan,
    ActionPlanner,
    ActionType,
    GameAction,
    SecondLookError,
)
from arenamcp.board_model import build_board_model
from arenamcp.concede import estimate_loss
from arenamcp.decision_memory import lethal_reach
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.line_search_moves import combat_counter_growth
from threat_boards_20261019 import ME, OPP, card, harbin_t11, scarab_god, state

pytestmark = pytest.mark.usefixtures("unlimited_search_deadline")


# --- #198: beginning-of-combat counters (Siege Veteran) and after-upkeep drain timing -------------


def test_siege_veteran_combat_counter_growth_and_harbin_t11_lethal_after_retriever_etb():
    board = harbin_t11()
    veteran = next(c for c in board["battlefield"] if c["instance_id"] == 455)
    assert combat_counter_growth(veteran) == ((1, "creature", False),)

    # On match 3ac9d511 T11 Main 1 after Rescue Retriever's ETB put a +1/+1 counter on Harbin
    # (6/5 -> 7/6), Siege Veteran's beginning-of-combat counter grows Harbin to 8/7 flying
    # before attackers are declared — lethal against our 8 life.
    harbin = next(c for c in board["battlefield"] if c["instance_id"] == 444)
    harbin["power"], harbin["toughness"] = 7, 6
    ba._CACHE.clear()
    facts = ba.assess(board)
    assert facts.their_next_attack == (8, 0)
    assert facts.opp_lethal_on_board
    assert "Siege Veteran grows +1/+1 a turn" in facts.clock_engines
    loss = estimate_loss(board)
    assert loss.facts["life_after"] == 0
    assert loss.facts["evasive_power"] == 8
    assert loss.facts["evasive"] == ["Harbin, Vanguard Aviator 8/7"]
    assert any("Harbin, Vanguard Aviator +1/+1" in b for b in loss.blockers)


def test_upkeep_drain_not_double_counted_after_upkeep_in_main1():
    # Match 45de8f40 T18 Main 1: The Scarab God's upkeep trigger already drained us 18 -> 13.
    # During T18 Main 1, this turn's remaining drain before combat is 0, while future turns drain 5.
    from arenamcp.concede import _their_turn_engines

    ba._CACHE.clear()
    board = scarab_god()
    model = build_board_model(board)
    god = next(b for b in model.theirs if b["instance_id"] == 531)
    assert god["_drain"] == 5
    assert god["_drain_after_upkeep"] == 0
    facts = ba.assess(board)
    assert facts.our_life_now_attack == 11  # 13 - 2 combat damage (no second upkeep drain of 5)
    drain, _growth, _notes = _their_turn_engines(
        board, ME, OPP, board["battlefield"], list(model.theirs), our_turn=False, declared=False
    )
    assert drain == 0


# --- #195: death-trigger drain in _check_block_survival and _combat_life --------------------------


def test_check_block_survival_and_combat_life_count_death_trigger_drain():
    # Match 98170e12 T10: Wriggling Grub (dies: opponent loses 2 life) and Serrated Scorpion
    # (dies: each opponent loses 2 life, you gain 2 life) attack into our 3/3 blockers at 4 life.
    # Blocking both deals 0 combat damage + 4 death drain = 4 (lethal, 4 -> 0), whereas blocking
    # only the 2/1 Grub deals 1 combat damage (Scorpion unblocked) + 2 death drain = 3 (survives at 1!).
    grub = card(
        101,
        "Wriggling Grub",
        OPP,
        power=2,
        toughness=1,
        text="When this creature dies, target opponent loses 2 life.",
        tapped=True,
        attacking=True,
    )
    scorpion = card(
        102,
        "Serrated Scorpion",
        OPP,
        power=1,
        toughness=2,
        text="When this creature dies, each opponent loses 2 life and you gain 2 life.",
        tapped=True,
        attacking=True,
    )
    b1 = card(201, "Grizzly Bears", ME, power=3, toughness=3)
    b2 = card(202, "Centaur Courser", ME, power=3, toughness=3)
    raw_blockers = [
        {"blockerInstanceId": 201, "attackerInstanceIds": [101, 102]},
        {"blockerInstanceId": 202, "attackerInstanceIds": [101, 102]},
    ]
    board = state(
        [grub, scorpion, b1, b2],
        match="98170e12",
        local=ME,
        life=4,
        opp_life=10,
        turn=10,
        active=OPP,
        phase="Phase_Combat",
        step="Step_DeclareBlock",
        their_hand=1,
        raw_blockers=raw_blockers,
    )
    context = {
        "type": "declare_blockers",
        "legal_blocker_ids": [201, 202],
        "raw_blockers": raw_blockers,
    }
    plan = ActionPlan(
        actions=[
            GameAction(
                action_type=ActionType.DECLARE_BLOCKERS,
                blocker_instance_assignments={201: 101, 202: 102},
                reasoning="Block both attackers to take 0 combat damage.",
            )
        ]
    )
    ActionPlanner(Mock())._check_block_survival(plan, board, context)
    action = plan.actions[0]
    assert action.blocker_instance_assignments != {201: 101, 202: 102}
    assert len(action.blocker_instance_assignments) == 1
    assert plan.fallback_reason == "planner_lethal_block"

    ba._CACHE.clear()
    facts = ba.assess(board)
    assert facts.our_life_now_attack == 1


# --- #175: lethal_reach gated on playable Cast/Activate and blocker-aware combat ------------------


def test_lethal_reach_requires_cast_or_activate_option_and_caps_pure_board_by_blockers():
    attacker = card(301, "Goblin Piker", ME, power=2, toughness=1)
    blocker = card(401, "Wall of Runes", OPP, power=0, toughness=4)
    shock = {
        "instance_id": 302,
        "name": "Shock",
        "owner_seat_id": ME,
        "controller_seat_id": ME,
        "card_types": ["CardType_Instant"],
        "mana_cost": "{R}",
        "oracle_text": "Shock deals 2 damage to any target.",
    }
    mountain = {
        "instance_id": 303,
        "name": "Mountain",
        "owner_seat_id": ME,
        "controller_seat_id": ME,
        "card_types": ["CardType_Land"],
        "subtypes": ["SubType_Mountain"],
        "is_tapped": False,
        "oracle_text": "({T}: Add {R}.)",
    }
    board = state(
        [attacker, blocker, mountain],
        match="175-test",
        local=ME,
        life=10,
        opp_life=2,
        turn=5,
        active=ME,
        phase="Phase_Main1",
        step="",
        their_hand=0,
        hand=[shock],
    )
    # 1. Menu with only PlayLand + Pass (no Cast or Activate): lethal_reach returns None.
    land_only_decision = PendingDecision(
        request_id=(1, 1),
        request_type="ActionsAvailable",
        options=(
            DecisionOption("idx:0", "Play Plains", payable=True, meta={"actionType": "ActionType_Play"}),
            DecisionOption("pass", "Pass", payable=True, meta={"actionType": "ActionType_Pass"}),
        ),
    )
    assert lethal_reach(land_only_decision, board) is None

    # 2. Pure board combat (no burn in hand, only casting a non-haste creature) blocked by 0/4 Wall: None.
    board_no_burn = state(
        [attacker, blocker, mountain],
        match="175-noburn",
        local=ME,
        life=10,
        opp_life=2,
        turn=5,
        active=ME,
        phase="Phase_Main1",
        step="",
        their_hand=0,
        hand=[card(304, "Grizzly Bears", ME, power=2, toughness=2)],
    )
    bear_decision = PendingDecision(
        request_id=(1, 2),
        request_type="ActionsAvailable",
        options=(
            DecisionOption(
                "idx:0",
                "Cast Grizzly Bears",
                payable=True,
                meta={"actionType": "ActionType_Cast", "instanceId": 304},
            ),
            DecisionOption("pass", "Pass", payable=True, meta={"actionType": "ActionType_Pass"}),
        ),
    )
    assert lethal_reach(bear_decision, board_no_burn) is None

    # 3. With Shock in hand and Cast option available, Shock's 2 burn reaches their 2 life.
    shock_decision = PendingDecision(
        request_id=(1, 3),
        request_type="ActionsAvailable",
        options=(
            DecisionOption(
                "idx:0",
                "Cast Shock",
                payable=True,
                meta={"actionType": "ActionType_Cast", "instanceId": 302},
            ),
            DecisionOption("pass", "Pass", payable=True, meta={"actionType": "ActionType_Pass"}),
        ),
    )
    reach = lethal_reach(shock_decision, board)
    assert reach is not None and reach.damage >= 2 and reach.burn == 2


def test_lethal_reach_gate_and_cap_in_isolation():
    """Review 2026-10-20: the land-only gate holds without a blocker, and a castable spell that can
    change the combat (here a tap effect, no face damage) keeps the blocker-free bound."""
    attacker = card(311, "Goblin Piker", ME, power=2, toughness=1)
    blocker = card(411, "Wall of Runes", OPP, power=0, toughness=4)
    mountain = {
        "instance_id": 313, "name": "Mountain", "owner_seat_id": ME, "controller_seat_id": ME,
        "card_types": ["CardType_Land"], "is_tapped": False, "oracle_text": "({T}: Add {R}.)",
    }  # fmt: skip
    land_only = PendingDecision(
        request_id=(2, 1),
        request_type="ActionsAvailable",
        options=(
            DecisionOption("idx:0", "Play Plains", payable=True, meta={"actionType": "ActionType_Play"}),
            DecisionOption("pass", "Pass", payable=True, meta={"actionType": "ActionType_Pass"}),
        ),
    )
    open_board = state(
        [attacker, mountain], match="175-gate", local=ME, life=10, opp_life=2, turn=5, active=ME,
        phase="Phase_Main1", step="", their_hand=0, hand=[],
    )  # fmt: skip
    assert lethal_reach(land_only, open_board) is None  # the gate alone: no blocker to cap anything
    tap_spell = {
        "instance_id": 314, "name": "Frost Breath", "owner_seat_id": ME, "controller_seat_id": ME,
        "card_types": ["CardType_Instant"], "type_line": "Instant", "mana_cost": "{R}",
        "oracle_text": "Tap target creature. It doesn't untap during its controller's next untap step.",
    }  # fmt: skip
    walled = state(
        [attacker, blocker, mountain], match="175-tap", local=ME, life=10, opp_life=2, turn=5, active=ME,
        phase="Phase_Main1", step="", their_hand=0, hand=[tap_spell],
    )  # fmt: skip
    tap_decision = PendingDecision(
        request_id=(2, 2),
        request_type="ActionsAvailable",
        options=(
            DecisionOption("idx:0", "Cast Frost Breath", payable=True,
                           meta={"actionType": "ActionType_Cast", "instanceId": 314}),
            DecisionOption("pass", "Pass", payable=True, meta={"actionType": "ActionType_Pass"}),
        ),
    )  # fmt: skip
    reach = lethal_reach(tap_decision, walled)
    assert reach is not None and reach.damage >= 2  # tapping the Wall lets the Piker's 2 through


# --- #196: declare_blockers with empty assignments at DeclareAttackers translates to pass ----------


def test_parse_response_translates_empty_declare_blockers_at_declare_attackers():
    planner = ActionPlanner(Mock())
    board = state(
        [card(501, "Grizzly Bears", ME, power=2, toughness=2)],
        match="196-test",
        local=ME,
        life=20,
        opp_life=20,
        turn=3,
        active=ME,
        phase="Phase_Combat",
        step="Step_DeclareAttack",
        their_hand=3,
    )
    context = {
        "type": "declare_attackers",
        "legal_attacker_ids": [501],
    }
    raw = (
        '{"actions": [{"action_type": "declare_blockers", "blocker_assignments": {}, '
        '"reasoning": "Hold back Grizzly Bears on defense."}]}'
    )
    plan = planner._parse_response(
        raw,
        ["Attack with: Grizzly Bears (id=501)", "Pass"],
        decision_context=context,
        bridge_request="DeclareAttackers",
        game_state=board,
    )
    assert len(plan.actions) == 1
    assert plan.actions[0].action_type == ActionType.DECLARE_ATTACKERS
    assert plan.actions[0].attacker_names == []
    # Review 2026-10-20: a mislabelled answer that names attackers keeps them.
    raw = (
        '{"actions": [{"action_type": "declare_blockers", "blocker_assignments": {}, '
        '"attacker_names": ["Grizzly Bears"], "reasoning": "Swing with the Bears."}]}'
    )
    plan = planner._parse_response(
        raw,
        ["Attack with: Grizzly Bears (id=501)", "Pass"],
        decision_context=context,
        bridge_request="DeclareAttackers",
        game_state=board,
    )
    (action,) = plan.actions
    assert action.action_type == ActionType.DECLARE_ATTACKERS and action.attacker_names == ["Grizzly Bears"]


# --- #131: _board_math_fallback skips attack-only creature set aside by _SecondLook ---------------


def test_board_math_fallback_skips_attack_only_cast_set_aside_when_no_attack_follows():
    # Match 6175f002 / de85f2c2: Fleeting Effigy (2/2 haste, returns to hand at end step) cast into
    # a 5/5 blocker that would kill it for free; the losing-attack guard holds the attack.
    their_wall = card(601, "Appa, Loyal Sky Bison", OPP, power=5, toughness=5)
    effigy = {
        "instance_id": 602,
        "name": "Fleeting Effigy",
        "owner_seat_id": ME,
        "controller_seat_id": ME,
        "card_types": ["CardType_Creature"],
        "type_line": "Creature — Elemental",
        "power": 2,
        "toughness": 2,
        "mana_cost": "{R}",
        "keywords": ["Haste"],
        "oracle_text": "Haste\nAt the beginning of your end step, return this creature to its owner's hand.",
    }
    board = state(
        [their_wall],
        match="de85f2c2",
        local=ME,
        life=20,
        opp_life=20,
        turn=4,
        active=ME,
        phase="Phase_Main1",
        step="",
        their_hand=3,
        hand=[effigy],
    )
    decision = PendingDecision(
        request_id=(1, 10),
        request_type="ActionsAvailable",
        options=(
            DecisionOption(
                "idx:0",
                "Cast Fleeting Effigy",
                payable=True,
                meta={"actionType": "ActionType_Cast", "instanceId": 602},
            ),
            DecisionOption("pass", "Pass", payable=True, meta={"actionType": "ActionType_Pass"}),
        ),
    )
    planner = ActionPlanner(Mock())
    planner._last_decision_trace = {}
    concern = planner._attack_only_cast_concern(
        decision, board, "idx:0", "Cast Fleeting Effigy to attack.", None, {}
    )
    assert concern != ""
    assert planner._last_decision_trace.get("attack_only", {}).get("chosen") == "idx:0"

    # When _second_look sets aside idx:0 (last_llm_failure == "set_aside"), _board_math_fallback
    # must filter out idx:0 and pick "pass" instead of re-casting Fleeting Effigy.
    planner.last_llm_failure = "set_aside"
    picked = planner._board_math_fallback(decision, board)
    assert picked == ["pass"]


# --- #150: _second_look rejects re-picked choice whose new reasoning repeats a refuted claim ------


def test_second_look_raises_when_repicked_choice_reasoning_still_makes_refuted_claim():
    # Match bd34f972 T8: Fleeting Effigy (a creature) is cast on second look with the false claim
    # that casting it "triggers Slickshot Show-Off's prowess" — decision_claim_notes refutes that
    # a creature spell triggers noncreature-spell prowess, so _second_look must raise SecondLookError.
    slickshot = card(
        701,
        "Slickshot Show-Off",
        ME,
        power=1,
        toughness=2,
        text="Flying, haste\nWhenever you cast a noncreature spell, Slickshot Show-Off gets +2/+0 until end of turn.",
    )
    effigy = {
        "instance_id": 702,
        "name": "Fleeting Effigy",
        "owner_seat_id": ME,
        "controller_seat_id": ME,
        "card_types": ["CardType_Creature"],
        "type_line": "Creature — Elemental",
        "power": 2,
        "toughness": 2,
        "mana_cost": "{R}",
        "keywords": ["Haste"],
        "oracle_text": "Haste\nAt the beginning of your end step, return this creature to its owner's hand.",
    }
    board = state(
        [slickshot],
        match="bd34f972",
        local=ME,
        life=20,
        opp_life=20,
        turn=8,
        active=ME,
        phase="Phase_Main1",
        step="",
        their_hand=2,
        hand=[effigy],
    )
    decision = PendingDecision(
        request_id=(1, 20),
        request_type="ActionsAvailable",
        options=(
            DecisionOption(
                "idx:0",
                "Cast Fleeting Effigy",
                payable=True,
                meta={"actionType": "ActionType_Cast", "instanceId": 702},
            ),
            DecisionOption("pass", "Pass", payable=True, meta={"actionType": "ActionType_Pass"}),
        ),
    )
    planner = ActionPlanner(Mock())
    planner._last_decision_trace = {}
    with pytest.raises(SecondLookError, match="second-look re-pick \\['idx:0'\\] refuted"):
        planner._second_look(
            decision,
            board,
            {"reasoning": "Cast Fleeting Effigy."},
            ["idx:0"],
            concerns_for=lambda _c, _d: [("attack_only", "Fleeting Effigy would not attack.")],
            ask=lambda *_a, **_kw: '{"reasoning": "Casting Fleeting Effigy triggers Slickshot Show-Off prowess."}',
            parse=lambda _raw: (
                {"reasoning": "Casting Fleeting Effigy triggers Slickshot Show-Off prowess."},
                ["idx:0"],
            ),
            message="Choose an option.",
            answered_high=False,
        )


# --- #134: engine-role creatures count as threats and re-plan when removal target predates -------


def test_engine_creature_counts_as_threat_and_replans_when_removal_target_predates_it():
    from arenamcp.game_plan import (
        GamePlan,
        board_basis,
        board_changes,
        plan_removal_target_predates_threat,
    )

    frodo = card(801, "Frodo, Determined Hero", OPP, power=2, toughness=2)
    biotech = card(
        802,
        "Biotech Specialist",
        OPP,
        power=1,
        toughness=3,
        text="Whenever you sacrifice an artifact, this creature deals 2 damage to target opponent.",
    )
    clue = {
        "instance_id": 803,
        "name": "Clue",
        "owner_seat_id": OPP,
        "controller_seat_id": OPP,
        "card_types": ["CardType_Artifact"],
        "subtypes": ["SubType_Clue"],
        "type_line": "Token Artifact — Clue",
        "oracle_text": "{2}, Sacrifice this artifact: Draw a card.",
    }
    before = state(
        [frodo, clue],
        match="16ed84d0",
        local=ME,
        life=18,
        opp_life=20,
        turn=6,
        active=OPP,
        phase="Phase_Main1",
        step="",
        their_hand=3,
    )
    after = state(
        [frodo, biotech, clue],
        match="16ed84d0",
        local=ME,
        life=18,
        opp_life=20,
        turn=7,
        active=ME,
        phase="Phase_Main1",
        step="",
        their_hand=2,
    )
    old_basis = board_basis(before)
    new_basis = board_basis(after)
    assert board_changes(old_basis, new_basis, threats_only=True) == [
        "Biotech Specialist 1/3 entered"
    ]
    plan = GamePlan(
        win_conditions=["Burn"],
        path="Kill Frodo, Determined Hero with Iroh's Demonstration",
        turn_formed=6,
        turn_plan=[
            {
                "turn": 7,
                "label": "T",
                "land": "",
                "cast": ["Iroh's Demonstration (on Frodo, Determined Hero)"],
                "attack": "none",
                "hold": "",
            }
        ],
        basis=old_basis,
    )
    assert plan_removal_target_predates_threat(plan, new_basis, 7) is True


# --- #141: late-game plan starvation with <=1 castable card or after 2 consecutive supersedes -----


def test_late_game_sparse_hand_and_consecutive_supersedes_use_low_effort():
    import threading
    from arenamcp.game_plan import GamePlan, GamePlanManager

    efforts: list[str] = []

    class Backend:
        def complete(self, system, user, max_tokens, **kwargs):
            efforts.append(str(kwargs.get("reasoning_effort")))
            return '{"win_conditions": ["Burn"], "path": "topdeck", "role": "aggressor", "turns": []}'

    mgr = GamePlanManager(Backend())
    mgr._plan = GamePlan(win_conditions=["Burn"], path="old", turn_formed=8, effort="high")

    # Late game (turn 10) with 1 castable spell in hand -> low effort.
    board_t10 = state(
        [card(901, "Grizzly Bears", ME, power=2, toughness=2)],
        match="d9a33232",
        local=ME,
        life=10,
        opp_life=10,
        turn=10,
        active=OPP,
        phase="Phase_Main1",
        step="",
        their_hand=1,
        hand=[card(902, "Shock", ME, text="Shock deals 2 damage to any target.")],
    )
    plan = mgr._reform(board_t10, 10)
    assert plan is not None and plan.effort == "low"
    assert efforts == ["low"]

    # After 2 supersedes in a row, the next reform runs at low effort and is not superseded again.
    cancel1 = threading.Event()
    mgr._inflight = True
    mgr._inflight_cancel = cancel1
    mgr._inflight_plan_turn = 10
    mgr._supersede_stale_inflight_locked({"turn": {"turn_number": 11}})
    assert cancel1.is_set() and mgr._consecutive_supersedes == 1

    cancel2 = threading.Event()
    mgr._inflight = True
    mgr._inflight_cancel = cancel2
    mgr._inflight_plan_turn = 11
    mgr._supersede_stale_inflight_locked({"turn": {"turn_number": 12}})
    assert cancel2.is_set() and mgr._consecutive_supersedes == 2

    cancel3 = threading.Event()
    mgr._inflight = True
    mgr._inflight_cancel = cancel3
    mgr._inflight_plan_turn = 12
    mgr._supersede_stale_inflight_locked({"turn": {"turn_number": 13}})
    assert not cancel3.is_set()


def test_one_supersede_with_a_sparse_late_hand_keeps_the_late_plan():
    """Review 2026-10-20: the keep-running and discard paths share one rule, so the late answer that
    was allowed to run on is not thrown away when it lands."""
    from arenamcp.game_plan import GamePlan, GamePlanManager

    mgr = GamePlanManager(backend=None)
    mgr._plan = GamePlan(win_conditions=["Burn"], path="old", turn_formed=8)
    sparse_t10 = state(
        [card(911, "Grizzly Bears", ME, power=2, toughness=2)], match="d9a33232", local=ME, life=10,
        opp_life=10, turn=10, active=OPP, phase="Phase_Main1", step="", their_hand=1,
        hand=[card(912, "Shock", ME, text="Shock deals 2 damage to any target.")],
    )  # fmt: skip
    assert not mgr._keep_late_plan(sparse_t10, 10)
    mgr._consecutive_supersedes = 1
    assert mgr._keep_late_plan(sparse_t10, 10)
    assert not mgr._keep_late_plan({"turn": {"turn_number": 5}, "hand": []}, 5)  # early game: one is not enough
    mgr._consecutive_supersedes = 2
    assert mgr._keep_late_plan({"turn": {"turn_number": 5}, "hand": []}, 5)


# --- #146: named_burn_damage counts a second copy of the played card when reasoning names both -----


def test_named_burn_damage_counts_second_copy_when_reasoning_names_both_bursts():
    from arenamcp.play_safety import named_burn_damage

    burst1 = {
        "instance_id": 1001,
        "name": "Burst Lightning",
        "type_line": "Instant",
        "mana_cost": "{R}",
        "oracle_text": "Kicker {4}\nBurst Lightning deals 2 damage to any target. If this spell was kicked, it deals 4 damage instead.",
    }
    burst2 = {
        "instance_id": 1002,
        "name": "Burst Lightning",
        "type_line": "Instant",
        "mana_cost": "{R}",
        "oracle_text": "Kicker {4}\nBurst Lightning deals 2 damage to any target. If this spell was kicked, it deals 4 damage instead.",
    }
    board = state(
        [card(1003, "Dazzling Angel", OPP, power=2, toughness=3)],
        match="6a8fc725",
        local=ME,
        life=20,
        opp_life=20,
        turn=4,
        active=ME,
        phase="Phase_Main1",
        step="",
        their_hand=2,
        hand=[burst1, burst2],
    )

    def mountain(iid):
        return {
            "instance_id": iid, "name": "Mountain", "owner_seat_id": ME, "controller_seat_id": ME,
            "card_types": ["CardType_Land"], "type_line": "Basic Land — Mountain", "is_tapped": False,
            "oracle_text": "({T}: Add {R}.)",
        }  # fmt: skip

    board["battlefield"] += [mountain(1101), mountain(1102)]  # {R}{R}: both Bursts are payable
    # Naming "both Bursts" / "two Bursts" / "second Burst" counts the second copy in hand (2 damage).
    assert (
        named_burn_damage("Cast Burst Lightning; kills Dazzling Angel (2/3) — needs both Bursts.", board, burst1)
        == 2
    )
    assert (
        named_burn_damage("Two Bursts kill Dazzling Angel (2/3).", board, burst1)
        == 2
    )
    # Ordinary single-copy reasoning does not count the second copy in hand.
    assert (
        named_burn_damage("Cast Burst Lightning to kill Dazzling Angel (2/3) and trigger both prowess creatures.", board, burst1)
        == 0
    )
    # Review 2026-10-20: the card's name written twice is no second copy, and neither is a copy
    # the mana can't pay for.
    assert named_burn_damage("Burst Lightning on the Angel; Burst Lightning is our best play.", board, burst1) == 0
    board["battlefield"] = [c for c in board["battlefield"] if c["instance_id"] != 1102]
    assert named_burn_damage("Needs both Bursts for Dazzling Angel (2/3).", board, burst1) == 0


def test_double_strike_is_no_second_lightning_strike():
    from arenamcp.play_safety import named_burn_damage

    strike = {
        "name": "Lightning Strike", "type_line": "Instant", "mana_cost": "{1}{R}",
        "oracle_text": "Lightning Strike deals 3 damage to any target.",
    }  # fmt: skip
    played, other = {**strike, "instance_id": 2001}, {**strike, "instance_id": 2002}
    board = state(
        [card(2003, "Ironclad Krovod", OPP, power=2, toughness=4)],
        match="review-146", local=ME, life=20, opp_life=20, turn=6, active=ME,
        phase="Phase_Main1", step="", their_hand=2, hand=[played, other],
    )  # fmt: skip
    board["battlefield"] += [
        {"instance_id": 2100 + i, "name": "Mountain", "owner_seat_id": ME, "controller_seat_id": ME,
         "card_types": ["CardType_Land"], "type_line": "Basic Land — Mountain", "is_tapped": False,
         "oracle_text": "({T}: Add {R}.)"}
        for i in range(4)
    ]  # fmt: skip
    reasoning = "Lightning Strike kills their 2/4 so our double strike attacker connects."
    assert named_burn_damage(reasoning, board, played) == 0
    assert named_burn_damage("Two Lightning Strikes kill their 2/4.", board, played) == 3


# --- #181: Hare Apparent not marked "cast after combat" when Hares attack -------------------------


def test_hare_apparent_not_marked_post_combat():
    from arenamcp.board_model import build_board_model
    from arenamcp.line_search import _Search

    hare_text = (
        "When this creature enters, create a number of 1/1 white Rabbit creature tokens equal to "
        "the number of other creatures you control named Hare Apparent.\n"
        "A deck can have any number of cards named Hare Apparent."
    )
    board = state(
        [
            card(1101, "Hare Apparent", ME, power=2, toughness=2, text=hare_text),
            card(1102, "Hare Apparent", ME, power=2, toughness=2, text=hare_text),
            {
                **card(1103, "Plains", ME, types=["CardType_Land"]),
                "type_line": "Basic Land — Plains",
                "subtypes": ["SubType_Plains"],
            },
            {
                **card(1104, "Plains", ME, types=["CardType_Land"]),
                "type_line": "Basic Land — Plains",
                "subtypes": ["SubType_Plains"],
            },
        ],
        match="5736ffca",
        local=ME,
        life=20,
        opp_life=20,
        turn=12,
        active=ME,
        phase="Phase_Main1",
        step="",
        their_hand=2,
        hand=[
            {
                "instance_id": 1105,
                "name": "Hare Apparent",
                "owner_seat_id": ME,
                "controller_seat_id": ME,
                "card_types": ["CardType_Creature"],
                "type_line": "Creature — Rabbit Noble",
                "power": 2,
                "toughness": 2,
                "mana_cost": "{1}{W}",
                "oracle_text": hare_text,
            }
        ],
    )
    model = build_board_model(board)
    search = _Search(
        model, survival=False, lethal_now=False, hard_ms=1e9, opponent=None, groups=8, width=8, k_next=4
    )
    search.free = True
    node = next(iter(dict(search.roots()).values()))
    _base, sources = search.sources(node, None)
    casts = next(c for c in search.cast_sets(node, sources, instant_only=False) if c)
    mid = search.begin(node, None, casts)
    ids = tuple(b["instance_id"] for b in node.ours)
    step = search.finish(mid, ("all", ids)).history[-1]
    assert step.casts == ("Hare Apparent",)
    assert step.post_combat == ()
    assert "after combat" not in step.text()


# --- #107: opponent attacker's "deals 4 damage to any target" trigger with no known target --------


def test_their_attacker_damage_trigger_without_target_does_not_abort_concede():
    from arenamcp.concede import estimate_loss
    from tests.bug_180436_states import bug_state

    st = bug_state()
    st["stack"] = [
        {
            "instance_id": 995,
            "name": "Overlord of the Boilerbilges ability",
            "oracle_text": "Whenever this creature enters or attacks, it deals 4 damage to any target.",
            "controller_seat_id": 2,
            "object_kind": "ABILITY",
        }
    ]
    estimate = estimate_loss(st)
    assert estimate.confidence >= 0.95, estimate.blockers


def test_their_untargeted_trigger_that_may_hit_their_own_creature_still_holds_the_concede():
    """Review 2026-10-20: a 'target creature' trigger with no creature of ours to aim at must hit
    one of theirs (maybe the attacker that kills us), so it still caps the confidence."""
    from arenamcp.concede import _stack_caps

    view = {
        "battlefield": [
            {"instance_id": 50, "name": "Their Attacker", "controller_seat_id": 2,
             "card_types": ["CardType_Creature"], "power": 6, "toughness": 6},
        ],
        "stack": [
            {"instance_id": 996, "name": "Their Attacker ability", "controller_seat_id": 2,
             "oracle_text": "Whenever this creature attacks, it deals 3 damage to target creature.",
             "object_kind": "ABILITY"},
        ],
    }  # fmt: skip
    caps = _stack_caps(view, local=1, opponent=2)
    assert caps and caps[0][0] == 0.7
    # With a creature of ours on the battlefield it can (and will) aim at ours: no cap.
    view["battlefield"].append(
        {"instance_id": 60, "name": "Our Bear", "controller_seat_id": 1, "card_types": ["CardType_Creature"],
         "power": 2, "toughness": 2}
    )
    assert _stack_caps(view, local=1, opponent=2) == []
    # "any target" can always aim at us.
    view["battlefield"].pop()
    view["stack"][0]["oracle_text"] = "Whenever this creature attacks, it deals 4 damage to any target."
    assert _stack_caps(view, local=1, opponent=2) == []


# --- #148: opponent_lost_life_this_turn on busy turns + Step_CombatDamage not finished ------------


def test_opponent_lost_life_readable_on_busy_turns_and_combat_damage_step():
    from arenamcp.concede import _opponent_lost_life_this_turn, estimate_loss
    from arenamcp.gamestate import GameState
    from tests.test_concede import board, creature

    gs = GameState()
    gs.turn_info.turn_number = 12
    gs.turn_info.phase = "Phase_Ending"
    gs._add_event({"type": "phase_change"})
    gs.turn_info.turn_number = 13
    gs.turn_info.phase = "Phase_Main1"
    for i in range(55):
        gs._add_event({"type": "layered_effect", "affected_ids": [i]})
    snap = gs._build_raw_snapshot_locked()
    # Even after >50 events on turn 13, the prior-turn boundary is kept and snapshot has all recent_events.
    assert len(snap["recent_events"]) > 10
    assert _opponent_lost_life_this_turn(snap, 2, 13) is False

    # And Step_CombatDamage is not treated as "our turn (untap and draw) comes before their next attack".
    knight = creature(
        10, "Knight", 2, 5, 5, keywords=("flying",), is_attacking=True, tapped=True, attack_target_id=1
    )
    est = estimate_loss(board([knight], phase="Phase_Combat", step="Step_CombatDamage"))
    assert est.confidence < 0.95
    assert "combat damage is being dealt" in est.reason
    assert "our turn (untap and draw) comes before their next attack" not in est.blockers


# --- #167: spoken concede reason uses chump margin figure; outs list deduplicates -----------------


def test_spoken_concede_reason_uses_chump_margin_and_deduplicates_outs():
    from arenamcp.concede import estimate_loss
    from tests.test_concede import basic, board, creature, spell

    theirs = [
        creature(10, "Sun Titan", 2, 6, 6),
        creature(11, "Golem", 2, 8, 8),
        creature(12, "Beast", 2, 8, 8),
    ]
    ours = [creature(20, "Hare Apparent", 1, 2, 2)]
    # 22 total power vs 14 life; our 2/2 chumps an 8/8 so 14 damage gets through (thin margin),
    # or at 12 life 14 damage gets through our best blocks (not 22) and concedes.
    est = estimate_loss(board(theirs + ours, life=12))
    assert est.confidence >= 0.95
    assert "14 damage gets through your best blocks" in est.reason

    # Duplicate copies of Hare Apparent in hand produce one "Hare Apparent (castable now)" entry.
    hares = [
        spell(30 + i, "Hare Apparent", "Creature — Rabbit Noble", "{1}{W}", "Create tokens.", power=2, toughness=2)
        for i in range(4)
    ]
    plains = [basic(40 + i, 1, "Plains") for i in range(3)]
    est_outs = estimate_loss(
        board(theirs + ours + plains, life=4, active=1, phase="Phase_Main1", hand=hares)
    )
    assert est_outs.facts["outs"] == ["Hare Apparent (castable now)"]


# --- #105: damage_dealt uses affectorId as source and coach formats target_id ---------------------


def test_damage_dealt_uses_affector_id_and_formats_target():
    from arenamcp.coach import CoachEngine
    from arenamcp.gamestate import GameObject, GameState

    gs = GameState()
    gs.local_seat_id = 1
    gs.game_objects[101] = GameObject(instance_id=101, grp_id=1001, zone_id=28, owner_seat_id=1, controller_seat_id=1)
    gs.game_objects[202] = GameObject(instance_id=202, grp_id=2002, zone_id=28, owner_seat_id=2, controller_seat_id=2)
    gs._resolve_card_name = lambda grp_id: {1001: "Burst Lightning", 2002: "Pyrewood Gearhulk"}.get(grp_id, "?")

    gs._process_annotations(
        [
            {
                "id": 1,
                "affectorId": 101,
                "affectedIds": [202],
                "type": ["AnnotationType_DamageDealt"],
                "details": [{"key": "damage", "valueInt32": [4]}],
            }
        ]
    )
    evt = gs.recent_events[-1]
    assert evt["type"] == "damage_dealt"
    assert evt["source_id"] == 101
    assert evt["source"] == "Burst Lightning"
    assert evt["target_id"] == 202
    assert evt["target"] == "Pyrewood Gearhulk"

    engine = CoachEngine.__new__(CoachEngine)
    lines = engine._format_zones_and_events(
        {
            "recent_events": [
                {"type": "damage_dealt", "source": "Burst Lightning", "amount": 4, "target_id": 202}
            ],
            "battlefield": [{"instance_id": 202, "name": "Pyrewood Gearhulk"}],
        },
        local_seat=1,
        opp_seat=2,
    )
    assert any("Burst Lightning dealt 4 to Pyrewood Gearhulk" in line for line in lines)


# --- #106: a mid-turn reload snapshot is not the turn's start ------------------------------------
# (Gemini's countered-cast marking was reverted in review 2026-10-20: it guessed a counter from the
# opponent's next cast, so a spell whose Spell Pierce tax we paid was marked countered.)


def test_mid_turn_reload_is_not_the_turn_start_in_plan_ledger():
    from arenamcp.game_plan import GamePlanManager

    mgr = GamePlanManager(backend=None)

    def _state(turn: int, active: int, phase: str, opp_life: int):
        return {
            "match_id": "5d5d32f8",
            "turn": {"turn_number": turn, "active_player": active, "phase": phase},
            "local_seat_id": 2,
            "players": [
                {"seat_id": 1, "life": opp_life},
                {"seat_id": 2, "life": 20, "is_local": True},
            ],
            "battlefield": [
                {
                    "instance_id": 295,
                    "name": "Giant Cindermaw",
                    "controller_seat_id": 2,
                    "card_types": ["CardType_Creature"],
                    "power": 4,
                    "toughness": 3,
                    "is_tapped": True,
                }
            ],
            "hand": [],
            "graveyard": [],
            "stack": [],
            "action_history": [],
        }

    # Mid-turn reload on T7 (Main2 after Cindermaw already dealt 4 damage): start must not be taken as 11->11.
    mgr.observe(_state(7, active=2, phase="Phase_Main2", opp_life=11))
    mgr.observe(_state(8, active=1, phase="Phase_Main1", opp_life=11))
    mgr.observe(_state(9, active=2, phase="Phase_Main1", opp_life=11))
    mgr.observe(_state(10, active=1, phase="Phase_Main1", opp_life=7))
    mgr.observe(_state(11, active=2, phase="Phase_Main1", opp_life=7))
    block = mgr.ledger_payload()["block"]
    lessons = mgr.ledger_payload()["lessons"]
    assert "them 11->11" not in block.splitlines()[1]
    assert not any("T7" in lesson for lesson in lessons)


# --- #112: Unholy Annex's locked Ritual Chamber flagged as an incoming 6/6 flying Demon threat ----


def test_locked_room_door_token_flagged_as_incoming_threat():
    from arenamcp.board_assessment import _threats

    annex = {
        "instance_id": 310,
        "name": "Unholy Annex // Ritual Chamber",
        "controller_seat_id": 2,
        "card_types": ["CardType_Enchantment"],
        "type_line": "Enchantment — Room // Enchantment — Room",
        "oracle_text": (
            "At the beginning of your end step, draw a card. If you control a Demon, each opponent loses 2 life "
            "and you gain 2 life. Otherwise, you lose 2 life.\n"
            "---\n"
            "When you unlock this door, create a 6/6 black Demon creature token with flying."
        ),
    }
    threats = _threats([annex], opponent=2, theirs=[], our_air=False, our_count=0)
    assert len(threats) == 1
    assert threats[0].name == "Unholy Annex // Ritual Chamber"
    assert "locked Ritual Chamber: creates 6/6 flying Demon token when unlocked" in threats[0].why
    assert threats[0].score > 10.0


# --- #135: keep the plan/staleness block per decision in the packet trace ------------------------


def test_decision_trace_stores_plan_and_staleness_block():
    backend = Mock()
    backend.complete.return_value = '{"selected_ids": ["pass"], "reasoning": "Hold mana."}'
    planner = ActionPlanner(backend=backend)
    planner.set_game_plan_source(
        lambda state, with_lines=True: "GAME PLAN (formed turn 6):\n  PLAN PREDATES: Biotech Specialist entered."
    )
    decision = PendingDecision((1, 1), "ActionsAvailable", (DecisionOption("pass", "Pass"),))
    state = {
        "match_id": "16ed84d0",
        "turn": {"turn_number": 7, "active_player": 1, "phase": "Phase_Main1"},
        "local_seat_id": 1,
        "players": [{"seat_id": 1, "life": 20, "is_local": True}, {"seat_id": 2, "life": 20}],
        "battlefield": [],
        "hand": [],
        "graveyard": [],
        "stack": [],
    }
    chosen = planner.plan_decision_options(decision, state)
    assert chosen == ["pass"]
    trace = planner.get_last_decision_trace()
    assert "PLAN PREDATES: Biotech Specialist entered." in trace.get("plan_block", "")


# --- #165: attack holds record reasoning in the match packet --------------------------------------


def test_attack_hold_records_combat_decision_in_packet(monkeypatch):
    import threading

    from arenamcp.autopilot import AutopilotConfig, AutopilotEngine
    from arenamcp.match_packets import MatchPacket

    packet = MatchPacket("b7337658")
    monkeypatch.setattr("arenamcp.match_packets.get_current_packet", lambda: packet)
    monkeypatch.setattr("arenamcp.autopilot_bridge._progress_bridge_for", lambda self, gs: self._gre_bridge)

    engine = AutopilotEngine.__new__(AutopilotEngine)
    engine._config = AutopilotConfig(dry_run=False)
    engine._abort_event = threading.Event()
    engine._gre_bridge_failed_methods = set()
    engine._log_execution_path = lambda *args, **kwargs: None
    engine._current_plan = ActionPlan(
        actions=[], overall_strategy="Hold back: all attackers get eaten by their 4/4 blocker."
    )
    engine._get_game_state = lambda: {
        "match_id": "b7337658",
        "turn": {"turn_number": 7, "phase": "Phase_Combat", "step": "Step_DeclareAttack", "active_player": 1},
        "legal_actions": ["Attack with: Hare Apparent"],
        "_bridge_request_class": "DeclareAttackersRequest",
    }
    bridge = Mock()
    bridge.connect.return_value = True
    bridge.get_pending_actions.return_value = {"has_pending": True, "request_class": "DeclareAttackersRequest"}
    bridge.submit_attackers.return_value = True
    engine._gre_bridge = bridge

    action = GameAction(
        action_type=ActionType.CLICK_BUTTON,
        card_name="done",
        reasoning="Keep Hare Apparent back as a blocker.",
        model_choice=True,
    )
    res = engine._try_gre_bridge(action, engine._get_game_state())
    assert res is not None and res.success
    assert len(packet.decisions) == 1
    rec = packet.decisions[0]
    assert rec["pending_decision"]["request_type"] == "DeclareAttackers"
    assert rec["chosen_options"] == ["No attacks"]
    assert rec["explanation"] == "Keep Hare Apparent back as a blocker."


# --- #163: Brawl deck analysis waits for the command-zone commander --------------------------------


def test_brawl_deck_analysis_waits_for_command_zone_commander(monkeypatch):
    from types import SimpleNamespace

    from arenamcp.coach import CoachEngine
    from arenamcp.standalone_deck import _DeckAnalysisMixin

    workers = []
    monkeypatch.setattr(
        "arenamcp.standalone_deck.threading.Thread",
        lambda **kw: SimpleNamespace(start=lambda: workers.append(kw["target"])),
    )
    runtime = _DeckAnalysisMixin()
    runtime._coach = CoachEngine(Mock())
    runtime._auto_deck_strategy = True

    pre_gsm = {
        "match_id": "brawl-match-1",
        "deck_cards": [1002] * 99,
        "commander_grp_ids": [],
        "format_profile": {"family": "brawl", "is_commander_format": True},
        "players": [],
    }
    assert not runtime._maybe_analyze_deck(pre_gsm)
    assert workers == []

    with_commander = {
        **pre_gsm,
        "commander_grp_ids": [1001],
    }
    assert runtime._maybe_analyze_deck(with_commander)
    assert len(workers) == 1

    # Review 2026-10-20: once the game is under way (the command zone was filled before turn 1) a
    # 99-card deck with no commander seen is analysed anyway rather than waited on forever.
    mid_game = {**pre_gsm, "match_id": "brawl-match-2", "turn": {"turn_number": 4}}
    assert runtime._maybe_analyze_deck(mid_game)
    assert len(workers) == 2


def test_brawl_free_mulligan_keeps_seven_in_verdict_and_prompt():
    """#166: Brawl's freeMulliganCount=1 means the first mulligan leads to a 7-card hand."""
    from arenamcp import mulligan_policy
    from arenamcp.gamestate import GameState
    from arenamcp.gamestate_decisions import _handle_decision_message

    gs = GameState()
    gs.local_seat_id = 1
    _handle_decision_message(
        gs,
        "GREMessageType_MulliganReq",
        {
            "type": "GREMessageType_MulliganReq",
            "mulliganReq": {
                "mulliganType": "MulliganType_London",
                "mulliganCount": 0,
                "freeMulliganCount": 1,
            },
        },
    )
    assert gs.decision_context["mulligan_count"] == 0
    assert gs.decision_context["free_mulligan_count"] == 1

    hand = [
        {
            "instance_id": 10,
            "grp_id": 1,
            "name": "Mountain",
            "type_line": "Basic Land — Mountain",
            "mana_cost": "",
            "oracle_text": "({T}: Add {R}.)",
        }
    ] + [
        {
            "instance_id": 11 + i,
            "grp_id": 2 + i,
            "name": f"Dragon {i}",
            "type_line": "Creature — Dragon",
            "mana_cost": "{4}{R}",
            "oracle_text": "Flying",
        }
        for i in range(6)
    ]
    state = {
        "local_seat_id": 1,
        "decision_context": dict(gs.decision_context),
        "turn": {"turn_number": 0, "active_player": 1},
        "hand": hand,
    }
    found = mulligan_policy.situation(state)
    assert found.free_mulligans == 1
    assert found.keep_size == 7
    verdict = mulligan_policy.mulligan_verdict(found)
    assert verdict == ("mulligan", "1 land(s) in 7 cards; a 7-card hand is better")
    lines = "\n".join(mulligan_policy.describe(state))
    assert "mulligans taken this game: 0 (1 free)" in lines
    assert "MULLIGAN → you will see a new 7, keep 7 (free mulligan, bottom 0)." in lines


def test_vanilla_token_from_mtgadb_does_not_get_phantom_keyword_by_name():
    """#173: Ranger Class's vanilla 2/2 Wolf token (grp 77380) must not inherit Deathtouch by name."""
    from types import SimpleNamespace

    from arenamcp.card_db import CardInfo, FallbackCardDatabase, MTGADatabaseAdapter

    class _FakeMtgaDb:
        available = True

        def get_card(self, arena_id: int):
            if arena_id == 77380:
                return SimpleNamespace(
                    grp_id=77380,
                    name="Wolf",
                    oracle_text="",
                    type_line="Token Creature — Wolf",
                    mana_cost="",
                    colors="5",
                    rarity="token",
                    power="2",
                    toughness="2",
                    linked_face_ids=[],
                    is_token=True,
                )
            return None

    class _FakeScryfallByName:
        def get_card_by_arena_id(self, arena_id: int):
            return None

        def get_card_by_name(self, name: str):
            if name == "Wolf":
                return CardInfo(
                    name="Wolf",
                    oracle_text="Deathtouch",
                    type_line="Token Creature — Wolf",
                    mana_cost="",
                    source="scryfall",
                    power="1",
                    toughness="1",
                )
            return None

    db = FallbackCardDatabase(sources=[MTGADatabaseAdapter(_FakeMtgaDb()), _FakeScryfallByName()])
    wolf = db.get_card_by_arena_id(77380)
    assert wolf is not None
    assert wolf.name == "Wolf"
    assert wolf.is_token is True
    assert wolf.oracle_text == ""


def test_auto_queue_treats_blocked_after_queue_started_as_bounded_matchmaking_wait():
    """#85: Once _queue_started/_queue_confirmed is set, screen=blocked/action=stop waits for the match."""
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock

    from tests.mac_fakes import make_frame

    from arenamcp.auto_queue import AutoQueueNavigator

    state = {"match_id": "ended"}
    controller = Mock()
    controller.capture.side_effect = make_frame
    controller.execute.return_value = True
    backend = SimpleNamespace(
        complete_with_image=Mock(
            return_value=json.dumps(
                {
                    "screen": "blocked",
                    "action": "stop",
                    "label": "Waiting for Opponent",
                    "point": None,
                    "confidence": 0.96,
                    "reason": "Matchmaking transition screen",
                }
            )
        )
    )
    statuses = []
    nav = AutoQueueNavigator(
        backend=backend,
        controller=controller,
        get_game_state=lambda: state,
        status_fn=statuses.append,
        refine_target=lambda frame, data, action: action,
    )
    nav.set_enabled(True)
    assert nav.note_match_end(
        {"match_id": "ended", "event_id": "Ladder", "deck_name": "Current deck"},
        confirmed_match_end=True,
    )
    nav._queue_started = True
    nav._queue_confirmed = True

    nav._step(nav._generation, nav._abort)
    assert nav._queue_started is True
    assert nav.paused_reason == ""
    assert statuses[-1] == "Waiting for the next match"



