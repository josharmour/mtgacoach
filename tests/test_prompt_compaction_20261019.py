"""Token-swarm decision prompts are compact, and a big prompt's first token may come later (ledger #182).

Match 41b07b15 (2026-10-19 15:40-15:55, Leonardo, Big Brother Brawl vs Myrel's
Soldiers), standalone.log: decision prompts of 38-119K characters on the shared
GPUs (prefill ~1-1.5K tokens/s). 15:47:09 decision.plan 43,108 chars / 11,623
tokens, first token at 7.87 s; 15:48:58 decision.typed 38,424 chars, both the
high and the low leg "no first token within 8.0s (FirstTokenTimeout)", the
backend marked degraded and the board-math line cast the turn; 15:54:35
decision.plan 118,957 chars, the low leg timed out. Three quarters of such a
prompt was the "If X blocks Y" trade matrix: one line per attacker and
blocker pair (15 attackers into 33 untapped blockers: 528 lines).

The boards are the match's own Player.log replayed through LogParser +
GameState (``server.get_game_state``), trimmed to the planner fields:

- ``swarm_41b07b15_t19_main1_20261019.json``: T19 main 1 at 15:48:50, the
  typed decision that timed out (Cast Leonardo {8}{W} unpayable, Cast Hare
  Apparent, Pass); 10 creatures of ours, 30 of theirs (23 Soldier tokens).
- ``swarm_41b07b15_t23_attack_20261019.json``: T23 DeclareAttackers at
  15:54:22 (after an MTGA relaunch): 15 attackers on Arena's menu (Human, two
  Hare Apparents, twelve Rabbits) against 33 untapped blockers.

Measured on these boards (without the deck reference): the typed prompt
31,569 -> 12,852 characters, the plan prompt 60,277 -> 25,095.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.backends.proxy import FIRST_TOKEN_CAP_S, estimated_tokens, first_token_budget
from arenamcp.board_compaction import group_counted, group_labels, group_trades, number_ranges
from arenamcp.coach import CoachEngine
from arenamcp.decisions import build_pending_decision

FIXTURES = Path(__file__).parent / "fixtures"
T19 = FIXTURES / "swarm_41b07b15_t19_main1_20261019.json"
T23 = FIXTURES / "swarm_41b07b15_t23_attack_20261019.json"
TRADE = re.compile(
    r"^  If (?P<blk>.+?)(?: \(any of \d+\))? blocks (?P<atk>.+?)(?: \(any of \d+\))?: (?P<result>.+)$"
)


def _state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _context(state: dict[str, Any]) -> str:
    return CoachEngine.__new__(CoachEngine)._format_game_context(state, for_planner=True)


class _Backend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def complete(
        self, system_prompt, user_message, max_tokens=4096, temperature=0.3, request_timeout_s=None,
        raise_on_error=False, *, call_class=None, first_token_timeout_s=None, reasoning_effort=None,
        cancel_event=None,
    ):  # fmt: skip
        self.calls.append(
            {
                "class": call_class,
                "effort": reasoning_effort,
                "chars": len(system_prompt) + len(user_message),
                "first_token": first_token_timeout_s,
                "budget": request_timeout_s,
                "user": user_message,
            }
        )
        if call_class == "decision.plan":
            return '{"actions": [{"action_type": "pass_priority"}]}'
        return '{"option_ids": ["pass"], "reasoning": "hold"}'


def _t19_decision(state: dict[str, Any]):
    names = {c.get("grp_id"): c.get("name") for c in state["battlefield"] + state["hand"] + state["command"]}
    actions = [{k: v for k, v in a.items() if not k.startswith("_")} for a in state["legal_actions_raw"]]
    for action in actions:
        if action.get("actionType") == "ActionType_Cast":
            action["hasAutoTap"] = bool(action.get("autoTapSolution"))
    poll = {"has_pending": True, "request_type": "ActionsAvailable", "can_pass": True, "actions": actions}
    return build_pending_decision(poll, resolve_name=lambda grp: names.get(grp, ""))


# --- the helpers ----------------------------------------------------------------------------------


def test_number_ranges_and_labels():
    assert number_ranges([5, 1, 2, 3]) == "#1..#3, #5"
    assert number_ranges([7]) == "#7"
    labels = [f"Rabbit #{i}" for i in range(1, 13)] + ["Human", "Hare Apparent #1", "Hare Apparent #2"]
    assert group_labels(labels) == ["Rabbit #1..#12", "Human", "Hare Apparent #1", "Hare Apparent #2"]
    # Twins must match in everything after the number.
    mixed = ["Soldier #1 1/1", "Soldier #2 1/1", "Soldier #3 2/2 (DTH)", "Soldier #4 1/1"]
    assert group_labels(mixed) == ["Soldier #1..#2/#4 1/1", "Soldier #3 2/2 (DTH)"]
    assert group_counted(["Soldier 1/1"] * 23 + ["Myrel 3/4"]) == ["Soldier 1/1 ×23", "Myrel 3/4"]


def test_trades_name_how_many_cards_a_line_stands_for():
    entries = [
        ("Soldier 1/1", b, "Rabbit 1/1", a, "TRADE (both die)") for b in range(23) for a in range(100, 112)
    ]
    entries.append(("Myrel 3/4", 1, "Rabbit 1/1", 100, "BAD — Rabbit dies"))
    assert group_trades(entries) == [
        "  If Soldier 1/1 (any of 23) blocks Rabbit 1/1 (any of 12): TRADE (both die)",
        "  If Myrel 3/4 blocks Rabbit 1/1: BAD — Rabbit dies",
    ]
    # Only some of the pairings: the line says how many it covers.
    partial = [("Soldier 1/1", 1, "Rabbit 1/1", 100, "x"), ("Soldier 1/1", 2, "Rabbit 1/1", 101, "x")]
    assert group_trades(partial) == ["  If Soldier 1/1 blocks Rabbit 1/1: x [2 such pairs]"]


# --- the real boards ------------------------------------------------------------------------------


def test_the_t23_attack_matrix_keeps_every_distinct_trade_once():
    state = _state(T23)
    context = _context(state)
    lines = context.split("\n")
    trades = [line for line in lines if TRADE.match(line)]
    # 528 pair lines before (15 attackers x 33 untapped blockers, flying aside); now one per distinct fight.
    assert 10 <= len(trades) <= 30
    assert len(trades) == len(set(trades))
    assert "  If Soldier 1/1 (any of 23) blocks Rabbit 1/1 (any of 12): TRADE (both die)" in trades
    assert (
        "  If Mondrak, Glory Dominus 4/4 blocks Hare Apparent 2/2 (any of 3): BAD — Hare Apparent dies, "
        "Mondrak, Glory Dominus lives (2 left)" in trades
    )
    # Every fight the old matrix described is still there, with the same result.
    engine = CoachEngine.__new__(CoachEngine)
    local = state["local_seat_id"]
    ctx_ids = set(state["decision_context"]["legal_attacker_ids"])
    ours = [c for c in state["battlefield"] if c["instance_id"] in ctx_ids]
    theirs = [
        c for c in state["battlefield"]
        if c["controller_seat_id"] != local and engine._is_creature_now(c) and not c.get("is_tapped")
    ]  # fmt: skip
    shown = {(m["blk"], m["atk"]): m["result"] for line in trades if (m := TRADE.match(line))}
    expected = set()
    for atk in ours:
        for blk in theirs:
            trade = engine._compute_combat_trade(atk, blk)
            if trade is not None:
                expected.add(
                    (
                        f"{blk['name']} {blk['power']}/{blk['toughness']}",
                        f"{atk['name']} {atk['power']}/{atk['toughness']}",
                    )
                )
    assert expected and expected <= set(shown)


def test_identical_permanents_share_a_board_line_and_different_ones_do_not():
    context = _context(_state(T23))
    assert "  *Soldier ×23 (#1..#23) 1/1 — dies to 1 damage" in context
    assert "  *Rabbit ×12 (#1..#12) 1/1" in context
    assert "*Soldier #" not in context and "*Rabbit #" not in context
    # Untapped and tapped Plains stay apart; so does the damaged Hare and the tapped Herald.
    assert re.search(r"^  Plains ×3 \(#1\.\.#3\)$", context, re.M)
    assert re.search(r"^  Plains ×5 \(#4\.\.#8\) \[T\]$", context, re.M)
    assert re.search(r"^  Hare Apparent #2 2/2 — 1 damage marked; dies to 1 damage$", context, re.M)
    assert "Herald of Anafenza 4/5 (3P1P1) [T]" in context
    # The blocker list counts identical bodies.
    assert "Soldier 1/1 ×23, Warrior 1/1 ×5" in context
    assert "can attack: Hare Apparent #1..#3, Human, Rabbit #1..#12" in context


def test_a_card_that_differs_in_one_fact_keeps_its_own_line():
    state = _state(T23)
    rabbits = [c for c in state["battlefield"] if c["name"] == "Rabbit"]
    rabbits[3]["counters"] = {"CounterType_P1P1": 1}
    rabbits[3].update(power=2, toughness=2)
    rabbits[5]["is_tapped"] = True
    context = _context(state)
    assert "  *Rabbit ×10 (#1..#3, #5, #7..#12) 1/1" in context
    assert re.search(r"^  \*Rabbit #4 2/2 \(1P1P1\)", context, re.M)
    assert re.search(r"^  \*Rabbit #6 1/1 \[T\]", context, re.M)


def test_the_block_side_groups_attackers_and_fights_too():
    state = _state(T23)
    local = state["local_seat_id"]
    opponent = state["opponent_seat_id"]
    state["turn"].update(active_player=opponent, priority_player=local, step="Step_DeclareBlock")
    soldiers = [c for c in state["battlefield"] if c["name"] == "Soldier"][:10]
    for soldier in soldiers:
        soldier["is_attacking"] = True
    attacker_ids = [s["instance_id"] for s in soldiers]
    state["decision_context"] = {"type": "declare_blockers", "attacker_ids": attacker_ids}
    context = _context(state)
    assert "Attackers: Soldier #1..#10 1/1" in context
    trades = [line for line in context.split("\n") if TRADE.match(line)]
    assert len(trades) == len(set(trades)) and len(trades) <= 6
    assert "  If Rabbit 1/1 (any of 12) blocks Soldier 1/1 (any of 10): TRADE (both die)" in trades


def test_the_real_prompts_shrink():
    backend = _Backend()
    planner = ActionPlanner(backend, timeout=15.0, land_drop_first=False)
    state = _state(T19)
    planner.plan_decision_options(_t19_decision(state), state)
    typed = backend.calls[-1]
    assert typed["class"] == "decision.typed"
    assert typed["chars"] < 16_000  # 31,569 before
    state = _state(T23)
    planner.plan_actions(state, "decision_required", state["legal_actions"], state["decision_context"])
    plan = backend.calls[-1]
    assert plan["class"] == "decision.plan"
    assert plan["chars"] < 30_000  # 60,277 before
    # The decision context is one compact JSON line, not ~100 indented ones.
    decision_line = next(line for line in plan["user"].split("\n") if line.startswith("Decision: "))
    assert json.loads(decision_line[len("Decision: ") :])["legal_attacker_ids"][0] == 466


# --- the first-token budget -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("chars", "expected"),
    [
        (12_000, 8.0),  # ordinary prompts: the saturation limit stays
        (int(6000 * 3.4), 8.0),
        (39_440, 8.0 + 0.5 * (estimated_tokens(39_440) - 6000) / 1000),  # 15:48:58's 38K chars: ~10.8 s
        (118_957, FIRST_TOKEN_CAP_S),  # 15:54:35's prompt
    ],
)
def test_the_first_token_limit_grows_with_the_prompt(chars, expected):
    assert first_token_budget(8.0, chars) == pytest.approx(expected)


def test_the_first_token_limit_never_drops_below_the_callers():
    assert 10.5 < first_token_budget(8.0, 39_440) < 11.0
    assert first_token_budget(12.0, 30_000) == 12.0
    assert first_token_budget(8.0, 1_000_000) == FIRST_TOKEN_CAP_S == 15.0


def test_a_big_typed_prompt_gets_the_later_first_token_and_keeps_its_answer_time():
    state = _state(T19)
    state["deck_reference"] = "DECK REFERENCE\n" + "x" * 50_000  # a prompt as big as the live ones
    backend = _Backend()
    planner = ActionPlanner(backend, timeout=15.0, land_drop_first=False)
    planner.plan_decision_options(_t19_decision(state), state)
    call = backend.calls[-1]
    chars = call["chars"]
    first = first_token_budget(ActionPlanner._FIRST_TOKEN_TIMEOUT_S, chars)
    assert first > ActionPlanner._FIRST_TOKEN_TIMEOUT_S
    assert call["first_token"] == pytest.approx(first)
    # The leg's budget grows by as much as its first token may come later (at most 18 s).
    assert call["budget"] == pytest.approx(min(18.0, 12.0 + first - 8.0), abs=0.1)
    assert call["budget"] - call["first_token"] >= 3.0
    # An ordinary prompt keeps today's 8 s / 12 s.
    backend.calls.clear()
    small = _state(T19)
    planner.plan_decision_options(_t19_decision(small), small)
    assert (backend.calls[-1]["first_token"], backend.calls[-1]["budget"]) == (8.0, 12.0)


def test_a_big_plan_prompt_waits_longer_but_inside_the_planner_budget():
    planner = ActionPlanner(_Backend(), timeout=15.0, land_drop_first=False)
    assert planner._plan_first_token_timeout() == 12.0
    # ~17.6K tokens would wait 13.8 s; it stays 2 s inside the 15 s planner budget.
    assert planner._plan_first_token_timeout(60_000) == 13.0
    assert planner._plan_first_token_timeout(40_000) == 12.0  # ~11.8K tokens: 10.9 s, below the plan's 12
    assert ActionPlanner(_Backend(), timeout=20.0)._plan_first_token_timeout(60_000) == pytest.approx(
        first_token_budget(12.0, 60_000)
    )
    assert ActionPlanner(_Backend(), timeout=9.0)._plan_first_token_timeout(60_000) == 8.0


def test_sized_budgets_never_shrink_and_are_capped():
    planner = ActionPlanner(_Backend(), timeout=15.0, land_drop_first=False)
    assert planner._sized_budget(12.0, 8.0) == 12.0
    assert planner._sized_budget(1.0, 8.0) == 1.0  # a short configured budget is kept for small prompts
    assert planner._sized_budget(12.0, 15.0) == 18.0
    assert planner._sized_budget(15.0, 15.0) == 18.0
    assert planner._sized_budget(20.0, 15.0) == 20.0


def test_no_battlefield_line_is_repeated_on_the_real_boards():
    for path in (T19, T23):
        lines = _context(_state(path)).split("\n")
        start = lines.index("YOUR BOARD:")
        opp = lines.index("OPP BOARD:")
        end = next(i for i in range(opp + 1, len(lines)) if not lines[i].startswith(" "))
        board = [line for line in lines[start:end] if line.startswith("  ")]
        repeated = [line for line, count in Counter(board).items() if count > 1]
        assert repeated == [], (path.name, repeated[:5])
        assert len(board) < 40, path.name  # 60-80 card lines before
