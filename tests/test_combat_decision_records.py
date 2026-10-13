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
