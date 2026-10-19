"""A six-card keep keeps three lands, as the bottom prompt says (ledger #101, match 18b1601a, 2026-10-19).

standalone.log 12:02:50-12:02:53: the seven after one mulligan were Slickshot
Show-Off x2, Burst Lightning, Iroh's Demonstration and three Mountains. The keep
guard said "keeping 6 with 2 lands", the bottom prompt said "Keep lands toward
2-3 (3 when keeping 6)" next to "Land/curve suggestion: bottom Mountain", and
the model bottomed the third Mountain; the 20-land deck sat on three lands
T5-T9. The policy now values the third land of a six-card keep above any spell.
"""

from __future__ import annotations

import json
import logging

from tests.test_mulligan_bottom_roles_20261019 import BURST, MOUNTAIN, SLICKSHOT, _bottom_decision, _state

from arenamcp import mulligan_policy as mp
from arenamcp.action_planner import ActionPlanner

IROH = (
    "Iroh's Demonstration",
    "Sorcery — Lesson",
    "{1}{R}",
    "uncommon",
    "Choose one —\n• Iroh's Demonstration deals 1 damage to each creature your opponents control.\n"
    "• Iroh's Demonstration deals 4 damage to target creature.",
)
# The seven shown after the mulligan, in Arena's order (instance ids 280-286).
HAND_18B1601A = [SLICKSHOT, BURST, IROH, MOUNTAIN, MOUNTAIN, MOUNTAIN, SLICKSHOT]


def _names(state, ids) -> list[str]:
    by_id = {card["instance_id"]: card["name"] for card in state["hand"]}
    return [by_id[i] for i in ids]


def test_keeping_six_keeps_the_third_land():
    state = _state(HAND_18B1601A)
    ids = [card["instance_id"] for card in state["hand"]]
    bottom = mp.bottom_choice(state, ids, 1)
    assert _names(state, bottom) == ["Iroh's Demonstration"]
    assert mp.kept_land_count(state, ids, bottom) == 3
    choice, reason = mp.mulligan_verdict(mp.situation(state))
    assert choice == "keep" and "keeping 6 with 3 lands" in reason, reason


def test_the_bottom_prompt_and_its_suggestion_agree(caplog):
    state = _state(HAND_18B1601A)
    mountain = next(f"grp:{card['instance_id']}" for card in state["hand"] if card["name"] == "Mountain")

    class Backend:
        prompts: list[str] = []

        def complete(self, system, user, *args, **kwargs):
            self.prompts.append(user)
            return json.dumps({"option_ids": [mountain], "reasoning": "the third Mountain is redundant"})

    backend = Backend()
    planner = ActionPlanner(backend, timeout=5, land_drop_first=False)
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        chosen = planner.plan_decision_options(_bottom_decision(state), state)
    prompt = backend.prompts[0]
    assert "(3 when keeping 6)" in prompt
    assert "Land/curve suggestion: bottom Iroh's Demonstration." in prompt
    # The model's 12:02:53 answer (the third Mountain) is clearly worse: the guard replaces it.
    assert _names(state, [int(c[4:]) for c in chosen]) == ["Iroh's Demonstration"]
    assert "Mulligan bottom guard" in caplog.text


def test_two_lands_in_the_seven_still_keep_both_and_five_card_keeps_are_unchanged():
    two = _state([SLICKSHOT, BURST, IROH, MOUNTAIN, SLICKSHOT, MOUNTAIN, BURST])
    ids = [card["instance_id"] for card in two["hand"]]
    assert mp.kept_land_count(two, ids, mp.bottom_choice(two, ids, 1)) == 2
    # Keeping 5 of the 18b1601a seven: lands toward 2-3, the old values.
    five = _state(HAND_18B1601A, mulligans=2)
    ids = [card["instance_id"] for card in five["hand"]]
    assert mp.kept_land_count(five, ids, mp.bottom_choice(five, ids, 2)) == 2
