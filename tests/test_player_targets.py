"""Effects that target a player first ("Target opponent sacrifices ...").

2026-10-12 23:42:33-49 (bug_20261012_234250, turn 9): Break Under Pressure's
only target was the opponent (tgt:1, seat 1). The model chose it three times;
the target gate read the text as unclassified ("Target opponent sacrifices"
puts the target before the verb), logged "Declining unclassified target tgt:1
without explicit controller intent" and cancelled the cast each time, until the
user picked the opponent by hand.
"""

from __future__ import annotations

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision, TargetSlot
from arenamcp.target_effects import target_effect_is_harmful

OPP, ME = 1, 2
BREAK_UNDER_PRESSURE = (
    "Target opponent sacrifices a creature or planeswalker with the greatest mana value among creatures "
    "and planeswalkers they control. You gain 2 life."
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


def _state(oracle: str = BREAK_UNDER_PRESSURE, name: str = "Break Under Pressure") -> dict:
    return {
        "match_id": "player-targets",
        "local_seat_id": ME,
        "turn": {"turn_number": 9, "active_player": ME, "priority_player": ME, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": OPP, "is_local": False, "life_total": 17},
            {"seat_id": ME, "is_local": True, "life_total": 22},
        ],
        "battlefield": [
            {
                "instance_id": 250,
                "name": "Hapatra, Vizier of Poisons",
                "controller_seat_id": OPP,
                "type_line": "Legendary Creature — Human Cleric",
                "power": 2,
                "toughness": 2,
            }
        ],
        "hand": [],
        "stack": [
            {
                "instance_id": 290,
                "grp_id": 106279,
                "name": name,
                "type_line": "Instant",
                "oracle_text": oracle,
                "controller_seat_id": ME,
            }
        ],
        "graveyard": [],
        "exile": [],
        "command": [],
        "_bridge_request_payload": {"sourceId": 290},
        "decision_context": {"type": "target_selection", "source_id": 290, "source_card": name},
    }


def _decision(*seats: int, name: str = "Break Under Pressure") -> PendingDecision:
    seats = seats or (OPP,)
    return PendingDecision(
        request_id=(189, 250),
        request_type="SelectTargets",
        options=tuple(DecisionOption(f"tgt:{seat}", f"Player {seat}") for seat in seats),
        min_select=1,
        max_select=1,
        can_cancel=True,
        source_label=name,
        slots=(TargetSlot(1, 1, 1, 0, tuple(seats)),),
    )


def test_target_opponent_effects_are_harmful():
    assert target_effect_is_harmful(BREAK_UNDER_PRESSURE) is True
    assert (
        target_effect_is_harmful("Target opponent reveals their hand. That player discards a card.") is True
    )
    assert target_effect_is_harmful("Target player discards two cards.") is True
    # Self-mill decks aim this at themselves; draw is a benefit: both stay unread.
    assert target_effect_is_harmful("Target player mills three cards.") is None
    assert target_effect_is_harmful("Target player draws two cards.") is None


def test_the_live_pick_of_the_opponent_is_submitted():
    planner = _planner('{"option_ids": ["tgt:1"], "reasoning": "their only creature goes"}')
    assert planner.plan_decision_options(_decision(), _state()) == ["tgt:1"]


def test_a_discard_effect_aimed_at_ourselves_is_moved_to_the_opponent():
    text = "Target player discards two cards."
    planner = _planner('{"option_ids": ["tgt:2"], "reasoning": "discard"}')
    chosen = planner.plan_decision_options(_decision(OPP, ME, name="Mind Rot"), _state(text, "Mind Rot"))
    assert chosen == ["tgt:1"]
