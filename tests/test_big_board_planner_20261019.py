"""Declarations on big Brawl boards and their effort (ledger #155, #159, #162).

- #155 (match 2bb162d7, 14:27:34 and 14:29:23): the model named all 25 / 39
  attackers ("Hare Apparent [id:442]", "*Rabbit [id:470]", ...) but the
  declaration was dropped as illegal ("declare_attackers () not in [...]"):
  its attacker_targets did not key the attackers the same way. The answer is
  now repaired before it is checked, and "all" / "all except X" expand to the
  menu.
- #162: on those boards the block plan ran out its 15 s and the solver's safe
  default decided anyway; 20+ attackers or 36+ creatures in a block window now
  go to the solver first. Identical tokens show as one menu line.
- #159 (match be1a945f T10): declarations ran at low effort with 25 power
  against their 12 life.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from tests import leonardo_boards_20261019 as lb

from arenamcp.action_legality import normalize_attack_declaration
from arenamcp.action_planner import FALLBACK_BIG_BOARD, ActionPlanner, ActionType, GameAction
from arenamcp.big_board import big_board_declaration, compress_menu, declaration_effort, group_menu

# Player.log / coach log 14:27:34 (match 2bb162d7 T13): the menu the dropped declaration had.
MENU_2BB = [
    "Attack with: Hare Apparent #1 (2/2)", "Attack with: Hare Apparent #2 (2/2)",
    "Attack with: Hare Apparent #3 (2/2)", "Attack with: Rabbit #1 (1/1)",
    "Attack with: Leonardo, Big Brother (42/3)", "Attack with: Hare Apparent #4 (2/2)",
    *[f"Attack with: Rabbit #{i} (1/1)" for i in (2, 3, 4)], "Attack with: Hare Apparent #5 (2/2)",
    *[f"Attack with: Rabbit #{i} (1/1)" for i in (5, 6, 7, 8)], "Attack with: Hare Apparent #6 (2/2)",
    *[f"Attack with: Rabbit #{i} (1/1)" for i in (9, 10, 11)], "Attack with: Hare Apparent #7 (2/2)",
    *[f"Attack with: Rabbit #{i} (1/1)" for i in range(12, 18)], "Done (confirm attackers)",
]  # fmt: skip

# be1a945f T12 (14:17:57): fifteen attackers, the menu in Arena's id order.
T12_IDS = [744, 757, 761, 763, 767, 768, 791, 795, 796, 797, 798, 802, 803, 804, 805]


def _t12_menu() -> list[str]:
    hares = rabbits = 0
    menu = []
    for iid in T12_IDS:
        if iid in lb.HARES_T12:
            hares += 1
            menu.append(f"Attack with: Hare Apparent #{hares} (2/2)")
        else:
            rabbits += 1
            menu.append(f"Attack with: Rabbit #{rabbits} (1/1)")
    return [*menu, "Done (confirm attackers)"]


def _t12_attack_state() -> dict[str, Any]:
    state = lb.t12_main1()
    state["turn"].update(phase="Phase_Combat", step="Step_DeclareAttack")
    return state


class _Backend:
    def __init__(self, answers: dict[str | None, Any]) -> None:
        self.answers = answers
        self.calls: list[str | None] = []
        self.lock = threading.Lock()

    def complete(self, system: str, user: str, max_tokens: int = 0, *, reasoning_effort=None, **_kw) -> str:
        with self.lock:
            self.calls.append(reasoning_effort)
        answer = self.answers.get(reasoning_effort)
        if isinstance(answer, Exception):
            raise answer
        return answer


# --- #162: the solver first on big boards -------------------------------------------------------


def test_twenty_five_attackers_go_to_the_solver_without_a_model_call():
    assert big_board_declaration({"battlefield": []}, MENU_2BB) == "25 attackers on the menu"
    assert big_board_declaration(_t12_attack_state(), _t12_menu()) == ""  # 15: the model answered well
    backend = _Backend({})
    planner = ActionPlanner(backend, timeout=1.0, land_drop_first=False)
    state = _t12_attack_state()
    plan = planner.plan_actions(state, "decision_required", MENU_2BB, {"type": "declare_attackers"})
    assert plan.fallback_reason == FALLBACK_BIG_BOARD and not plan.actions
    assert backend.calls == []


def test_a_block_window_with_thirty_six_creatures_goes_to_the_solver():
    state = lb.t12_declare_blockers()
    state["battlefield"] += [lb.card(900 + i, "Rabbit", lb.LOCAL) for i in range(20)]
    assert big_board_declaration(state, ["Block with: Rabbit #1", "Done (no blocks)"]).endswith(
        "creatures on the battlefield"
    )


def test_identical_tokens_are_one_menu_line_with_the_numbering_kept():
    menu = group_menu(_t12_menu())
    assert menu[:2] == ["Attack with: Hare Apparent #1 (2/2)", "Attack with: Hare Apparent #2 (2/2)"]
    assert sorted(menu) == sorted(_t12_menu())
    lines = compress_menu(menu)
    assert lines == [
        "  1-5. Attack with: Hare Apparent #1..#5 (2/2): 5 identical",
        "  6-15. Attack with: Rabbit #1..#10 (1/1): 10 identical",
        "  16. Done (confirm attackers)",
    ]


# --- #155: the declaration's shape ----------------------------------------------------------------


def test_targets_keyed_without_the_decoration_are_matched():
    action = GameAction(
        action_type=ActionType.DECLARE_ATTACKERS,
        attacker_names=["Hare Apparent [id:744]", "*Rabbit [id:761]", "Rabbit [id:767]"],
        attacker_targets={"Hare Apparent": "Opponent", "Rabbit": "Opponent", "Ugin": "Opponent"},
    )
    assert normalize_attack_declaration(action, _t12_menu())
    assert action.attacker_targets == {name: "Opponent" for name in action.attacker_names}


def test_all_and_all_except_expand_to_the_menu_by_id():
    action = GameAction(
        action_type=ActionType.DECLARE_ATTACKERS, attacker_names=["all"], attacker_targets={"all": "Opponent"}
    )
    normalize_attack_declaration(action, _t12_menu(), T12_IDS)
    assert len(action.attacker_names) == 15 and action.attacker_names[0] == "Hare Apparent [id:744]"
    assert set(action.attacker_targets.values()) == {"Opponent"}
    action = GameAction(action_type=ActionType.DECLARE_ATTACKERS, attacker_names="all except Rabbit")
    normalize_attack_declaration(action, _t12_menu(), T12_IDS)
    assert [name.split(" [")[0] for name in action.attacker_names] == ["Hare Apparent"] * 5
    assert action.attacker_targets == {}


def test_the_planner_accepts_the_shapes_it_used_to_drop():
    state = _t12_attack_state()
    context = {"type": "declare_attackers", "legal_attacker_ids": T12_IDS}
    planner = ActionPlanner(_Backend({}), timeout=1.0, land_drop_first=False)
    names = [f"Hare Apparent [id:{i}]" if i in lb.HARES_T12 else f"*Rabbit [id:{i}]" for i in T12_IDS]
    for answer in (
        {"action_type": "declare_attackers", "attacker_names": ["all"], "attacker_targets": {"all": "Opponent"}},
        {"action_type": "declare_attackers", "attacker_names": names,
         "attacker_targets": {"Hare Apparent": "Opponent", "Rabbit": "Opponent"}},
        {"action_type": "declare_attackers", "attacker_names": names, "attacker_targets": "Opponent"},
    ):  # fmt: skip
        plan = planner._parse_response(
            json.dumps({"actions": [answer]}), _t12_menu(), decision_context=context,
            bridge_request="DeclareAttackers", game_state=state,
        )  # fmt: skip
        assert len(plan.actions) == 1, answer
        assert sorted(plan.actions[0].attacker_instance_ids) == sorted(T12_IDS)


# --- #159: effort ---------------------------------------------------------------------------------


def test_lethal_reach_or_danger_raises_a_declarations_effort():
    menu = [
        "Attack with: Leonardo, Big Brother (16/3)", *[f"Attack with: Hare Apparent #{i} (2/2)" for i in (1, 2, 3)],
        *[f"Attack with: Rabbit #{i} (1/1)" for i in (1, 2, 3)],
    ]  # fmt: skip
    effort, why = declaration_effort(lb.t10_declare_attackers(), menu)
    assert (effort, why) == ("high", "within lethal reach: attackers 25 against their 12 life")
    # T12 with their life at 25: 20 attacking power, but a Sneak Leonardo (15) for a Rabbit reaches it.
    state = _t12_attack_state()
    state["players"][1]["life_total"] = 25
    effort, why = declaration_effort(state, _t12_menu())
    assert effort == "high" and "Sneak Leonardo, Big Brother 14" in why
    state["players"][1]["life_total"] = 60
    # Their 24 power against our 17 life is danger enough on its own.
    assert declaration_effort(state, _t12_menu()) == (
        "high",
        "their creatures hit for 24 against our 17 life",
    )
    state["players"][0]["life_total"] = 30
    assert declaration_effort(state, _t12_menu()) == ("", "")


def test_blocks_in_danger_run_at_high_effort():
    state = lb.t12_declare_blockers()
    state["turn"]["active_player"] = lb.OPPONENT
    for card in state["battlefield"]:
        card["is_attacking"] = (
            card["controller_seat_id"] == lb.OPPONENT and card["name"] != "Dragonlord's Servant"
        )
    effort, why = declaration_effort(state, ["Block with: Rabbit #1", "Done (no blocks)"])
    assert effort == "high" and why.endswith("against our 17 life")


def test_a_failed_high_effort_declaration_falls_back_to_the_low_answer():
    planner = ActionPlanner(_Backend({}), timeout=2.0, land_drop_first=False)
    backend = _Backend({"high": TimeoutError("budget"), None: '{"actions": []}'})
    planner._backend = backend
    answer = planner._hedged_declaration(
        lambda call_class, effort=None: backend.complete("", "", reasoning_effort=effort)
    )
    assert answer == '{"actions": []}' and sorted(map(str, backend.calls)) == ["None", "high"]
