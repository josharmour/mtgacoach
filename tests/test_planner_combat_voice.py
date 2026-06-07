"""Tests for combat voice phrasing in action_planner._humanize_legal_action.

The deterministic fallback voice_advice should be a clean, concrete spoken
sentence — no "(P/T)"/"[tag]" clutter, and a non-block rendered as "Don't block."
rather than a confusing "Confirm." This complements the system-prompt VOICE
CLARITY rule (which shapes the LLM path).
"""

from __future__ import annotations

from arenamcp.action_planner import ActionPlanner


h = ActionPlanner._humanize_legal_action


def test_block_strips_annotations():
    assert h("Block with: Llanowar Elves (1/1) [CHUMP]") == "Block with Llanowar Elves."


def test_block_keeps_dedup_index():
    assert h("Block with: Llanowar Elves #2 (1/1)") == "Block with Llanowar Elves #2."


def test_attack_with_strips_annotations():
    assert h("Attack with: Grizzly Bears #2 (2/2) [0 POWER]") == "Attack with Grizzly Bears #2."


def test_declare_attackers_strips_annotations():
    assert h("Declare Attackers: Bear (2/2)") == "Attack with Bear."


def test_no_blocks_renders_dont_block():
    assert h("Done (no blocks)") == "Don't block."
    assert h("Declare no blockers") == "Don't block."


def test_cast_and_land_unchanged():
    assert h("Cast Lightning Bolt [OK]") == "Cast Lightning Bolt."
    assert h("Play Land: Forest") == "Play Forest."


def test_pass_is_terse():
    assert h("Pass (Opponent has priority)") == "Pass."


def test_voice_clarity_rule_present_in_prompt():
    from arenamcp.action_planner import AUTOPILOT_SYSTEM_PROMPT

    assert "VOICE CLARITY" in AUTOPILOT_SYSTEM_PROMPT
    assert "Computed optimal blocks" in AUTOPILOT_SYSTEM_PROMPT
