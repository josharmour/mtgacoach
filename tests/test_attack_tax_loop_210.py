"""Bug #210: the attack-tax loop burned Arena's clock every turn.

Match cd673437 (T9 18:55:30-18:57:17): declare 4 attackers -> PayCosts
"4xGeneric" (Archangel of Tithes' {1}-per-attacker tax) -> unpayable-cost
cancel -> Arena re-offers DeclareAttackers with the selections still held ->
the planner re-declares the same "free attack" -> 37 cancels in ~2 minutes
until the active-player timer passed the turn. On T13 the self-cancel guard
then blamed the tax on the last typed cast and withheld Herald's Horn, and
the safe default refused ("an attack selection is already pending") into
MANUAL REQUIRED.

Asserted here: the tax is recognised from a fresh attack declaration (and
never attributed to _last_typed_play), the attack request is answered with a
withdrawn no-attack instead of a re-declaration, and the safe default
submits that withdraw instead of refusing.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

import arenamcp.autopilot_bridge as autopilot_bridge
from arenamcp.action_planner import ActionType, GameAction
from arenamcp.autopilot import AutopilotEngine
from arenamcp.autopilot_progress import DecisionProgressGuard
from arenamcp.mac_bridge_adapter import MacBridgeAdapter

LOCAL, OPPONENT = 2, 1
TURN = 9


def taxed_state(turn: int = TURN) -> dict:
    return {
        "match_id": "cd673437",
        "local_seat_id": LOCAL,
        "turn": {"turn_number": turn, "active_player": LOCAL, "phase": "Phase_Combat", "step": ""},
        "players": [
            {"seat_id": OPPONENT, "is_local": False, "life_total": 20},
            {"seat_id": LOCAL, "is_local": True, "life_total": 18},
        ],
        "battlefield": [],
        "hand": [],
        "stack": [],
        "decision_context": {"type": "declare_attackers"},
    }


def attacker_pending(selected: bool = True) -> dict:
    """A DeclareAttackerRequest whose selection survived the tax cancel."""
    return {
        "has_pending": True,
        "request_type": "DeclareAttackers",
        "request_class": "DeclareAttackerRequest",
        "attackers": [
            {
                "attackerInstanceId": 322,
                "mustAttack": False,
                "legalDamageRecipients": [{"type": "DamageRecType_Player", "playerSystemSeatId": OPPONENT}],
                "selectedDamageRecipient": (
                    {"type": "DamageRecType_Player", "playerSystemSeatId": OPPONENT} if selected else None
                ),
            }
        ],
    }


class CancelBridge:
    """Records cancel_action calls for _cancel_unpayable_costs."""

    def __init__(self) -> None:
        self.cancels = 0

    def cancel_action(self) -> bool:
        self.cancels += 1
        return True


class AttackBridge:
    """Records submit_attackers_raw calls (keyword arguments included)."""

    def __init__(self, pending: dict) -> None:
        self.connected = True
        self.pending = pending
        self.calls: list[dict] = []

    def connect(self) -> bool:
        return True

    def get_pending_actions(self) -> dict:
        return self.pending

    def submit_attackers_raw(self, entries, **kwargs):
        self.calls.append({"entries": list(entries), **kwargs})
        return {"ok": True}

    def auto_respond(self):  # pragma: no cover - must never fire
        raise AssertionError("combat requests must never be auto-responded")


def engine_for(bridge, state: dict) -> AutopilotEngine:
    engine = AutopilotEngine.__new__(AutopilotEngine)
    engine._gre_bridge = bridge
    engine._abort_event = threading.Event()
    engine._progress_guard = DecisionProgressGuard()
    engine._progress_game_state = {}
    engine._progress_last_poll = None
    engine._config = MagicMock(dry_run=False)
    engine._path_stats = {}
    engine._gre_bridge_failed_methods = set()
    engine._advice_recorder = None
    engine._get_game_state = lambda: state
    return engine


def make_tax_engine() -> AutopilotEngine:
    from tests.fakes import make_engine

    engine = make_engine(game_state={})
    engine._progress_bridge = lambda *a, **k: CancelBridge()
    return engine


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(autopilot_bridge.time, "sleep", lambda seconds: None)


# --- tax detection ------------------------------------------------------------------


def test_unpayable_cost_right_after_attacking_is_an_attack_tax():
    engine = make_tax_engine()
    state = taxed_state()
    # Herald's Horn was typed before the attack (the T13 sequence).
    decoy = (time.monotonic() - 1.0, 8, "ActionType_Cast", 219, 0, "herald's horn")
    engine._last_typed_play = decoy
    engine._note_attack_declared(state, [322])
    assert engine._cancel_unpayable_costs(state, "trigger", "PayCosts cancelled (no payment route)")
    assert engine._attack_tax_unpaid_turn == TURN
    # The tax is not misattributed to the last typed cast (#210 T13): it is
    # neither withheld nor cleared.
    assert engine._last_typed_play == decoy


def test_a_newer_cast_is_still_withheld_rather_than_called_a_tax():
    engine = make_tax_engine()
    state = taxed_state()
    engine._note_attack_declared(state, [322])
    # The typed play came after the declaration: the cost is its own.
    engine._last_typed_play = (time.monotonic() + 0.001, TURN, "ActionType_Cast", 219, 0, "herald's horn")
    assert engine._cancel_unpayable_costs(state, "trigger", "PayCosts cancelled (no autotap)")
    assert engine._attack_tax_unpaid_turn == 0
    assert engine._last_typed_play is None  # withheld as before #210


def test_an_old_attack_declaration_is_not_a_tax():
    engine = make_tax_engine()
    state = taxed_state()
    engine._note_attack_declared(state, [322])
    engine._last_attack_declared = (time.monotonic() - 30.0, TURN)
    assert not engine._attack_tax_follows_declaration(state)
    assert engine._cancel_unpayable_costs(state, "trigger", "PayCosts cancelled (no autotap)")
    assert engine._attack_tax_unpaid_turn == 0


def test_the_tax_flag_expires_with_its_turn():
    engine = make_tax_engine()
    state = taxed_state()
    engine._note_attack_declared(state, [322])
    assert engine._cancel_unpayable_costs(state, "trigger", "PayCosts cancelled (no payment route)")
    assert engine._attack_tax_pending(state)
    assert not engine._attack_tax_pending(taxed_state(turn=TURN + 1))


# --- the withdraw -------------------------------------------------------------------


def test_a_taxed_attack_request_is_withdrawn_not_redeclared():
    state = taxed_state()
    bridge = AttackBridge(attacker_pending())
    engine = engine_for(bridge, state)
    engine._attack_tax_unpaid_turn = TURN
    result = engine._try_bridge_declare_attackers(
        GameAction(ActionType.DECLARE_ATTACKERS, attacker_names=["Slickshot Show-Off [id:322]"])
    )
    assert result is not None and result.success
    assert bridge.calls == [{"entries": [], "withdraw": True}]
    assert engine._attack_tax_unpaid_turn == 0  # answered; later turns attack normally


def test_withdraw_falls_back_to_the_plain_no_attack_submit():
    state = taxed_state()
    bridge = AttackBridge(attacker_pending())

    def flaky(entries, **kwargs):
        bridge.calls.append({"entries": list(entries), **kwargs})
        if kwargs.get("withdraw"):
            return {"ok": False, "error": "not supported"}
        return {"ok": True}

    bridge.submit_attackers_raw = flaky
    engine = engine_for(bridge, state)
    engine._attack_tax_unpaid_turn = TURN
    result = engine._withdraw_taxed_attack(bridge, state)
    assert result is not None and result.success
    assert [bool(call.get("withdraw")) for call in bridge.calls] == [True, False]


def test_the_safe_default_submits_the_withdraw_instead_of_refusing():
    state = taxed_state()
    bridge = AttackBridge(attacker_pending())
    engine = engine_for(bridge, state)
    engine._attack_tax_unpaid_turn = TURN
    notes: list[str] = []

    def ok(note: str) -> bool:
        notes.append(note)
        return True

    assert engine._safe_default_attacks(state, bridge.get_pending_actions(), ok) is True
    assert notes == ["declare_attackers: no attackers (unpaid attack tax)"]
    assert bridge.calls == [{"entries": [], "withdraw": True}]


def test_the_safe_default_still_refuses_without_the_tax():
    state = taxed_state()
    bridge = AttackBridge(attacker_pending())
    engine = engine_for(bridge, state)
    notes: list[str] = []
    assert engine._safe_default_attacks(state, bridge.get_pending_actions(), notes.append) is False
    assert bridge.calls == []


# --- the adapter's deselect ---------------------------------------------------------


def test_the_adapter_withdraw_clears_selections_then_finalizes():
    from tests.mac_bridge_fakes import MSG, FakeGame, enum, listing

    recipient = {"$c": MSG + "DamageRecipient", "$h": 31, "type_": enum("Player", 1)}
    attacker = {
        "$c": MSG + "Attacker",
        "$h": 30,
        "attackerInstanceId_": 500,
        "legalDamageRecipients_": listing(recipient),
        "selectedDamageRecipient_": recipient,
    }
    game = FakeGame(
        {"$c": "GreClient.Rules.DeclareAttackerRequest", "$h": 5, "Attackers": listing(attacker)}
    )
    response = MacBridgeAdapter(game.send).handle(
        {"action": "submit_attackers", "attackers": [], "withdraw": True}
    )
    assert response["ok"] is True
    assert response["submitted_type"] == "DeclareAttackersWithdraw"
    assert response["withdrawn_count"] == 1
    [submit] = game.submits()
    sets = [op for op in submit if op["op"] == "set"]
    assert [(op["target"], op["member"], op["value"]) for op in sets] == [
        ({"h": 30}, "SelectedDamageRecipient", {"null": True})
    ]
    calls = [op["method"] for op in submit if op["op"] == "call"]
    assert calls == ["UpdateAttacker", "SubmitAttackers"]
    update_args = [op["args"] for op in submit if op.get("method") == "UpdateAttacker"][0]
    assert update_args == [{"list": [{"h": 30}]}]


def test_the_adapter_withdraw_with_nothing_held_just_finalizes():
    from tests.mac_bridge_fakes import MSG, FakeGame, enum, listing

    recipient = {"$c": MSG + "DamageRecipient", "$h": 31, "type_": enum("Player", 1)}
    attacker = {
        "$c": MSG + "Attacker",
        "$h": 30,
        "attackerInstanceId_": 500,
        "legalDamageRecipients_": listing(recipient),
        "selectedDamageRecipient_": None,
    }
    game = FakeGame(
        {"$c": "GreClient.Rules.DeclareAttackerRequest", "$h": 5, "Attackers": listing(attacker)}
    )
    response = MacBridgeAdapter(game.send).handle(
        {"action": "submit_attackers", "attackers": [], "withdraw": True}
    )
    assert response == {"ok": True, "submitted_type": "DeclareAttackersSubmit"}
    [submit] = game.submits()
    assert [op["method"] for op in submit if op["op"] == "call"] == ["SubmitAttackers"]
