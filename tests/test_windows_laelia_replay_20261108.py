"""Reduced GRE replays: Favor 34711245/213, Volcano 66ff2998/253→256.

No game, card-server or model calls. Fixtures retain exact offered action and
source IDs; stale/fresh public snapshots model the observed parser lag.

Provenance split: fixtures/windows_laelia_20261108.json is the raw historical
packet (recorded request/action IDs only, never modified here). Board
snapshots in this module are synthetic constructions; the single
visible-board freshness stamp is a clearly labelled synthetic contract
fixture (owner-authorized 2026-11-11), not recorded evidence.
"""

import json
import time
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from tests.test_autopilot_log_catch_up import _engine
from tests.test_tactical_timing_backlog import card

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import build_pending_decision
from arenamcp.play_safety import filter_play_options

FIXTURE = json.loads((Path(__file__).parent / "fixtures/windows_laelia_20261108.json").read_text())
FAVOR = "Enchant creature you control\nWhen this Aura enters, discover 3.\nEnchanted creature gets +1/+1 and has trample."
VOLCANO = "This land enters tapped. {T}: Add {R}. {4}{R}, {T}, Sacrifice this land: Discover 4. Activate only as a sorcery."


def favor_case():
    request = FIXTURE["favor_target_request"]
    payload = request["selectTargetsReq"]
    poll = dict(
        has_pending=True,
        request_type="SelectTargets",
        game_state_id=213,
        msg_id=280,
        source_instance_id=payload["sourceId"],
        request_payload=payload,
        can_cancel=True,
        target_selections=payload["targets"],
        target_candidates=[{"targetInstanceId": 481, "targetIdx": 1}],
    )
    stale = dict(
        local_seat_id=1,
        _log_game_state_id=207,
        _log_summarized_at_gsid=207,
        _bridge_game_state_id=207,
        stack=[],
        turn=dict(turn_number=10, active_player=1, phase="Main1"),
        battlefield=[card(481, "Geological Appraiser", "", "Creature", power=3, toughness=2)],
    )
    fresh = {
        **stale,
        "_log_game_state_id": 213,
        "stack": [card(619, "Etali's Favor", FAVOR, "Enchantment — Aura")],
    }
    return poll, stale, fresh


def test_favor_source_transition_waits_for_real_board_then_keeps_safe_target(monkeypatch):
    poll, stale, fresh = favor_case()
    engine = _engine([fresh])
    selected = engine._await_log_catch_up(poll, stale)
    assert selected is fresh
    selected = {**selected, "_bridge_request_payload": poll["request_payload"]}
    decision = build_pending_decision(poll, resolve_instance=lambda _: "Geological Appraiser")
    planner = ActionPlanner(backend=Mock())
    monkeypatch.setattr("arenamcp.action_planner._ability_rules_text", lambda _: "")
    assert "enchant creature you control" in planner._decision_source_oracle(decision, selected)
    assert planner._gate_harmful_llm_target_picks(decision, selected, ["tgt:481"]) == ["tgt:481"]


@pytest.mark.parametrize("bridge_id", [0, 207, 212, 213])
def test_summarized_log_is_not_proof_stale_bridge_board_is_current(bridge_id):
    poll, stale, _ = favor_case()
    # 213 is included deliberately: the raw historical packet proves only the
    # SelectTargets request id 213 (source 619). A request id alone — with no
    # certified visible-board snapshot — never authorizes recovery.
    stale["_bridge_game_state_id"] = bridge_id
    engine = _engine([stale])
    engine._LOG_CATCH_UP_TIMEOUT_S = 0
    assert engine._await_log_catch_up(poll, stale) is None


def test_synthetic_visible_board_contract_recovers_through_current_bridge_without_log_detail():
    """SYNTHETIC CONTRACT TEST — owner-authorized 2026-11-11 (t_ae19e37a).

    The visible-board snapshot below is constructed synthetically to exercise
    the producer contract: the server stamps ``_bridge_visible_game_state_id``
    only after complete zones/counts, turn, players and a connected bridge,
    and such a certified visible board recovers a summarized log without log
    detail. This is NOT recorded evidence and does not claim to reproduce the
    missing capture for match 34711245; the raw historical packet
    (fixtures/windows_laelia_20261108.json) is unchanged and still proves only
    the SelectTargets request 213 / source 619. Runtime freshness guards are
    untouched.
    """
    poll, stale, fresh = favor_case()
    # Synthetic snapshot: log still behind (207); the bridge reports pending
    # request 213 AND certifies visible-board freshness at 213.
    fresh.update(
        _log_game_state_id=207,
        _bridge_game_state_id=213,
        _bridge_visible_game_state_id=213,
    )
    assert _engine([fresh])._await_log_catch_up(poll, stale) is fresh


def volcano_case(monkeypatch, state_id=253):
    actions = FIXTURE["volcano_actions_" + str(state_id)]["actionsAvailableReq"]["actions"]
    ids = [a["instanceId"] for a in actions if a["actionType"] == "ActionType_Activate_Mana"]
    state = dict(
        local_seat_id=1,
        match_id="66ff2998",
        turn=dict(turn_number=12, active_player=1, phase="Main1"),
        players=[dict(seat_id=1, is_local=True, mana_pool={})],
        stack=[],
        battlefield=[
            card(
                iid,
                "Hidden Volcano" if iid == 745 else "Mountain",
                VOLCANO if iid == 745 else "{T}: Add {R}.",
                "Land",
                is_tapped=False,
            )
            for iid in ids
        ],
    )
    monkeypatch.setattr(
        "arenamcp.decisions._default_ability_description",
        lambda aid: (
            ("", "{4}{R}, {T}, Sacrifice this land: Discover 4. Activate only as a sorcery.")
            if aid == 169779
            else ("", "")
        ),
    )
    poll = dict(
        has_pending=True,
        request_type="ActionsAvailable",
        game_state_id=state_id,
        actions=actions,
        can_pass=True,
    )
    decision = build_pending_decision(
        poll,
        resolve_name=lambda gid: "Hidden Volcano" if gid == 87443 else "Mountain",
        activation_unaffordable=lambda _: False,
    )
    return state, decision


def test_volcano_not_payable_until_offered_land_drop_and_solver_solution(monkeypatch):
    state, decision = volcano_case(monkeypatch)
    original = deepcopy(state)
    kept = filter_play_options(decision, state)
    assert not any(o.meta.get("abilityGrpId") == 169779 for o in kept.options)
    assert any(o.meta.get("actionType") == "ActionType_Play" for o in kept.options)
    assert state == original
    state, decision = volcano_case(monkeypatch, 256)
    assert any(o.meta.get("abilityGrpId") == 169779 for o in filter_play_options(decision, state).options)


def test_payment_cancel_reopens_only_on_new_verified_resources(monkeypatch):
    state, decision = volcano_case(monkeypatch)
    engine = _engine([state])
    engine._report_oops = lambda *a: None
    engine._last_typed_play = (time.monotonic(), 12, "ActionType_Activate", 745, 169779, "hidden volcano")
    engine._withhold_after_self_cancel(state, "PayCosts cancelled (4 mana offered for a cost of 5)")
    state["_bridge_actions"] = [o.meta for o in decision.options]
    args = ("ActionType_Activate", 745, 169779, "hidden volcano")
    assert engine._self_cancel_withheld(state, *args)
    fresh, decision = volcano_case(monkeypatch, 256)
    fresh["_bridge_actions"] = [o.meta for o in decision.options]
    assert not engine._self_cancel_withheld(fresh, *args)


@pytest.mark.parametrize(
    "change", ["no_solution", "same_budget", "wrong_ability", "wrong_source", "target_cancel"]
)
def test_payment_retry_needs_improved_budget_same_ability_and_confirmed_solution(monkeypatch, change):
    state, _ = volcano_case(monkeypatch)
    engine = _engine([state])
    engine._report_oops = lambda *args: None
    engine._last_typed_play = (time.monotonic(), 12, "ActionType_Activate", 745, 169779, "hidden volcano")
    reason = (
        "SelectTargets cancelled"
        if change == "target_cancel"
        else "PayCosts cancelled (4 mana offered for a cost of 5)"
    )
    engine._withhold_after_self_cancel(state, reason)
    fresh, decision = volcano_case(monkeypatch, 256)
    fresh["_bridge_actions"] = [deepcopy(o.meta) for o in decision.options]
    action = next(a for a in fresh["_bridge_actions"] if a.get("abilityGrpId") == 169779)
    if change == "no_solution":
        action["hasAutoTap"] = False
    elif change == "same_budget":
        fresh["battlefield"] = state["battlefield"]
    elif change == "wrong_ability":
        action["abilityGrpId"] = 1004
    elif change == "wrong_source":
        action["instanceId"] = 999
    assert engine._self_cancel_withheld(fresh, "ActionType_Activate", 745, 169779, "hidden volcano")


@pytest.mark.parametrize("flag", ["_bridge_state_stale", "_bridge_state_incomplete"])
def test_current_id_does_not_make_rejected_bridge_overlay_authoritative(flag):
    poll, stale, _ = favor_case()
    stale.update(_bridge_game_state_id=213)
    stale[flag] = True
    engine = _engine([stale])
    engine._LOG_CATCH_UP_TIMEOUT_S = 0
    assert engine._await_log_catch_up(poll, stale) is None


def test_unknown_favor_source_retains_safety_veto(monkeypatch):
    poll, stale, _ = favor_case()
    stale["_bridge_request_payload"] = poll["request_payload"]
    decision = build_pending_decision(poll, resolve_instance=lambda _: "Geological Appraiser")
    planner = ActionPlanner(backend=Mock())
    monkeypatch.setattr("arenamcp.action_planner._ability_rules_text", lambda _: "")
    assert planner._gate_harmful_llm_target_picks(decision, stale, ["tgt:481"]) != ["tgt:481"]


def test_summarized_mac_board_uses_visible_id_without_changing_request_identity():
    poll, stale, fresh = favor_case()
    fresh.update(_log_game_state_id=207, _bridge_game_state_id=0, _bridge_visible_game_state_id=213)
    assert _engine([fresh])._await_log_catch_up(poll, stale) is fresh
