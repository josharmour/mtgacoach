"""Reanimation whose text qualifies the card before naming the graveyard.

2026-10-12 23:52:36-44 (bug_20261012_235251, turn 14): Rewrite Regrets ("return
target creature or planeswalker card with mana value 6 or less from your
graveyard to the battlefield") targeted our Apex Witchstalker (tgt:270) as the
model chose, but the recursion rule wanted "card from your graveyard" side by
side, so the text read as a bounce: "Overriding harmful LLM target pick
['tgt:270'] (own permanent) with ['__decline__']", and the self-cancel guard
then hid the spell for the rest of the turn.
"""

from __future__ import annotations

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision, TargetSlot
from arenamcp.target_effects import target_effect_is_harmful

ME, OPP = 1, 2
REWRITE_REGRETS = (
    "Return target creature or planeswalker card with mana value 6 or less from your graveyard to the "
    "battlefield."
)


class _Backend:
    def __init__(self, reply: str):
        self.reply = reply

    def complete(self, system, user, *args, **kwargs):
        return self.reply


def _planner(reply: str) -> ActionPlanner:
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._timeout = 1.0
    planner._backend = _Backend(reply)
    return planner


def _grave(iid: int, name: str, mana_cost: str) -> dict:
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Creature",
        "mana_cost": mana_cost,
        "owner_seat_id": ME,
        "controller_seat_id": ME,
    }


def _state() -> dict:
    return {
        "match_id": "reanimate",
        "local_seat_id": ME,
        "turn": {"turn_number": 14, "active_player": ME, "priority_player": ME, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": ME, "is_local": True, "life_total": 9},
            {"seat_id": OPP, "is_local": False, "life_total": 18},
        ],
        "battlefield": [],
        "hand": [],
        "graveyard": [
            _grave(270, "Apex Witchstalker", "{4}{B}{B}"),
            _grave(329, "Grim Repriser", "{B/R}{B/R}"),
            _grave(330, "Theorix Metamage", "{2}{U/B}"),
        ],
        "stack": [
            {
                "instance_id": 363,
                "name": "Rewrite Regrets",
                "type_line": "Sorcery",
                "oracle_text": REWRITE_REGRETS,
                "controller_seat_id": ME,
            }
        ],
        "exile": [],
        "command": [],
        "_bridge_request_payload": {"sourceId": 363},
        "decision_context": {"type": "target_selection", "source_id": 363, "source_card": "Rewrite Regrets"},
    }


def _decision() -> PendingDecision:
    ids = (270, 329, 330)
    return PendingDecision(
        request_id=(317, 433),
        request_type="SelectTargets",
        options=tuple(DecisionOption(f"tgt:{iid}", f"Target #{iid}") for iid in ids),
        min_select=1,
        max_select=1,
        can_cancel=True,
        source_label="Rewrite Regrets",
        slots=(TargetSlot(1, 1, 1, 0, ids),),
    )


def test_qualified_reanimation_reads_beneficial_and_bounce_stays_harmful():
    assert target_effect_is_harmful(REWRITE_REGRETS) is False
    assert (
        target_effect_is_harmful(
            "Put target creature card with mana value 3 or less from a graveyard onto the battlefield under "
            "your control."
        )
        is False
    )
    assert target_effect_is_harmful("Return target creature to its owner's hand.") is True
    assert target_effect_is_harmful("Exile target creature card from a graveyard.") is True


def test_the_live_pick_of_our_graveyard_card_is_submitted():
    planner = _planner('{"option_ids": ["tgt:270"], "reasoning": "reanimate the 6/4"}')
    assert planner.plan_decision_options(_decision(), _state()) == ["tgt:270"]
