"""Unit tests for the meaningful-window advice gate in standalone.py.

These tests exercise the pure ``StandaloneCoach._is_meaningful_advice_window``
predicate over crafted ``game_state`` dicts. The predicate is what gates
NON-CRITICAL triggers in the coaching loop: a *meaningful* window (real choice)
→ SPEAK (generate advice / Coach-Log / TTS); a *trivial* window (pass-only,
opponent priority with no instant response, empty legal moves) → SKIP entirely.

No GUI / live MTGA is needed — everything is pure-function over dicts. This is
the autonomous verification path for the "only speak on meaningful decisions"
filter.
"""

from __future__ import annotations

from typing import Any

from arenamcp.coach import GameStateTrigger
from arenamcp.standalone import StandaloneCoach


# Seat 1 == local player, seat 2 == opponent (mirrors the shape every filter
# helper reads: players[].seat_id/is_local/life_total, turn.active/priority,
# legal_actions, pending_decision, hand, battlefield).
LOCAL_SEAT = 1
OPP_SEAT = 2


def _make_state(
    *,
    legal_actions: list[str] | None = None,
    pending_decision: str | None = None,
    active_player: int = LOCAL_SEAT,
    priority_player: int = LOCAL_SEAT,
    local_life: int = 20,
    opp_life: int = 20,
    hand: list[dict] | None = None,
    battlefield: list[dict] | None = None,
    phase: str = "Phase_Main1",
    step: str = "Step_Main",
) -> dict[str, Any]:
    return {
        "players": [
            {"seat_id": LOCAL_SEAT, "is_local": True, "life_total": local_life},
            {"seat_id": OPP_SEAT, "is_local": False, "life_total": opp_life},
        ],
        "turn": {
            "active_player": active_player,
            "priority_player": priority_player,
            "turn_number": 4,
            "phase": phase,
            "step": step,
        },
        "hand": hand or [],
        "battlefield": battlefield or [],
        "graveyard": [],
        "stack": [],
        "exile": [],
        "legal_actions": list(legal_actions or []),
        "pending_decision": pending_decision,
    }


def _is_meaningful(state: dict[str, Any], *, has_castable_instants: bool = False) -> bool:
    return StandaloneCoach._is_meaningful_advice_window(
        state, has_castable_instants=has_castable_instants
    )


# --------------------------------------------------------------------------- #
# Your turn: real plays available → SPEAK
# --------------------------------------------------------------------------- #
def test_your_turn_castable_spell_speaks():
    state = _make_state(legal_actions=["Cast Lightning Bolt [ok]", "Wait"])
    assert _is_meaningful(state) is True


def test_your_turn_playable_land_speaks():
    state = _make_state(legal_actions=["Play Land: Forest", "Wait"])
    assert _is_meaningful(state) is True


def test_your_turn_activated_ability_speaks():
    state = _make_state(legal_actions=["Activate Ability: tap for mana", "Wait"])
    assert _is_meaningful(state) is True


def test_your_turn_legal_attack_speaks():
    state = _make_state(
        legal_actions=["Action: Attack with Grizzly Bears", "Wait"],
        phase="Phase_Combat",
        step="Step_DeclareAttack",
    )
    assert _is_meaningful(state) is True


# --------------------------------------------------------------------------- #
# Your turn: nothing to do → SKIP
# --------------------------------------------------------------------------- #
def test_your_turn_pass_only_skips():
    state = _make_state(legal_actions=["Wait"])
    assert _is_meaningful(state) is False


def test_your_turn_pass_priority_only_skips():
    state = _make_state(legal_actions=["Pass", "Pass Priority"])
    assert _is_meaningful(state) is False


def test_empty_legal_moves_no_pending_skips():
    state = _make_state(legal_actions=[])
    assert _is_meaningful(state) is False


# --------------------------------------------------------------------------- #
# Opponent's turn / opponent priority
# --------------------------------------------------------------------------- #
def test_opponent_turn_no_instants_skips():
    state = _make_state(
        active_player=OPP_SEAT,
        priority_player=OPP_SEAT,
        legal_actions=["Wait"],
    )
    assert _is_meaningful(state, has_castable_instants=False) is False


def test_opponent_turn_with_castable_instant_speaks():
    state = _make_state(
        active_player=OPP_SEAT,
        priority_player=OPP_SEAT,
        legal_actions=["Wait"],
    )
    assert _is_meaningful(state, has_castable_instants=True) is True


def test_opponent_turn_local_priority_with_instant_legal_speaks():
    # Opponent's turn but priority passed to us and an instant cast is offered.
    state = _make_state(
        active_player=OPP_SEAT,
        priority_player=LOCAL_SEAT,
        legal_actions=["Cast Counterspell [ok]", "Wait"],
    )
    assert _is_meaningful(state) is True


# --------------------------------------------------------------------------- #
# Pending decisions → always SPEAK (even with sparse legal_actions)
# --------------------------------------------------------------------------- #
def test_pending_target_selection_speaks():
    state = _make_state(legal_actions=[], pending_decision="Select Targets")
    assert _is_meaningful(state) is True


def test_pending_scry_speaks():
    state = _make_state(legal_actions=[], pending_decision="Scry")
    assert _is_meaningful(state) is True


def test_pending_discard_speaks():
    state = _make_state(legal_actions=[], pending_decision="Discard")
    assert _is_meaningful(state) is True


def test_pending_mulligan_speaks():
    state = _make_state(
        legal_actions=[],
        pending_decision="Mulligan",
        phase="Phase_None",
        step="Step_None",
    )
    assert _is_meaningful(state) is True


# --------------------------------------------------------------------------- #
# Generic "Action Required" placeholder is NOT a real decision
# --------------------------------------------------------------------------- #
def test_action_required_pass_only_skips():
    state = _make_state(legal_actions=["Pass"], pending_decision="Action Required")
    assert _is_meaningful(state) is False


def test_action_required_with_real_play_speaks():
    # "Action Required" placeholder but an actual castable spell is offered.
    state = _make_state(
        legal_actions=["Cast Llanowar Elves [ok]", "Pass"],
        pending_decision="Action Required",
    )
    assert _is_meaningful(state) is True


# --------------------------------------------------------------------------- #
# Low / lethal life → SPEAK (defensive bias)
# --------------------------------------------------------------------------- #
def test_low_life_speaks_even_pass_only():
    state = _make_state(legal_actions=["Wait"], local_life=3)
    assert _is_meaningful(state) is True


def test_low_life_on_opponent_turn_speaks():
    state = _make_state(
        active_player=OPP_SEAT,
        priority_player=OPP_SEAT,
        legal_actions=["Wait"],
        local_life=4,
    )
    assert _is_meaningful(state, has_castable_instants=False) is True


def test_low_life_threshold_boundary():
    # At the threshold (<=5) it is meaningful; just above it falls back to the
    # normal pass-only triviality check.
    assert _is_meaningful(_make_state(legal_actions=["Wait"], local_life=5)) is True
    assert _is_meaningful(_make_state(legal_actions=["Wait"], local_life=6)) is False


# --------------------------------------------------------------------------- #
# Uncertainty bias: unrecognized non-pass legal action on local priority → SPEAK
# --------------------------------------------------------------------------- #
def test_unknown_non_pass_action_biases_to_speak():
    state = _make_state(legal_actions=["Choose a mode", "Wait"])
    assert _is_meaningful(state) is True


# --------------------------------------------------------------------------- #
# Gate scoping: the predicate only gates noisy "filler" triggers. Real
# decision points (combat, new_turn, critical) must never be gated, so a
# mis-populated combat-blockers window can't silence a real block.
# --------------------------------------------------------------------------- #
def test_gate_scoped_to_filler_triggers_only():
    gated = StandaloneCoach._MEANINGFUL_GATE_TRIGGERS
    # Filler triggers ARE gated.
    assert {"priority_gained", "opponent_turn", "land_played", "spell_resolved"} <= set(gated)
    # Real decision points must NOT be gated.
    for protected in (
        "combat_attackers",
        "combat_blockers",
        "new_turn",
        "decision_required",
        "low_life",
        "opponent_low_life",
        "threat_detected",
        "losing_badly",
        "stack_spell_yours",
        "stack_spell_opponent",
    ):
        assert protected not in gated, f"{protected} must not be gated"


# --------------------------------------------------------------------------- #
# Consistency with the real castable-instants detector used by the loop
# --------------------------------------------------------------------------- #
def test_integration_with_real_castable_instants_detector():
    """The loop computes has_castable_instants via GameStateTrigger; verify the
    predicate + that detector agree for an instant-in-hand opponent window."""
    trigger = GameStateTrigger.__new__(GameStateTrigger)
    instant_hand = [
        {
            "name": "Lightning Bolt",
            "type_line": "Instant",
            "oracle_text": "Lightning Bolt deals 3 damage to any target.",
            "mana_cost": "{R}",
        }
    ]
    battlefield = [
        {"owner_seat_id": LOCAL_SEAT, "type_line": "Basic Land — Mountain", "is_tapped": False}
    ]
    state = _make_state(
        active_player=OPP_SEAT,
        priority_player=OPP_SEAT,
        legal_actions=["Wait"],
        hand=instant_hand,
        battlefield=battlefield,
    )
    has_instants = GameStateTrigger._has_castable_instants(trigger, state)
    assert has_instants is True
    assert _is_meaningful(state, has_castable_instants=has_instants) is True

    # No untapped lands → can't actually cast it → trivial opponent window.
    state_no_mana = _make_state(
        active_player=OPP_SEAT,
        priority_player=OPP_SEAT,
        legal_actions=["Wait"],
        hand=instant_hand,
        battlefield=[
            {"owner_seat_id": LOCAL_SEAT, "type_line": "Basic Land — Mountain", "is_tapped": True}
        ],
    )
    has_instants_dry = GameStateTrigger._has_castable_instants(trigger, state_no_mana)
    assert has_instants_dry is False
    assert _is_meaningful(state_no_mana, has_castable_instants=has_instants_dry) is False
