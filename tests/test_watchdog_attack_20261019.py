"""The watchdog's attack follows the planner's fresh plan and never holds back free fliers (#67).

2026-10-19 (match 115944ce, turn 8): standing by after a misread manual play,
the planner advised at 11:14:39.443 "Attack with Slickshot Show-Off,
Slickshot Show-Off, and Emberheart Challenger at the opponent." At .554 the
decision watchdog (20 s left on Arena's clock) declared its safe default, the
combat solver's attack: Emberheart Challenger alone — the opponent's only
creature, Trostani, was tapped, so both 1/2 fliers stayed home for nothing.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from tests.fakes import make_engine

import arenamcp.combat_strategy as combat_strategy
from arenamcp.action_planner import ActionPlan, ActionType, GameAction
from arenamcp.autopilot_models import ClickResult
from arenamcp.combat_strategy import CombatChoice

LOCAL, OPP = 2, 1
FACE = {"type": "DamageRecType_Player", "playerSystemSeatId": OPP}
SLICKSHOT = (
    "Flying, haste\nWhenever you cast a noncreature spell, Slickshot Show-Off gets +2/+0 until end of turn.\n"
    "Plot {1}{R}"
)


def creature(iid, name, seat, power, toughness, oracle="", tapped=False):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Creature",
        "oracle_text": oracle,
        "power": power,
        "toughness": toughness,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "is_tapped": tapped,
    }


def combat_state(*, trostani_tapped=True, turn=8):
    return {
        "match_id": "115944ce",
        "local_seat_id": LOCAL,
        "turn": {
            "turn_number": turn,
            "active_player": LOCAL,
            "phase": "Phase_Combat",
            "step": "Step_DeclareAttack",
        },
        "players": [
            {"seat_id": OPP, "life_total": 13},
            {"seat_id": LOCAL, "life_total": 15, "is_local": True},
        ],
        "battlefield": [
            creature(299, "Trostani, Three Whispers", OPP, 7, 7, tapped=trostani_tapped),
            creature(292, "Slickshot Show-Off", LOCAL, 1, 2, SLICKSHOT),
            creature(305, "Slickshot Show-Off", LOCAL, 1, 2, SLICKSHOT),
            creature(324, "Emberheart Challenger", LOCAL, 2, 2, "Haste, prowess"),
        ],
    }


ATTACK_POLL = {
    "ok": True,
    "has_pending": True,
    "request_type": "DeclareAttackers",
    "request_class": "DeclareAttackersRequest",
    "game_state_id": 229,
    "attackers": [{"attackerInstanceId": iid, "legalDamageRecipients": [FACE]} for iid in (292, 305, 324)],
}


@pytest.fixture
def engine(monkeypatch):
    bridge = MagicMock()
    bridge.connected = True
    bridge.get_pending_actions.return_value = ATTACK_POLL
    engine = make_engine(bridge, MagicMock())
    engine.declared = []

    def declare(action):
        engine.declared.append(action)
        return ClickResult(True, description="attackers", submitted_action=action)

    engine._try_bridge_declare_attackers = declare
    engine._try_interactive_safe_default = lambda state, trigger: (
        engine.declared.append("usual safe default") or True
    )
    # The solver's choice at 11:14:39: Emberheart alone.
    monkeypatch.setattr(
        combat_strategy, "combat_choice", lambda state, **kw: CombatChoice(assignments={324: FACE})
    )
    return engine


ADVICE = ActionPlan(
    actions=[
        GameAction(
            action_type=ActionType.DECLARE_ATTACKERS,
            attacker_names=["Slickshot Show-Off", "Slickshot Show-Off", "Emberheart Challenger"],
            target_names=["Opponent"],
        )
    ]
)


def test_a_planner_attack_moments_old_is_declared(engine):
    engine.note_advised_plan(ADVICE, combat_state())
    submitted, detail = engine.watchdog_safe_default(combat_state(), reason="20s left on Arena's clock")
    assert submitted is True
    assert detail == (
        "declared the planner's attack with Slickshot Show-Off, Slickshot Show-Off, Emberheart Challenger"
    )
    assert engine.declared[0].attacker_names == ADVICE.actions[0].attacker_names


def test_a_stale_or_other_turn_plan_is_not_followed(engine):
    engine.note_advised_plan(ADVICE, combat_state(turn=6))
    engine.watchdog_safe_default(combat_state())
    engine.note_advised_plan(ADVICE, combat_state())
    engine._advised_attack = (time.monotonic() - 3.0, *engine._advised_attack[1:])
    engine.watchdog_safe_default(combat_state())
    # Both times: the solver's attack plus the free fliers, not the advice.
    assert [sorted(action.attacker_instance_ids) for action in engine.declared] == [[292, 305, 324]] * 2


def test_no_untapped_blocker_sends_the_fliers_with_the_solver_attack(engine):
    submitted, detail = engine.watchdog_safe_default(combat_state())
    assert submitted is True
    action = engine.declared[0]
    assert sorted(action.attacker_instance_ids) == [292, 305, 324]
    assert set(action.attacker_targets.values()) == {"Opponent"}
    assert detail.startswith("attacked with ")


def test_an_untapped_blocker_leaves_the_usual_safe_default(engine):
    submitted, detail = engine.watchdog_safe_default(combat_state(trostani_tapped=False))
    assert (submitted, detail) == (True, "submitted the safe default")
    assert engine.declared == ["usual safe default"]


def test_a_planner_attack_that_fails_leaves_the_usual_safe_default(engine):
    # It may have reached Arena in part: only the usual default re-checks the request.
    engine.note_advised_plan(ADVICE, combat_state())
    engine._try_bridge_declare_attackers = lambda action: engine.declared.append(action) or None
    assert engine.watchdog_safe_default(combat_state()) == (True, "submitted the safe default")
    assert engine.declared[1:] == ["usual safe default"]


def test_a_forced_attacker_leaves_the_usual_safe_default(engine):
    forced = {**ATTACK_POLL, "attackers": [dict(entry) for entry in ATTACK_POLL["attackers"]]}
    forced["attackers"][2]["mustAttack"] = True
    engine._gre_bridge.get_pending_actions.return_value = forced
    assert engine.watchdog_safe_default(combat_state()) == (True, "submitted the safe default")
    assert engine.declared == ["usual safe default"]
