"""Loot discards: say DISCARD, and name the card just drawn before deciding.

2026-10-08 game 1 (match 07c043d8, ~/.arenamcp/standalone.log 17:07-17:13):
Arni's loot presented a SelectN over our hand with no source label; the model
treated it as "pick the card to keep" and discarded the Swamp Garruk needed
(T7), Garruk himself (T11), Screeching Soulbreaker (T13) and Plan for All
Outcomes (T15), each with a rationale praising the discarded card. Twice the
freshly drawn card was an unnamed "Option N" because the bridge presented the
request before the log had the draw (Sphinx's Approach went to the yard
unseen).
"""

from __future__ import annotations

import time

import arenamcp.autopilot as autopilot_module
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine
from arenamcp.decisions import build_pending_decision, select_n_discard_note

HAND = {
    119: "Garruk, Veiled Butcher",
    122: "Icy Reception",
    207: "Living Library",
    225: "Mindseeker Oculus",
    232: "Swamp",
}
LOOT = {
    "instance_id": 231,
    "name": "Ability (ID: 1186)",
    "oracle_text": "{oT}: Draw a card, then discard a card.",
    "object_kind": "ABILITY",
}


def _state(stack=None, hand=HAND):
    return {
        "match_id": "07c043d8-e07b-4c15-9f05-173d2fe178ee",
        "turn": {"turn_number": 7, "active_player": 1, "phase": "Phase_Main1", "step": ""},
        "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2, "is_local": False}],
        "hand": [{"instance_id": iid, "name": name} for iid, name in hand.items()],
        "battlefield": [],
        "stack": list(stack or []),
    }


def _decision(ids, names=HAND):
    return build_pending_decision(
        {
            "has_pending": True,
            "request_type": "SelectN",
            "select_n_ids": list(ids),
            "select_n_min": 1,
            "select_n_max": 1,
        },
        resolve_instance=lambda iid: names.get(iid, ""),
    )


def test_a_loot_over_our_hand_is_framed_as_a_discard():
    note = select_n_discard_note(_decision(HAND), _state(stack=[LOOT]))
    assert note.startswith("THIS IS A DISCARD")
    assert "Draw a card, then discard a card" in note
    assert "value is a reason NOT to pick it" in note


def test_a_selection_that_is_not_a_discard_gets_no_note():
    assert select_n_discard_note(_decision(HAND), _state(stack=[])) == ""
    surveil = dict(LOOT, oracle_text="Surveil 1.")
    assert select_n_discard_note(_decision(HAND), _state(stack=[surveil])) == ""
    # Choices that are not our hand cards (a trigger ordering) are not discards.
    assert select_n_discard_note(_decision([234, 235], {}), _state(stack=[LOOT])) == ""


def test_a_discard_source_label_is_enough():
    decision = _decision(HAND)
    decision = type(decision)(**{**decision.__dict__, "source_label": "Twinned Vision: Discard a card"})
    assert select_n_discard_note(decision, _state()).startswith("THIS IS A DISCARD")


def test_the_card_just_drawn_is_marked_unknown_until_the_log_names_it():
    decision = _decision([119, 256])
    by_id = {option.option_id: option for option in decision.options}
    assert by_id["sel:119"].meta["identity_known"] is True
    assert by_id["sel:256"].label == "Option 256" and by_id["sel:256"].meta["identity_known"] is False


class _DummyBridge:
    connected = False

    def connect(self):
        return False


def test_the_typed_path_waits_briefly_for_the_name_then_decides(monkeypatch):
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: _DummyBridge())
    engine = AutopilotEngine(planner=None, get_game_state=lambda: {}, config=AutopilotConfig(dry_run=True))
    decision = _decision([119, 256])
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    assert engine._wait_for_search_identities(decision, _state()) is True  # waiting
    clock[0] += 1.0
    assert engine._wait_for_search_identities(decision, _state()) is True  # still within the cap
    clock[0] += engine._SELECT_N_IDENTITY_WAIT_S
    assert engine._wait_for_search_identities(decision, _state()) is False  # decide anyway, never manual
    assert engine._state != autopilot_module.AutopilotState.PAUSED
    # A fully named decision never waits.
    assert engine._wait_for_search_identities(_decision(HAND), _state()) is False
