"""NN6 verification: E1 planner_certain gate honesty and bounded mechanical post-cast hops."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest
from tests.fakes import make_engine

from arenamcp import line_guard as lg
from arenamcp.autopilot import AutopilotEngine
from arenamcp.autopilot_models import AutopilotState
from arenamcp.decisions import DecisionOption, PendingDecision, build_pending_decision
from arenamcp.guard_settings import GUARD_DEFAULTS
from arenamcp.line_search import ALIVE


def _cast_poll(state_id: int = 1, msg_id: int = 100, should_stop: bool = True) -> dict:
    return {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "game_state_id": state_id,
        "msg_id": msg_id,
        "can_pass": True,
        "can_cancel": True,
        "actions": [
            {
                "actionType": "Cast",
                "instanceId": 10,
                "grpId": 115,
                "abilityGrpId": 115,
                "hasAutoTap": True,
                **({"shouldStop": True} if should_stop else {}),
            }
        ],
    }


def _followup_substep_poll(request_type: str, state_id: int = 2, msg_id: int = 200) -> dict:
    base = {
        "has_pending": True,
        "request_type": request_type,
        "game_state_id": state_id,
        "msg_id": msg_id,
        "can_cancel": True,
    }
    if request_type == "SelectTargets":
        return {
            **base,
            "source_instance_id": 10,
            "target_candidates": [{"instanceId": 501, "grpId": 123, "targetIdx": 0, "legalAction": "Select"}],
            "target_selections": [{"targetIdx": 0, "minTargets": 1, "maxTargets": 1}],
        }
    if request_type == "CastingTimeOptions":
        return {
            **base,
            "actions": [
                {
                    "actionType": "CastingTimeOption",
                    "choiceKind": "modal",
                    "minSel": 1,
                    "maxSel": 1,
                    "grpId": 115,
                    "label": "Mode 1",
                }
            ],
        }
    if request_type == "Search":
        return {
            **base,
            "source_instance_id": 10,
            "search_candidates": [601],
            "min_select": 0,
            "max_select": 1,
        }
    if request_type in ("SearchFromGroups", "SelectFromGroups"):
        return {
            **base,
            "source_instance_id": 10,
            "select_from_groups": [{"group_id": 0, "ids": [601], "min": 0, "max": 1}],
        }
    if request_type == "SelectN":
        return {
            **base,
            "source_instance_id": 10,
            "min_select": 1,
            "max_select": 1,
            "select_n_ids": [701, 702],
        }
    if request_type == "OptionalAction":
        return {**base, "source_instance_id": 10}
    if request_type == "Group":
        return {
            **base,
            "source_instance_id": 10,
            "group_context": "GroupingContext_Scry",
            "group_instance_ids": [801],
            "group_specs": [
                {
                    "zoneType": "ZoneType_Library",
                    "subZoneType": "SubZoneType_Top",
                    "lowerBound": 0,
                    "upperBound": 1,
                },
                {
                    "zoneType": "ZoneType_Library",
                    "subZoneType": "SubZoneType_Bottom",
                    "lowerBound": 0,
                    "upperBound": 1,
                },
            ],
        }
    raise AssertionError(f"unexpected request_type: {request_type}")


def _sole_pass_poll(state_id: int = 2, msg_id: int = 201) -> dict:
    return {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "game_state_id": state_id,
        "msg_id": msg_id,
        "can_pass": True,
        "can_cancel": False,
        "actions": [{"actionType": "Pass"}],
    }


def _cast_and_pass_poll(
    *,
    instance_id: int = 301,
    grp_id: int = 99301,
    can_cancel: bool = False,
    state_id: int = 2,
    msg_id: int = 202,
) -> dict:
    return {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "game_state_id": state_id,
        "msg_id": msg_id,
        "can_pass": True,
        "can_cancel": can_cancel,
        "actions": [
            {
                "actionType": "Cast",
                "instanceId": instance_id,
                "grpId": grp_id,
                "abilityGrpId": grp_id,
                "hasAutoTap": True,
            },
            {"actionType": "Pass"},
        ],
    }


def _own_stack_state(
    *,
    stack_owner: int = 1,
    phase: str = "Phase_Main1",
    hand_card: dict | None = None,
    attacking: bool = False,
) -> dict:
    card = hand_card or {
        "instance_id": 301,
        "grp_id": 99301,
        "name": "Shardmage's Rescue",
        "type_line": "Enchantment — Aura",
        "mana_cost": "{W}",
        "oracle_text": (
            "Flash\n"
            "Enchant creature you control\n"
            "As long as this Aura entered this turn, enchanted creature has hexproof.\n"
            "Enchanted creature gets +1/+1."
        ),
        "owner_seat_id": 1,
        "controller_seat_id": 1,
    }
    return {
        "match_id": "nn6-test-match",
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "turn": {
            "turn_number": 3,
            "active_player": 1,
            "priority_player": 1,
            "decision_player": 1,
            "phase": phase,
            "step": "",
        },
        "players": [
            {"seat_id": 1, "life_total": 20, "is_local": True},
            {"seat_id": 2, "life_total": 20, "is_local": False},
        ],
        "battlefield": [
            {
                "instance_id": 401,
                "name": "Optimistic Scavenger",
                "type_line": "Creature — Human Scout",
                "power": 1,
                "toughness": 1,
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "is_attacking": attacking,
            }
        ],
        "hand": [card],
        "stack": [
            {
                "instance_id": 201,
                "name": "Optimistic Scavenger",
                "type_line": "Creature — Human Scout",
                "object_kind": "SPELL",
                "owner_seat_id": stack_owner,
                "controller_seat_id": stack_owner,
            }
        ],
        "graveyard": [],
        "exile": [],
    }


def _hop_engine(followup_poll: dict, state: dict) -> tuple[AutopilotEngine, list[tuple]]:
    engine = make_engine(game_state=state)
    engine._POST_CAST_PRIORITY_POLL_S = 0.05
    engine._POST_CAST_PRIORITY_POLL_INTERVAL_S = 0.001
    engine._gre_bridge = NS(connected=True, get_pending_actions=lambda: followup_poll)
    engine._get_game_state = lambda: dict(state)
    calls: list[tuple] = []
    engine._try_typed_decision_path = lambda *a, **k: calls.append((a, k)) or True
    return engine, calls


def test_nn6_planner_certain_default_remains_shadow_and_enforces_opposite_controls(monkeypatch):
    # Gate: 0 labelled B3 fires and <20 B2 shadow matches -> keep default "shadow" until NN7.
    assert GUARD_DEFAULTS["planner_certain"] == "shadow"

    monkeypatch.setattr(
        "arenamcp.board_assessment.assess",
        lambda _s: NS(lethal_now=False, survival_mode=False, unknown_bodies=()),
    )
    monkeypatch.setattr(
        lg, "action_key", lambda o, _s: ("nocast",) if o.option_id == "pass" else (o.option_id,)
    )
    monkeypatch.setattr(lg, "_unmodelled_finisher", lambda *a: False)

    def _line(v: float, *, timing: int = 0, step_land: bool = False):
        return NS(
            v=v,
            timing=timing,
            cls=ALIVE,
            complete=True,
            unsupported=(),
            dead_at=None,
            win_at=None,
            ranked=(ALIVE, 0, v),
            score=(ALIVE, 0, v),
            steps=(NS(land=step_land),),
        )

    result = NS(
        truncated=False,
        dead_now=False,
        unsupported=(),
        choice_caveats=(),
        bounded=False,
        dead_in=None,
        dead_in_greedy=None,
        all_dead_at_first=False,
        cast_trigger_risk=False,
        posture="hold",
        model=NS(our_turn=True, stack_nonempty=False, unknown_bodies=(), phase="Phase_Main2"),
        first_action={("cast:1",): _line(10.0), ("nocast",): _line(1.0)},
    )
    decision = PendingDecision(
        (1, 1),
        "ActionsAvailable",
        (DecisionOption("cast:1", "Cast Bolt"), DecisionOption("pass", "Pass")),
        can_pass=True,
        can_cancel=False,
    )
    assert lg.planner_certain_pick(result, decision, {"stack": []}) == "cast:1"

    # Opposite control 1: Main1 abstains even on clear margin.
    main1 = NS(**{**result.__dict__, "model": NS(**{**result.model.__dict__, "phase": "Phase_Main1"})})
    assert lg.planner_certain_pick(main1, decision, {"stack": []}) is None

    # Opposite control 2: margin below LINE_GUARD_MARGIN abstains.
    tight = NS(**{**result.__dict__, "first_action": {("cast:1",): _line(2.0), ("nocast",): _line(1.8)}})
    assert lg.planner_certain_pick(tight, decision, {"stack": []}) is None

    # Opposite control 3: survival mode abstains.
    monkeypatch.setattr(
        "arenamcp.board_assessment.assess",
        lambda _s: NS(lethal_now=False, survival_mode=True, unknown_bodies=()),
    )
    assert lg.planner_certain_pick(result, decision, {"stack": []}) is None


def test_nn6_hop_constants_preserve_contract():
    assert AutopilotEngine._MAX_POST_CAST_PRIORITY_HOPS == 1
    assert AutopilotEngine._MAX_MECHANICAL_POST_CAST_HOPS == 3


@pytest.mark.parametrize(
    "request_type",
    [
        "SelectTargets",
        "CastingTimeOptions",
        "Search",
        "SearchFromGroups",
        "SelectFromGroups",
        "SelectN",
        "OptionalAction",
        "Group",
    ],
)
def test_nn6_mechanical_substeps_continue_past_primary_hop_limit(request_type):
    state = {"match_id": "m1", "local_seat_id": 1}
    engine, calls = _hop_engine(_followup_substep_poll(request_type), state)
    answered = build_pending_decision(_cast_poll(), resolve_instance=lambda _iid: "")
    engine._surface_post_cast_priority(
        answered,
        ["idx:0"],
        _cast_poll(),
        {},
        "decision_required",
        depth=engine._MAX_POST_CAST_PRIORITY_HOPS,
    )
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0]["_bridge_request_type"] == request_type
    assert kwargs["depth"] == engine._MAX_POST_CAST_PRIORITY_HOPS + 1


def test_nn6_sole_pass_continues_past_primary_hop_limit():
    state = {"match_id": "m1", "local_seat_id": 1}
    engine, calls = _hop_engine(_sole_pass_poll(), state)
    answered = build_pending_decision(_cast_poll(), resolve_instance=lambda _iid: "")
    engine._surface_post_cast_priority(
        answered,
        ["idx:0"],
        _cast_poll(),
        {},
        "decision_required",
        depth=engine._MAX_POST_CAST_PRIORITY_HOPS,
    )
    assert len(calls) == 1
    assert calls[0][1]["depth"] == engine._MAX_POST_CAST_PRIORITY_HOPS + 1


def test_nn6_own_stack_continuation_continues_without_side_effects():
    state = _own_stack_state(stack_owner=1, phase="Phase_Main1")
    poll = _cast_and_pass_poll(can_cancel=False)
    engine, calls = _hop_engine(poll, state)
    # Attach a real-shaped read-only planner attribute holder to ensure classifier is side-effect-free.
    planner = NS(
        _held_instants=[],
        _held_notes=[],
        _plan_step_source=lambda _s: None,
        _plan_text_now=lambda _s: "",
    )
    engine._planner = planner

    answered = build_pending_decision(_cast_poll(), resolve_instance=lambda _iid: "")
    engine._surface_post_cast_priority(
        answered,
        ["idx:0"],
        _cast_poll(),
        {},
        "decision_required",
        depth=engine._MAX_POST_CAST_PRIORITY_HOPS,
    )
    assert len(calls) == 1
    assert calls[0][1]["depth"] == engine._MAX_POST_CAST_PRIORITY_HOPS + 1
    assert planner._held_instants == []
    assert planner._held_notes == []


@pytest.mark.parametrize(
    "case",
    [
        "hostile_opponent_stack",
        "empty_stack_nonpass",
        "cancellable_menu",
        "combat_window",
        "unreserved_instant_burn",
        "hard_mechanical_cap_substep",
        "hard_mechanical_cap_sole_pass",
    ],
)
def test_nn6_opposite_controls_yield_at_hop_limit(case):
    depth = AutopilotEngine._MAX_POST_CAST_PRIORITY_HOPS
    if case == "hostile_opponent_stack":
        state = _own_stack_state(stack_owner=2, phase="Phase_Main1")
        poll = _cast_and_pass_poll(can_cancel=False)
    elif case == "empty_stack_nonpass":
        state = _own_stack_state(stack_owner=1, phase="Phase_Main1")
        state["stack"] = []
        poll = _cast_and_pass_poll(can_cancel=False)
    elif case == "cancellable_menu":
        state = _own_stack_state(stack_owner=1, phase="Phase_Main1")
        poll = _cast_and_pass_poll(can_cancel=True)
    elif case == "combat_window":
        state = _own_stack_state(stack_owner=1, phase="Phase_Combat", attacking=True)
        poll = _cast_and_pass_poll(can_cancel=False)
    elif case == "unreserved_instant_burn":
        opt_spell = {
            "instance_id": 302,
            "grp_id": 99302,
            "name": "Opt",
            "type_line": "Instant",
            "mana_cost": "{U}",
            "oracle_text": "Scry 1, then draw a card.",
            "owner_seat_id": 1,
            "controller_seat_id": 1,
        }
        state = _own_stack_state(stack_owner=1, phase="Phase_Main1", hand_card=opt_spell)
        poll = _cast_and_pass_poll(instance_id=302, grp_id=99302, can_cancel=False)
    elif case == "hard_mechanical_cap_substep":
        state = {"match_id": "m1", "local_seat_id": 1}
        poll = _followup_substep_poll("SelectTargets")
        depth = AutopilotEngine._MAX_MECHANICAL_POST_CAST_HOPS
    else:
        state = {"match_id": "m1", "local_seat_id": 1}
        poll = _sole_pass_poll()
        depth = AutopilotEngine._MAX_MECHANICAL_POST_CAST_HOPS

    engine, calls = _hop_engine(poll, state)
    answered = build_pending_decision(_cast_poll(), resolve_instance=lambda _iid: "")
    engine._surface_post_cast_priority(
        answered,
        ["idx:0"],
        _cast_poll(),
        {},
        "decision_required",
        depth=depth,
    )
    assert calls == []


def test_nn6_stale_log_catch_up_and_request_drift_yield_safely():
    # Finding #8a: stale log catch-up failure before mechanical own-stack check yields without recursing.
    own_state = _own_stack_state(stack_owner=1, phase="Phase_Main1")
    poll = _cast_and_pass_poll(can_cancel=False)
    engine1, calls1 = _hop_engine(poll, own_state)
    engine1._await_log_catch_up = lambda *_a, **_kw: None
    answered = build_pending_decision(_cast_poll(), resolve_instance=lambda _iid: "")
    engine1._surface_post_cast_priority(
        answered,
        ["idx:0"],
        _cast_poll(),
        {},
        "decision_required",
        depth=engine1._MAX_POST_CAST_PRIORITY_HOPS,
    )
    assert calls1 == []

    # Finding #8b: if the request drifts to a non-mechanical window (opponent spell on stack) after
    # _surface_post_cast_priority recurses past _MAX_POST_CAST_PRIORITY_HOPS, yield IDLE with 0 LLM calls.
    opp_state = _own_stack_state(stack_owner=2, phase="Phase_Main1")
    engine2 = make_engine(game_state=opp_state)
    engine2._gre_bridge = NS(
        connected=True,
        get_pending_actions=lambda: poll,
        submit_action=lambda *_a, **_kw: pytest.fail("drifted non-mechanical window must not submit"),
    )
    engine2._await_log_catch_up = lambda _p, s: s
    planner_calls: list[int] = []
    engine2._planner = NS(
        plan_decision_options=lambda *_a, **_kw: planner_calls.append(1) or ["pass"],
    )
    res = engine2._try_typed_decision_path(
        opp_state,
        "decision_required",
        depth=engine2._MAX_POST_CAST_PRIORITY_HOPS + 1,
    )
    assert res is True
    assert engine2.state == AutopilotState.IDLE
    assert planner_calls == []
