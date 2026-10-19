"""A lethal Sneak line attacks with everything (ledger #189, match 2149ea96, 2026-10-19 16:22-16:26).

Brawl_Ladder with Leonardo, Big Brother. The Sneak guard kept {4}{W} open for "attack with
everything, then Sneak Leonardo after blocks: 39 against their 21", and the typed decision saw
"up to 121 damage this turn (attackers 66, Sneak Leonardo after blocks +55) against their 21
life". But with 28-57 attackers on the menu the planner left the declaration to the combat
solver ("Big board"), which declared no attackers, three turns running; the held mana went
unused and the game was lost on turn 28.

Board: ``tests/leonardo_boards_20261019.py`` T12 (match be1a945f, replayed from Player.log),
moved to our declare-attackers step.
"""

from __future__ import annotations

from tests import leonardo_boards_20261019 as lb

from arenamcp import sneak
from arenamcp.autopilot_bridge import _BridgeSubmitMixin
from arenamcp.board_assessment import assess


def _declare_step() -> dict:
    state = lb.t12_main1()
    state["turn"].update({"phase": "Phase_Combat", "step": "Step_DeclareAttack"})
    ours = [
        c["instance_id"]
        for c in state["battlefield"]
        if c.get("controller_seat_id", c.get("owner_seat_id")) == lb.LOCAL
        and c["name"] in ("Hare Apparent", "Rabbit")
        and not c.get("is_tapped")
    ]
    state["decision_context"] = {"type": "declare_attackers", "legal_attacker_ids": ours}
    return state


def test_the_sneak_hold_reads_the_declare_attackers_step_only_when_asked():
    state = _declare_step()
    assert sneak.sneak_main_hold(state) is None  # the main-phase hold is unchanged
    hold = sneak.sneak_main_hold(state, declare_step=True)
    assert hold is not None and hold.lethal and hold.through < hold.life
    assert hold.cast.name == "Leonardo, Big Brother"


def test_a_lethal_sneak_line_declares_every_legal_attacker():
    state = _declare_step()
    assert not assess(state).all_in  # the all-in rule alone would not attack
    action = _BridgeSubmitMixin._all_in_attack_action(None, state, [])
    assert action is not None
    assert len(action.attacker_names) == len(state["decision_context"]["legal_attacker_ids"])
    assert action.target_names == ["Opponent"]
    assert "Sneak line is lethal" in action.reasoning and "Leonardo, Big Brother" in action.reasoning


def test_no_sneak_all_in_when_the_line_is_not_lethal():
    state = _declare_step()
    state["players"][0]["life_total"] = 60  # their attack no longer kills us: no all-in either
    assert sneak.sneak_main_hold(state, declare_step=True) is not None
    assert _BridgeSubmitMixin._all_in_attack_action(None, state, []) is not None
    state["players"][1]["life_total"] = 30
    assert sneak.sneak_main_hold(state, declare_step=True) is None
    assert _BridgeSubmitMixin._all_in_attack_action(None, state, []) is None
    state = _declare_step()
    state["players"][0]["life_total"] = 60
    state["command"] = []  # no Leonardo to Sneak
    assert _BridgeSubmitMixin._all_in_attack_action(None, state, []) is None


def test_the_losing_attack_guard_keeps_the_attack_a_lethal_sneak_follows(monkeypatch):
    """At 16:20:41 and 16:21:51 the model planned the swing ("attackers 20 + Sneak Leonardo,
    Big Brother 14 against their 21 life") and this guard held all fifteen attackers one by
    one: each alone fed Thalia, the Survivor 3/4 lifelink."""
    from tests.test_combat_guards_20261019 import STRATEGY, lyra_board

    from arenamcp.combat_strategy import losing_attackers

    assert set(losing_attackers(lyra_board(), [368], strategy_text=STRATEGY)) == {368}
    calls = []

    def lethal_hold(state, *, declare_step=False):
        calls.append(declare_step)
        return object()

    monkeypatch.setattr(sneak, "sneak_main_hold", lethal_hold)
    assert losing_attackers(lyra_board(), [368], strategy_text=STRATEGY) == {}
    assert calls == [True]
