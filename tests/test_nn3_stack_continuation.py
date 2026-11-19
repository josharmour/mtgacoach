"""NN3 tests: stack-aware GamePlan progress, hold enforcement, own-stack continuation & TurnPlan unification."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from unittest.mock import MagicMock, patch

from arenamcp.action_planner import ActionPlanner, TurnPlan, TurnPlanStep
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine
from arenamcp.board_assessment import assess
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.game_plan import (
    _DONE,
    _ON_STACK,
    GamePlan,
    GamePlanManager,
    _with_progress,
    compose_strategy_block,
)
from arenamcp.play_safety import card_held_by_plan, own_stack_reserved_option

_RESCUE_ORACLE = (
    "Flash\n"
    "Enchant creature you control\n"
    "When this Aura enters, enchanted creature gains hexproof until end of turn.\n"
    "Enchanted creature gets +1/+1."
)
_SEAM_RIP_ORACLE = (
    "When this enchantment enters, exile target nonland permanent an opponent controls "
    "with mana value 2 or less until this enchantment leaves the battlefield."
)
_CUT_DOWN_ORACLE = "Destroy target creature with total power and toughness 5 or less."
_OPT_ORACLE = "Scry 1, then draw a card."

_T20_HOLD = (
    "Hold Shardmage's Rescue x2 and Seam Rip; keep mana flexible for flash protection or a drawn answer"
)


class _ScriptedBackend:
    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, *args: Any, **kwargs: Any) -> str:
        self.calls.append((system, user))
        return self.responses.pop(0) if self.responses else '{"option_ids": ["pass"], "reasoning": "pass"}'


def _t20_state(
    *, stack: list[dict] | None = None, phase: str = "Phase_Main1", step: str = ""
) -> dict[str, Any]:
    plains = [
        {
            "instance_id": 100 + i,
            "grp_id": 90001,
            "name": "Plains",
            "type_line": "Basic Land — Plains",
            "oracle_text": "({T}: Add {W}.)",
            "controller_seat_id": 1,
            "owner_seat_id": 1,
            "is_tapped": i < 3,
            "turn_entered_battlefield": 10 + i,
        }
        for i in range(5)
    ]
    scavenger = {
        "instance_id": 201,
        "grp_id": 90002,
        "name": "Optimistic Scavenger",
        "type_line": "Creature — Human Scout",
        "mana_cost": "{W}",
        "oracle_text": "Eerie — Whenever an enchantment you control enters, put a +1/+1 counter on target creature.",
        "power": 1,
        "toughness": 1,
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "turn_entered_battlefield": 20,
    }
    opp_flier_a = {
        "instance_id": 301,
        "grp_id": 90003,
        "name": "Aclazotz",
        "type_line": "Legendary Creature — Bat God",
        "oracle_text": "Flying, lifelink",
        "power": 4,
        "toughness": 4,
        "controller_seat_id": 2,
        "owner_seat_id": 2,
        "turn_entered_battlefield": 14,
    }
    opp_flier_b = {
        "instance_id": 302,
        "grp_id": 90004,
        "name": "Emeritus",
        "type_line": "Creature — Bird Wizard",
        "oracle_text": "Flying",
        "power": 4,
        "toughness": 4,
        "controller_seat_id": 2,
        "owner_seat_id": 2,
        "turn_entered_battlefield": 16,
    }
    spider_on_stack = {
        "instance_id": 401,
        "grp_id": 90005,
        "name": "Skyward Spider",
        "type_line": "Creature — Spider Human Hero",
        "mana_cost": "{1}{W}",
        "oracle_text": "Reach",
        "power": 2,
        "toughness": 3,
        "controller_seat_id": 1,
        "owner_seat_id": 1,
    }
    rescue_1 = {
        "instance_id": 501,
        "grp_id": 90006,
        "name": "Shardmage's Rescue",
        "type_line": "Enchantment — Aura",
        "mana_cost": "{W}",
        "oracle_text": _RESCUE_ORACLE,
        "controller_seat_id": 1,
        "owner_seat_id": 1,
    }
    rescue_2 = {
        "instance_id": 502,
        "grp_id": 90006,
        "name": "Shardmage's Rescue",
        "type_line": "Enchantment — Aura",
        "mana_cost": "{W}",
        "oracle_text": _RESCUE_ORACLE,
        "controller_seat_id": 1,
        "owner_seat_id": 1,
    }
    seam_rip = {
        "instance_id": 503,
        "grp_id": 90007,
        "name": "Seam Rip",
        "type_line": "Enchantment",
        "mana_cost": "{W}",
        "oracle_text": _SEAM_RIP_ORACLE,
        "controller_seat_id": 1,
        "owner_seat_id": 1,
    }
    turn: dict[str, Any] = {"turn_number": 20, "active_player": 1, "priority_player": 1, "phase": phase}
    if step:
        turn["step"] = step
    return {
        "match_id": "synthetic-match",
        "local_seat_id": 1,
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 18, "lands_played": 0, "hand_size": 3},
            {"seat_id": 2, "is_local": False, "life_total": 18, "lands_played": 0, "hand_size": 2},
        ],
        "turn": turn,
        "battlefield": [*plains, scavenger, opp_flier_a, opp_flier_b],
        "hand": [rescue_1, rescue_2, seam_rip],
        "stack": [spider_on_stack] if stack is None else stack,
    }


def _rescue_menu(*, include_seam_rip: bool = False) -> PendingDecision:
    opts = [
        DecisionOption(
            "idx:0",
            "Cast Shardmage's Rescue",
            True,
            {"actionType": "ActionType_Cast", "instanceId": 501, "hasAutoTap": True},
        ),
        DecisionOption(
            "idx:1",
            "Cast Shardmage's Rescue",
            True,
            {"actionType": "ActionType_Cast", "instanceId": 502, "hasAutoTap": True},
        ),
    ]
    if include_seam_rip:
        opts.append(
            DecisionOption(
                "idx:2",
                "Cast Seam Rip",
                True,
                {"actionType": "ActionType_Cast", "instanceId": 503, "hasAutoTap": True},
            )
        )
    opts.append(DecisionOption("pass", "Pass", True, {}))
    return PendingDecision((20, 1), "ActionsAvailable", tuple(opts), can_pass=True)


def _t20_game_plan(*, attack: str = "none") -> GamePlan:
    return GamePlan(
        turn_formed=20,
        role="control",
        win_conditions=["Evasive flyers + aura protection"],
        turn_plan=[
            {
                "turn": 20,
                "land": "",
                "cast": ["Optimistic Scavenger", "Skyward Spider"],
                "attack": attack,
                "hold": _T20_HOLD,
            }
        ],
    )


def _make_planner(
    backend: _ScriptedBackend, plan: GamePlan | None = None
) -> tuple[ActionPlanner, GamePlanManager | None]:
    planner = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    if plan is None:
        return planner, None
    mgr = GamePlanManager(backend)
    mgr._match_id = "synthetic-match"
    mgr._plan = plan
    planner.set_game_plan_source(mgr.strategy_block, step_source=mgr.current_step)
    return planner, mgr


# ── 1. Stack-aware GamePlan progress ───────────────────────────────────────────


def test_with_progress_marks_entered_done_and_stack_resolving_and_keeps_hold():
    state = _t20_state()
    plan = _t20_game_plan()
    step = plan.step_for(20)
    assert step is not None

    marked = _with_progress(step, assess(state), state)
    assert marked["cast"] == [
        f"Optimistic Scavenger{_DONE}",
        f"Skyward Spider{_ON_STACK}",
    ]
    assert marked["hold"] == _T20_HOLD

    # Assessment-free path + as_planner_block(state=state) + compose_strategy_block(None, ...)
    marked_no_assess = _with_progress(step, None, state)
    assert marked_no_assess["cast"] == marked["cast"]
    assert marked_no_assess["hold"] == _T20_HOLD

    block = plan.as_planner_block(state=state)
    assert f"Optimistic Scavenger{_DONE}" in block
    assert f"Skyward Spider{_ON_STACK}" in block
    assert f"hold: {_T20_HOLD}" in block

    composed = compose_strategy_block(None, plan, state=state)
    assert f"Skyward Spider{_ON_STACK}" in composed
    assert f"hold: {_T20_HOLD}" in composed


def test_with_progress_ignores_abilities_and_countered_stack_items():
    state = _t20_state(
        stack=[
            {
                "instance_id": 401,
                "name": "Skyward Spider",
                "type_line": "Creature — Spider Human Hero",
                "controller_seat_id": 1,
                "countered": True,
            },
            {
                "instance_id": 402,
                "name": "Skyward Spider",
                "object_kind": "ABILITY",
                "controller_seat_id": 1,
            },
        ]
    )
    step = _t20_game_plan().step_for(20)
    marked = _with_progress(step, None, state)
    assert marked["cast"] == [
        f"Optimistic Scavenger{_DONE}",
        "Skyward Spider",
    ]


# ── 2. Reported Turn 20 failing case (with and without GamePlan) ───────────────


def test_turn20_own_stack_continuation_passes_in_zero_llm_calls_with_game_plan():
    state = _t20_state()
    backend = _ScriptedBackend()
    planner, _mgr = _make_planner(backend, _t20_game_plan())

    chosen = planner.plan_decision_options(_rescue_menu(), state)

    assert chosen == ["pass"]
    assert backend.calls == []
    trace = planner.get_last_decision_trace()
    assert trace["policy"] == "own_stack_continuation"
    assert trace["own_stack"] == "Skyward Spider"
    assert trace["validated_ids"] == ["pass"]


def test_turn20_own_stack_continuation_passes_without_game_plan_for_beneficial_flash_aura():
    state = _t20_state()
    backend = _ScriptedBackend()
    planner, _ = _make_planner(backend, plan=None)

    chosen = planner.plan_decision_options(_rescue_menu(), state)

    assert chosen == ["pass"]
    assert backend.calls == []
    trace = planner.get_last_decision_trace()
    assert trace["policy"] == "own_stack_continuation"
    assert trace["own_stack"] == "Skyward Spider"


def test_own_stack_reserved_option_holds_flash_seam_rip_under_game_plan_hold():
    state = _t20_state()
    # Give Seam Rip flash (e.g. Leyline effect) so it could be offered while stack is non-empty.
    state["hand"][2]["oracle_text"] = "Flash\n" + _SEAM_RIP_ORACLE
    plan = _t20_game_plan()
    backend = _ScriptedBackend()
    planner, _ = _make_planner(backend, plan)

    chosen = planner.plan_decision_options(_rescue_menu(include_seam_rip=True), state)
    assert chosen == ["pass"]
    assert backend.calls == []

    # Verify own_stack_reserved_option returns a reason via plan_text THIS TURN line too.
    plan_text = compose_strategy_block(assess(state), plan, state=state)
    seam_opt = _rescue_menu(include_seam_rip=True).options[2]
    reason = own_stack_reserved_option(seam_opt, state["hand"][2], state, plan_text=plan_text)
    assert reason


# ── 3. Opposite controls ──────────────────────────────────────────────────────


def test_opposite_control_opposing_removal_on_stack_consults_llm_and_does_not_autopass():
    cut_down = {
        "instance_id": 499,
        "grp_id": 90099,
        "name": "Cut Down",
        "type_line": "Instant",
        "oracle_text": _CUT_DOWN_ORACLE,
        "controller_seat_id": 2,
        "owner_seat_id": 2,
        "targets": [201],
    }
    # Both alongside Skyward Spider and alone on stack.
    for stack in ([cut_down, _t20_state()["stack"][0]], [cut_down]):
        state = _t20_state(stack=stack)
        backend = _ScriptedBackend(['{"option_ids": ["idx:0"], "reasoning": "Save Optimistic Scavenger"}'])
        planner, _ = _make_planner(backend, _t20_game_plan())

        chosen = planner.plan_decision_options(_rescue_menu(), state)

        assert len(backend.calls) == 1
        assert chosen == ["idx:0"]


def test_opposite_control_combat_step_allows_flashing_rescue_even_with_own_trigger_on_stack():
    own_trigger = {
        "instance_id": 450,
        "name": "Optimistic Scavenger",
        "object_kind": "ABILITY",
        "type_line": "Creature — Human Scout",
        "controller_seat_id": 1,
        "owner_seat_id": 1,
    }
    for step_name in ("Step_DeclareBlock", "Step_CombatDamage"):
        state = _t20_state(stack=[own_trigger], phase="Phase_Combat", step=step_name)
        state["battlefield"][5]["is_attacking"] = True
        backend = _ScriptedBackend(['{"option_ids": ["idx:0"], "reasoning": "Pump and protect attacker"}'])
        planner, _ = _make_planner(backend, _t20_game_plan())

        chosen = planner.plan_decision_options(_rescue_menu(), state)

        assert len(backend.calls) == 1
        assert chosen == ["idx:0"]


def test_opposite_control_uncast_planned_spell_and_unheld_instant_not_held():
    assert card_held_by_plan("Opt", "Hold Opt", planned_casts=["Opt (draw)"]) is False
    assert card_held_by_plan("Opt", "Hold Opt", planned_casts=["Opt ✓"]) is True
    assert (
        card_held_by_plan(
            "Opt", "Hold Shardmage's Rescue", planned_casts=["Skyward Spider ⏳ on stack (resolving)"]
        )
        is False
    )
    assert card_held_by_plan("Opt", "Don't hold Opt") is False
    assert card_held_by_plan("Opt", "Do not hold Opt") is False
    assert card_held_by_plan("Opt", "Cast Opt now") is False
    assert card_held_by_plan("Opt", "Hold Optimistic Scavenger") is False
    assert card_held_by_plan("Fire // Ice", "Hold Fire") is True
    assert card_held_by_plan("Shardmage’s Rescue", _T20_HOLD) is True
    assert (
        card_held_by_plan("Jace, the Mind Sculptor", "Hold Jace, the Mind Sculptor, don't hold Opt") is True
    )
    assert card_held_by_plan("Opt", "Hold Jace, the Mind Sculptor, don't hold Opt") is False

    # Unheld instant Opt offered while our Skyward Spider is on the stack: must consult LLM.
    state = _t20_state()
    opt_card = {
        "instance_id": 504,
        "grp_id": 90008,
        "name": "Opt",
        "type_line": "Instant",
        "mana_cost": "{U}",
        "oracle_text": _OPT_ORACLE,
        "controller_seat_id": 1,
        "owner_seat_id": 1,
    }
    state["hand"].append(opt_card)
    menu = PendingDecision(
        (20, 2),
        "ActionsAvailable",
        (
            DecisionOption(
                "idx:0",
                "Cast Shardmage's Rescue",
                True,
                {"actionType": "ActionType_Cast", "instanceId": 501, "hasAutoTap": True},
            ),
            DecisionOption(
                "idx:1",
                "Cast Opt",
                True,
                {"actionType": "ActionType_Cast", "instanceId": 504, "hasAutoTap": True},
            ),
            DecisionOption("pass", "Pass", True, {}),
        ),
        can_pass=True,
    )
    backend = _ScriptedBackend(['{"option_ids": ["idx:1"], "reasoning": "Cast unheld Opt"}'])
    planner, _ = _make_planner(backend, _t20_game_plan())

    chosen = planner.plan_decision_options(menu, state)

    assert len(backend.calls) == 1
    assert chosen == ["idx:1"]


def test_opposite_control_planned_uncast_spell_in_hold_text_is_not_auto_passed():
    state = _t20_state()
    opt_card = {
        "instance_id": 504,
        "grp_id": 90008,
        "name": "Opt",
        "type_line": "Instant",
        "mana_cost": "{U}",
        "oracle_text": _OPT_ORACLE,
        "controller_seat_id": 1,
        "owner_seat_id": 1,
    }
    state["hand"] = [opt_card]
    plan = GamePlan(
        turn_formed=20,
        win_conditions=["Card advantage"],
        turn_plan=[
            {"turn": 20, "land": "", "cast": ["Skyward Spider", "Opt"], "attack": "none", "hold": "Hold Opt"}
        ],
    )
    menu = PendingDecision(
        (20, 3),
        "ActionsAvailable",
        (
            DecisionOption(
                "idx:0",
                "Cast Opt",
                True,
                {"actionType": "ActionType_Cast", "instanceId": 504, "hasAutoTap": True},
            ),
            DecisionOption("pass", "Pass", True, {}),
        ),
        can_pass=True,
    )
    backend = _ScriptedBackend(['{"option_ids": ["idx:0"], "reasoning": "Cast planned Opt"}'])
    planner, _ = _make_planner(backend, plan)

    chosen = planner.plan_decision_options(menu, state)

    assert len(backend.calls) == 1
    assert chosen == ["idx:0"]


# ── 4. TurnPlan unification & autopilot payload emission ──────────────────────


def test_turn_plan_unification_projects_game_plan_and_attack_intent():
    state = _t20_state()
    plan = _t20_game_plan(attack="Optimistic Scavenger")
    backend = _ScriptedBackend()
    planner, _ = _make_planner(backend, plan)

    chosen = planner.plan_decision_options(_rescue_menu(), state)
    assert chosen == ["pass"]

    payload = planner.get_turn_plan_payload()
    assert payload is not None
    assert payload["turn_number"] == 20
    assert payload["current_idx"] == 1
    assert payload["steps"] == [
        {
            "action_type": "cast_spell",
            "card_name": "Optimistic Scavenger",
            "target_names": [],
            "rationale": "",
            "status": "done",
        },
        {
            "action_type": "cast_spell",
            "card_name": "Skyward Spider",
            "target_names": [],
            "rationale": "on stack (resolving)",
            "status": "current",
        },
        {
            "action_type": "declare_attackers",
            "card_name": "Optimistic Scavenger",
            "target_names": [],
            "rationale": "",
            "status": "pending",
        },
    ]
    assert planner.has_pending_attack_intent() is True

    # Moving past combat marks declare_attackers done -> pending attack intent becomes False.
    main2 = deepcopy(state)
    main2["stack"] = []
    main2["turn"]["phase"] = "Phase_Main2"
    planner.sync_turn_plan_from_game_plan(main2)
    assert planner.has_pending_attack_intent() is False

    # Turn without a GamePlan step clears the GamePlan-projected TurnPlan.
    opp_turn = deepcopy(state)
    opp_turn["turn"] = {"turn_number": 21, "active_player": 2, "phase": "Phase_Main1"}
    assert planner.sync_turn_plan_from_game_plan(opp_turn) is None
    assert planner.get_turn_plan_payload() is None


def test_sync_turn_plan_preserves_legacy_turn_plan_when_step_source_unset():
    planner = ActionPlanner(_ScriptedBackend(), timeout=1.0, land_drop_first=False)
    legacy = TurnPlan(turn_number=4, steps=[TurnPlanStep(action_type="play_land", card_name="Plains")])
    planner._active_turn_plan = legacy

    assert planner.sync_turn_plan_from_game_plan(_t20_state()) is None
    assert planner._active_turn_plan is legacy


def test_autopilot_handle_typed_decision_emits_turn_plan_payload():
    state = _t20_state()
    plan = _t20_game_plan(attack="Optimistic Scavenger")
    backend = _ScriptedBackend()
    planner, mgr = _make_planner(backend, plan)

    emitted: list[dict[str, Any] | None] = []
    poll = {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "game_state_id": 20,
        "can_pass": True,
        "can_cancel": False,
        "actions": [
            {"actionType": "ActionType_Cast", "instanceId": 501, "hasAutoTap": True},
            {"actionType": "ActionType_Cast", "instanceId": 502, "hasAutoTap": True},
        ],
    }

    with patch("arenamcp.autopilot.get_bridge", return_value=MagicMock()):
        engine = AutopilotEngine(
            planner=planner,
            config=AutopilotConfig(dry_run=False),
            ui_turn_plan_fn=emitted.append,
        )
    engine._gre_bridge = MagicMock()
    engine._gre_bridge.connected = True
    engine._gre_bridge.get_pending_actions.return_value = poll
    engine._game_plan_mgr = mgr

    assert engine._try_typed_decision_path(state, "decision_required") is True
    assert backend.calls == []
    assert len(emitted) == 1
    assert emitted[0] is not None
    assert emitted[0]["turn_number"] == 20
    assert emitted[0]["steps"][0]["status"] == "done"
    assert emitted[0]["steps"][1]["status"] == "current"
    assert emitted[0]["steps"][1]["rationale"] == "on stack (resolving)"
    assert emitted[0]["steps"][2]["action_type"] == "declare_attackers"


def test_plan_actions_combat_preserves_game_plan_projected_turn_plan():
    state = _t20_state(stack=[], phase="Phase_Main1")
    plan = _t20_game_plan(attack="Optimistic Scavenger")
    backend = _ScriptedBackend(
        [
            '{"reasoning": "attack", "actions": [{"action_type": "declare_attackers", "targets": ["Optimistic Scavenger"]}]}'
        ]
    )
    planner, _mgr = _make_planner(backend, plan)

    main1_menu = PendingDecision(
        request_id=(1, 1),
        request_type="ActionsAvailable",
        can_pass=True,
        options=(DecisionOption("pass", "Pass", payable=True, meta={"actionType": "ActionType_Pass"}),),
    )
    assert planner.plan_decision_options(main1_menu, state) == ["pass"]
    assert planner._turn_memo_turn != 20
    assert planner.has_pending_attack_intent() is True

    combat = deepcopy(state)
    combat["turn"]["phase"] = "Phase_Combat"
    combat["turn"]["step"] = "Step_DeclareAttack"
    combat["battlefield"][-1]["turn_entered_battlefield"] = 19  # non-summoning-sick attacker
    combat["_bridge_request_type"] = "DeclareAttackers"
    combat["_bridge_legal_attackers"] = [{"instance_id": 201, "name": "Optimistic Scavenger"}]

    plan_result = planner.plan_actions(
        combat,
        "combat",
        ["declare_attackers(Optimistic Scavenger)", "pass"],
        {
            "type": "declare_attackers",
            "legal_attackers": [{"instance_id": 201, "name": "Optimistic Scavenger"}],
        },
    )
    assert plan_result.actions
    payload = planner.get_turn_plan_payload()
    assert payload is not None
    assert payload["turn_number"] == 20
    assert any(step["action_type"] == "declare_attackers" for step in payload["steps"])
    assert planner.has_pending_attack_intent() is True


def test_card_held_by_plan_rejects_negative_contextual_and_handles_split_cards():
    assert card_held_by_plan("Shardmage's Rescue", "Hold Shardmage's Rescue instead of Opt") is True
    assert card_held_by_plan("Opt", "Hold Shardmage's Rescue instead of Opt") is False
    assert card_held_by_plan("Opt", "Hold {W} (not Opt)") is False
    assert card_held_by_plan("Opt", "Cast Opt before holding {W}") is False
    assert card_held_by_plan("Skyward Spider", "Hold Shardmage's Rescue to protect Skyward Spider") is False
    assert card_held_by_plan("Skyward Spider", "Hold Shardmage's Rescue (for Skyward Spider)") is False
    assert (
        card_held_by_plan("Jace, the Mind Sculptor", "Hold Jace, the Mind Sculptor, don't hold Opt") is True
    )
    assert card_held_by_plan("Opt", "Hold Jace, the Mind Sculptor, don't hold Opt") is False

    # Split / MDFC names: front & back faces held when explicitly governed; common words ignored otherwise.
    assert card_held_by_plan("Fire // Ice", "Hold Ice for end step") is True
    assert card_held_by_plan("Fire // Ice", "Hold {R} for Fire") is True
    assert card_held_by_plan("Fire // Ice", "Hold Fire, Ice") is True
    assert card_held_by_plan("Turn // Burn", "Hold removal for their turn") is False
    assert card_held_by_plan("Turn // Burn", "Hold removal until turn 5") is False
    assert card_held_by_plan("Turn // Burn", "Hold Burn") is True

    # _with_progress matches Fire // Ice on the stack or battlefield against single-face planned cast "Ice".
    split_plan = {
        "turn": 6,
        "label": "T6",
        "cast": ["Ice"],
        "hold": "Counterspell",
    }
    split_state = {
        "local_seat_id": 1,
        "players": [{"seat_id": 1, "is_local": True, "lands_played": 0}],
        "turn": {"turn_number": 6, "active_player": 1, "phase": "Phase_Main1"},
        "battlefield": [],
        "stack": [
            {
                "instance_id": 701,
                "name": "Fire // Ice",
                "object_kind": "SPELL",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
            }
        ],
    }
    progressed = _with_progress(split_plan, None, split_state)
    assert progressed["cast"] == [f"Ice{_ON_STACK}"]


def test_opponent_turn_and_held_instant_merge_and_lethal_reach():
    state = _t20_state(
        stack=[{"instance_id": 600, "name": "Opt", "controller_seat_id": 1, "owner_seat_id": 1}]
    )
    state["turn"] = {"turn_number": 21, "active_player": 2, "phase": "Phase_Main1"}
    state["hand"].append(
        {
            "instance_id": 501,
            "name": "Shardmage's Rescue",
            "mana_cost": "{W}",
            "type_line": "Enchantment — Aura",
            "oracle_text": _RESCUE_ORACLE,
            "owner_seat_id": 1,
            "controller_seat_id": 1,
        }
    )
    rescue_opt = DecisionOption(
        "idx:0",
        "Cast Shardmage's Rescue",
        payable=True,
        meta={"actionType": "ActionType_Cast", "instanceId": 501},
    )
    future_plan_text = "THIS TURN (T22, our next turn): cast Origin of Spider-Man; hold: Shardmage's Rescue"
    # When GamePlanManager.current_step returned None (suppressed on opponent's turn 21),
    # own_stack_reserved_option must NOT import T22's hold into T21.
    assert (
        own_stack_reserved_option(
            rescue_opt,
            state["hand"][-1],
            state,
            plan_step=None,
            plan_text=future_plan_text,
            step_source_active=True,
        )
        == ""
    )
    # Even without step_source_active, T22 header mismatch prevents T21 enforcement.
    assert (
        own_stack_reserved_option(
            rescue_opt,
            state["hand"][-1],
            state,
            plan_step=None,
            plan_text=future_plan_text,
            step_source_active=False,
        )
        == ""
    )

    # Own-stack holds + _hold_instant_burn preserve both cards & notes rather than clobbering.
    own_state = _t20_state()
    own_state["hand"].append(
        {
            "instance_id": 505,
            "name": "Shock",
            "mana_cost": "{R}",
            "type_line": "Instant",
            "oracle_text": "Shock deals 2 damage to any target.",
            "owner_seat_id": 1,
            "controller_seat_id": 1,
        }
    )
    shock_opt = DecisionOption(
        "idx:2",
        "Cast Shock",
        payable=True,
        meta={"actionType": "ActionType_Cast", "instanceId": 505},
    )
    pass_opt = DecisionOption("pass", "Pass", payable=True, meta={"actionType": "ActionType_Pass"})
    menu = PendingDecision(
        request_id=(20, 9),
        request_type="ActionsAvailable",
        can_pass=True,
        options=(rescue_opt, shock_opt, pass_opt),
    )
    plan = _t20_game_plan()
    backend = _ScriptedBackend()
    planner, _ = _make_planner(backend, plan)
    # Simulating _own_stack_continuation_pass followed by _hold_instant_burn in upkeep:
    upkeep_state = deepcopy(own_state)
    upkeep_state["turn"]["phase"] = "Phase_Beginning"
    upkeep_state["turn"]["step"] = "Step_Upkeep"
    planner._held_instants = []
    planner._held_notes = []
    filtered, pick = planner._own_stack_continuation_pass(menu, upkeep_state)
    assert pick == []
    planner._hold_instant_burn(filtered, upkeep_state)
    assert "Shardmage's Rescue" in planner._held_instants
    assert "Shock" in planner._held_instants
    assert any("OWN-STACK TIMING" in n for n in planner._held_notes)
    assert any("UPKEEP:" in n for n in planner._held_notes)

    # lethal_reach must abort own-stack auto-pass even when _lethal_this_turn is False/None.
    with patch("arenamcp.action_planner.lethal_reach", return_value=MagicMock(text=lambda: "burn lethal")):
        kept_menu, lethal_pick = planner._own_stack_continuation_pass(menu, own_state)
    assert lethal_pick == []
    assert len(kept_menu.options) == 3
