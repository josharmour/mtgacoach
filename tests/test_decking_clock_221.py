"""Ledger #221: alternate win conditions — the decking (mill) clock.

The coach framed whole games as face-damage races while an opposing mill
engine banked (match 5c4b220a: one T19 activation milled our last 16 cards,
loss on the T20 draw step at 20 life). These tests pin the deterministic
decking facts: engine detection from oracle text, the observed mill rate from
zone_transfer Mill events, the plan-block fact, the role note that replaces
pure face-race framing, and the library count sourced from zone membership.
"""

from __future__ import annotations

from copy import deepcopy

from arenamcp import decking_clock as dc
from arenamcp.autopilot_progress import decision_semantics
from arenamcp.board_assessment import assess
from arenamcp.game_plan import _grounded_win

MILL_ENGINE = {
    "instance_id": 341,
    "grp_id": 94859,
    "name": "Eerie Millstone",
    "oracle_text": "{4}, {T}: Target player mills 16.",
    "type_line": "Artifact",
    "card_types": ["CardType_Artifact"],
    "controller_seat_id": 2,
    "owner_seat_id": 2,
    "is_tapped": False,
}


def _state(
    *,
    library: int | None = 32,
    engine: bool = True,
    events: list | None = None,
    source: str = "log_zone_membership",
):
    battlefield = [dict(MILL_ENGINE)] if engine else []
    return {
        "match_id": "221",
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "turn": {
            "turn_number": 12,
            "active_player": 1,
            "priority_player": 1,
            "phase": "Phase_Main1",
            "step": "",
        },
        "players": [
            {"seat_id": 1, "life_total": 20, "is_local": True, "lands_played": 0},
            {"seat_id": 2, "life_total": 9, "is_local": False, "lands_played": 0},
        ],
        "battlefield": battlefield,
        "hand": [],
        "graveyard": [],
        "stack": [],
        "zones": {
            "library_count": library if library is not None else "?",
            "library_count_source": source if library is not None else "unknown",
            "opponent_library_count": 40,
        },
        "recent_events": events or [],
    }


def _mill_transfer(turn: int, instance_id: int) -> dict:
    return {
        "type": "zone_transfer",
        "card": f"Card{instance_id}",
        "instance_id": instance_id,
        "from_zone": 32,
        "to_zone": 33,
        "category": "Mill",
        "owner_seat_id": 1,
        "controller_seat_id": 1,
        "turn": turn,
    }


def _fresh(state):
    from arenamcp import board_assessment as ba

    ba._CACHE.clear()
    result = assess(deepcopy(state))
    assert result is not None
    return result


# --- engine detection and rates ------------------------------------------------


def test_mill_engine_detected_from_oracle_text():
    assert dc.mill_engines([dict(MILL_ENGINE)], 2) == ["Eerie Millstone"]
    assert dc.mill_engines([dict(MILL_ENGINE)], 1) == []  # our own engine is no threat
    assert dc.mill_amount(MILL_ENGINE["oracle_text"]) == 16
    assert dc.mill_amount("Put the top eight cards of that player's library into their graveyard.") == 8
    assert (
        dc.mill_amount("Whenever this deals combat damage to a player, that player mills that many cards.")
        is None
    )


def test_observed_mill_reads_zone_transfers_of_our_cards():
    events = [_mill_transfer(18, i) for i in range(430, 435)] + [
        _mill_transfer(19, i) for i in range(440, 456)
    ]
    per_turn = dc.observed_mill_per_turn(events, 1)
    assert per_turn == {18: 5, 19: 16}
    # Their own cards going to their graveyard are not mill against us.
    theirs = [dict(_mill_transfer(20, 500), owner_seat_id=2, controller_seat_id=2)]
    assert dc.observed_mill_per_turn(theirs, 1) == {}


def test_decking_threat_prefers_the_fastest_observed_turn():
    events = [_mill_transfer(18, i) for i in range(430, 435)] + [
        _mill_transfer(19, i) for i in range(440, 456)
    ]
    threat = dc.decking_threat(_state(library=32, events=events))
    assert threat is not None
    assert threat.rate == 16 and threat.turns == 2 and threat.milled_recent == 21
    assert threat.engines == ["Eerie Millstone"]


# --- assessment integration ----------------------------------------------------


def test_mill_engine_on_board_puts_the_decking_fact_in_the_plan_block():
    a = _fresh(_state(library=32))
    assert a.decking_turns == 2 and a.mill_engines == ["Eerie Millstone"]
    assert a.decking_fact == "we deck in ~2 turn(s) (mill engine: Eerie Millstone on board)"
    facts = a.facts_line()
    assert "we deck in ~2 turn(s)" in facts
    assert a.prompt_block().count("we deck in ~2 turn(s)") == 1
    # Urgent decking leads the flags.
    assert any("DECKING IN ~2 TURNS" in f and "Eerie Millstone" in f for f in a.flags)


def test_no_mill_threat_no_decking_fact():
    a = _fresh(_state(engine=False))
    assert a.decking_turns is None and a.decking_fact == ""
    assert a.mill_engines == [] and a.milled_recent == 0
    assert "deck" not in a.facts_line()


def test_library_count_sourced_from_zone_membership():
    # An unobserved library (source unknown) yields no threat even with an engine.
    a = _fresh(_state(library=None))
    assert a.decking_turns is None and a.decking_fact == ""
    # The count itself comes from zones.library_count, not guesses.
    a = _fresh(_state(library=100))
    assert a.decking_turns == 7 and a.library_count == 100
    # The opponent's count rides along for our own mill plans.
    assert a.opponent_library_count == 40


def test_observed_mill_without_engine_still_threatens():
    events = [_mill_transfer(19, i) for i in range(440, 456)]
    a = _fresh(_state(library=16, engine=False, events=events))
    assert a.mill_rate == 16 and a.decking_turns == 1
    assert "milled 16 in recent turns" in a.decking_fact


def test_role_note_replaces_face_race_when_mill_beats_our_clock():
    # No creatures anywhere: we have no damage clock, so the role reason must
    # name the mill clock instead of pure face-race framing.
    a = _fresh(_state(library=32))
    assert a.role_reason.endswith(
        "their mill decks us in ~2 (we have no damage clock) — remove Eerie Millstone or win before it"
    )


def test_role_note_quiet_when_our_clock_wins_first():
    threat = dc.DeckingThreat(cards_left=90, engines=["Eerie Millstone"], rate=16, turns=6)
    assert dc.role_note(threat, our_clock=3) == ""
    assert dc.role_note(None, our_clock=None) == ""
    slow = dc.DeckingThreat(cards_left=90)  # engine on board, no rate known
    assert dc.role_note(slow, our_clock=None) == ""


# --- plan and window consumers --------------------------------------------------


def test_grounded_win_names_removing_the_engine():
    a = _fresh(_state(library=32))
    win = _grounded_win(a, _state(library=32))
    assert win.startswith("Remove Eerie Millstone (decks us in ~2)")
    # With our clock clearly faster the decking branch stays out of the way.
    from types import SimpleNamespace

    calm = SimpleNamespace(
        role="aggressor", our_clock=1, decking_turns=6, mill_engines=["Eerie Millstone"], lookahead=[]
    )
    assert not _grounded_win(calm, _state(library=32)).startswith("Remove")


def test_decision_semantics_window_includes_our_library_count():
    poll = {"request_type": "DeclareAttackersRequest", "has_pending": True}
    before = _state(library=32)
    after = _state(library=16)
    window_before = decision_semantics(poll, before)
    window_after = decision_semantics(poll, after)
    assert window_before is not None and window_after is not None
    assert window_before["board"]["library_count"] == 32
    assert window_after["board"]["library_count"] == 16
