"""Attack and block declarations are recorded for bug reports beside the typed decisions (2026-10-10)."""

from __future__ import annotations

from arenamcp.match_packets import MatchPacket


def test_combat_declarations_render_in_recent_decisions():
    packet = MatchPacket("m1")
    state = {"turn": {"turn_number": 19, "phase": "Combat", "step": "DeclareAttack", "active_player": 2}}
    packet.add_combat_decision(
        "DeclareAttackers",
        chosen=["Surveillance Phantasm -> Opponent"],
        options=["Attack with: Geist of Saint Thalia (1/2)", "Attack with: Surveillance Phantasm (2/3)"],
        explanation="Chip with the vigilant flyer while holding blockers.",
        trace={"reasoning": "Chip with the vigilant flyer while holding blockers.", "fallback_reason": ""},
        game_state=state,
    )
    packet.add_combat_decision(
        "DeclareBlockers",
        chosen=["Surveillance Phantasm [id:339] blocks Shatterwing Pegasus [id:350]"],
        options=["Block with: Geist of Saint Thalia", "Block with: Surveillance Phantasm"],
        explanation="Safe default: 0 damage at 4 life (no-block 18)",
        trace={"fallback_reason": "llm_unavailable"},
        game_state={
            "turn": {"turn_number": 20, "phase": "Combat", "step": "DeclareBlock", "active_player": 1}
        },
    )
    recent = packet.recent_decisions(5)
    assert [r["request_type"] for r in recent] == ["DeclareAttackers", "DeclareBlockers"]
    assert recent[0]["chosen"] == ["Surveillance Phantasm -> Opponent"]
    assert recent[0]["options"][1] == "Attack with: Surveillance Phantasm (2/3)"
    assert recent[0]["turn"]["turn_number"] == 19 and recent[0]["outcome"] == "executed"
    assert recent[1]["chosen"] == ["Surveillance Phantasm [id:339] blocks Shatterwing Pegasus [id:350]"]
    assert recent[1]["trace"] == {"fallback_reason": "llm_unavailable"}
    assert "Safe default" in recent[1]["explanation"]


def _attack_pilot(state):
    from unittest.mock import Mock

    from arenamcp.autopilot_bridge import _BridgeSubmitMixin

    pilot = _BridgeSubmitMixin()
    pilot._get_game_state = lambda: state
    pilot._find_instance_id = lambda name, battlefield, local: next(
        (card["instance_id"] for card in battlefield if card["name"] == name), None
    )
    pilot._gre_bridge = Mock()
    pilot._gre_bridge.get_pending_actions.return_value = {
        "has_pending": True,
        "request_class": "DeclareAttackerRequest",
        "attackers": state["decision_context"]["raw_attackers"],
    }
    pilot._gre_bridge.submit_attackers_raw.return_value = {"ok": True}
    pilot._attack_override = Mock(return_value=None)
    pilot._log_execution_path = Mock()
    return pilot


def test_a_bridge_attack_declaration_reaches_the_match_packet():
    """Live attacks were never recorded, only blocks: the recording wrapper
    had been renamed onto itself (eb72e38) and the live attack path,
    _try_bridge_declare_attackers, never called _record_combat_decision."""
    from arenamcp import match_packets
    from arenamcp.action_planner import ActionType, GameAction

    state = {
        "players": [{"seat_id": 2, "is_local": True}, {"seat_id": 1, "is_local": False}],
        "battlefield": [{"name": "Beast", "instance_id": 288}],
        "turn": {"turn_number": 7, "phase": "Combat", "step": "DeclareAttack", "active_player": 2},
        "legal_actions": ["Attack with: Beast (3/3)", "Cast Shock"],
        "decision_context": {
            "type": "declare_attackers",
            "raw_attackers": [
                {
                    "attackerInstanceId": 288,
                    "legalDamageRecipients": [{"type": "Player", "playerSystemSeatId": 1}],
                }
            ],
        },
    }
    pilot = _attack_pilot(state)
    previous = match_packets.stop_match_packet()
    match_packets.start_match_packet("attack-record-test")
    try:
        action = GameAction(
            action_type=ActionType.DECLARE_ATTACKERS,
            attacker_names=["Beast"],
            reasoning="Beast attacks into an empty board.",
        )
        result = pilot._try_bridge_declare_attackers(action)
        recent = match_packets.get_current_packet().recent_decisions(5)
    finally:
        match_packets.stop_match_packet()
        match_packets._current_packet = previous
    assert result is not None and result.success
    assert [entry["request_type"] for entry in recent] == ["DeclareAttackers"]
    assert recent[0]["chosen"] == ["Beast -> Opponent"]
    assert recent[0]["options"] == ["Attack with: Beast (3/3)"]
    assert recent[0]["explanation"] == "Beast attacks into an empty board."


def test_a_refused_attack_declaration_is_not_recorded():
    from arenamcp import match_packets
    from arenamcp.action_planner import ActionType, GameAction

    state = {
        "players": [{"seat_id": 2, "is_local": True}, {"seat_id": 1, "is_local": False}],
        "battlefield": [{"name": "Beast", "instance_id": 288}],
        "decision_context": {
            "type": "declare_attackers",
            "raw_attackers": [
                {
                    "attackerInstanceId": 288,
                    "legalDamageRecipients": [{"type": "Player", "playerSystemSeatId": 1}],
                }
            ],
        },
    }
    pilot = _attack_pilot(state)
    pilot._gre_bridge.submit_attackers_raw.return_value = {"ok": False}
    previous = match_packets.stop_match_packet()
    match_packets.start_match_packet("attack-refused-test")
    try:
        action = GameAction(action_type=ActionType.DECLARE_ATTACKERS, attacker_names=["Beast"])
        assert pilot._try_bridge_declare_attackers(action) is None
        recent = match_packets.get_current_packet().recent_decisions(5)
    finally:
        match_packets.stop_match_packet()
        match_packets._current_packet = previous
    assert recent == []
