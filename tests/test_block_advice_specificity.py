"""Block advice must name WHICH attacker each blocker blocks (issue #420).

First real Mac match (2026-07-16, log-only path — no GRE bridge): during a
DeclareBlockers decision the coach said "block with Veteran Survivor" without
naming an attacker, which is useless with multiple attackers on board.

Root cause was log-path data loss end to end:
  - gamestate.py never parsed the log's attackState enum (it looked for an
    "isAttacking" boolean that protobuf JSON never emits), so is_attacking
    stayed False without the bridge;
  - the DeclareBlockersReq handler dropped the per-blocker
    attackerInstanceIds, so decision_context had blocker names only;
  - the block-decision prompt therefore listed legal blockers but zero
    attackers, and nothing instructed the model to name assignments;
  - no post-check caught the vague advice.

These tests pin the log-path fixes. No network — the coach backend is a stub.
"""

from __future__ import annotations

from typing import Any

import pytest

from arenamcp.coach import CoachEngine
from arenamcp.gamestate import (
    GameObject,
    GameState,
    _parse_attack_state,
    _parse_block_state,
)


# ── helpers ──────────────────────────────────────────────────────────────


def _make_coach() -> CoachEngine:
    class _Stub:
        timeout_s = 5.0

        def complete(self, *a: Any, **k: Any) -> str:
            return ""

    return CoachEngine(backend=_Stub())


def _attacker(iid: int, name: str, power: int, toughness: int,
              oracle: str = "") -> dict[str, Any]:
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Creature — Beast",
        "power": power,
        "toughness": toughness,
        "oracle_text": oracle,
        "owner_seat_id": 2,
        "is_tapped": False,   # vigilance-style: NOT tapped — heuristic misses it
    }


def _blocker(iid: int, name: str, power: int, toughness: int) -> dict[str, Any]:
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Creature — Human Soldier",
        "power": power,
        "toughness": toughness,
        "oracle_text": "",
        "owner_seat_id": 1,
        "is_tapped": False,
    }


def _block_decision_state(*, flag_attackers: bool = False) -> dict[str, Any]:
    """Synthetic log-path game state: 2 attackers, 2 legal blockers.

    ``flag_attackers=False`` mimics the pre-fix log path where no battlefield
    card carries is_attacking — only decision_context knows the attackers.
    """
    atk1 = _attacker(50, "Grizzly Bears", 2, 2)
    atk2 = _attacker(51, "Colossal Dreadmaw", 6, 6, oracle="Trample")
    if flag_attackers:
        atk1["is_attacking"] = True
        atk2["is_attacking"] = True
    blk1 = _blocker(10, "Veteran Survivor", 1, 3)
    blk2 = _blocker(11, "Wall of Omens", 0, 4)
    return {
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 12},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "turn": {
            "active_player": 2,
            "priority_player": 1,
            "turn_number": 6,
            "phase": "Phase_Combat",
            "step": "Step_DeclareBlock",
        },
        "hand": [],
        "battlefield": [atk1, atk2, blk1, blk2],
        "graveyard": [],
        "stack": [],
        "exile": [],
        "legal_actions": [],
        "pending_decision": "Declare Blockers",
        "decision_context": {
            "type": "declare_blockers",
            "legal_blockers": ["Veteran Survivor", "Wall of Omens"],
            "legal_blocker_ids": [10, 11],
            "attacker_ids": [50, 51],
            "attackers": ["Grizzly Bears", "Colossal Dreadmaw"],
            "raw_blockers": [
                {"blockerInstanceId": 10, "attackerInstanceIds": [50, 51]},
                {"blockerInstanceId": 11, "attackerInstanceIds": [50, 51]},
            ],
        },
    }


# ── gamestate: log-path attacker capture ─────────────────────────────────


def test_parse_attack_state_enum_names():
    assert _parse_attack_state("AttackState_Attacking") is True
    assert _parse_attack_state("AttackState_Declared") is True
    assert _parse_attack_state("AttackState_None") is False
    # Tolerate raw enum ints (1=Declared, 2=Attacking)
    assert _parse_attack_state(2) is True
    assert _parse_attack_state(0) is False


def test_parse_block_state_enum_names():
    assert _parse_block_state("BlockState_Blocking") is True
    assert _parse_block_state("BlockState_Declared") is True
    # Blocked/Unblocked describe an ATTACKER's fate — must not set is_blocking
    assert _parse_block_state("BlockState_Blocked") is False
    assert _parse_block_state("BlockState_Unblocked") is False
    assert _parse_block_state("BlockState_None") is False


def test_declare_blockers_req_captures_attackers_from_log():
    """The log's declareBlockersReq blockers[].attackerInstanceIds must reach
    decision_context and flag is_attacking on the game objects."""
    from arenamcp.gamestate import _handle_decision_message

    gs = GameState()
    gs.game_objects[10] = GameObject(
        instance_id=10, grp_id=1001, zone_id=1, owner_seat_id=1)
    gs.game_objects[50] = GameObject(
        instance_id=50, grp_id=2001, zone_id=1, owner_seat_id=2)
    gs.game_objects[51] = GameObject(
        instance_id=51, grp_id=2002, zone_id=1, owner_seat_id=2)
    names = {1001: "Veteran Survivor", 2001: "Grizzly Bears",
             2002: "Colossal Dreadmaw"}
    gs._resolve_card_name = lambda grp_id: names.get(grp_id, f"Card {grp_id}")

    msg = {
        "declareBlockersReq": {
            "blockers": [
                {
                    "blockerInstanceId": 10,
                    "attackerInstanceIds": [50, 51],
                    "maxAttackers": 1,
                },
            ],
        },
    }
    _handle_decision_message(gs, "GREMessageType_DeclareBlockersReq", msg)

    ctx = gs.decision_context
    assert gs.pending_decision == "Declare Blockers"
    assert ctx["legal_blockers"] == ["Veteran Survivor"]
    assert ctx["legal_blocker_ids"] == [10]
    assert ctx["attacker_ids"] == [50, 51]
    assert ctx["attackers"] == ["Grizzly Bears", "Colossal Dreadmaw"]
    # Attackers are flagged on the objects, blocker is not
    assert gs.game_objects[50].is_attacking is True
    assert gs.game_objects[51].is_attacking is True
    assert gs.game_objects[10].is_attacking is False


# ── prompt: attacker enumeration + output-format instruction ─────────────


def test_block_prompt_enumerates_attackers_with_stats_and_instruction():
    """Log path (no is_attacking flags anywhere): the prompt must still list
    each attacker with P/T + keywords and instruct explicit assignments."""
    coach = _make_coach()
    state = _block_decision_state(flag_attackers=False)
    prompt = coach._format_game_context(state)

    # Attackers enumerated with stats
    assert "Grizzly Bears" in prompt
    assert "Colossal Dreadmaw" in prompt
    assert "2/2" in prompt
    assert "6/6" in prompt
    assert "TRAMPLE" in prompt

    # Explicit output-format instruction for assignments
    assert "Block [attacker] with" in prompt
    assert "No blocks" in prompt

    # Deterministic solver baseline is included (Calculator + Coach)
    assert "Computed optimal blocks:" in prompt


def test_block_prompt_decision_lines_alone_carry_attackers():
    coach = _make_coach()
    state = _block_decision_state(flag_attackers=False)
    lines = coach._format_decision_lines(state)
    blob = "\n".join(lines)
    assert "DECLARE BLOCKERS" in blob
    assert "Attackers:" in blob
    assert "Grizzly Bears 2/2" in blob
    assert "Colossal Dreadmaw 6/6" in blob
    assert "Block [attacker] with" in blob


def test_block_prompt_lists_restricted_blocker_candidates():
    """When the GRE restricts a blocker to a subset of attackers, the prompt
    says so explicitly."""
    coach = _make_coach()
    state = _block_decision_state(flag_attackers=False)
    # Wall of Omens may only block Grizzly Bears (e.g. skulk-like restriction)
    state["decision_context"]["raw_blockers"][1]["attackerInstanceIds"] = [50]
    lines = coach._format_decision_lines(state)
    blob = "\n".join(lines)
    assert "Wall of Omens can ONLY block: Grizzly Bears 2/2" in blob


def test_format_block_combat_lights_up_from_decision_context():
    """No is_attacking flags and no inferred ids — decision_context alone
    must produce the Attackers list and the solver line."""
    coach = _make_coach()
    state = _block_decision_state(flag_attackers=False)
    your_cards = [c for c in state["battlefield"] if c["owner_seat_id"] == 1]
    opp_cards = [c for c in state["battlefield"] if c["owner_seat_id"] == 2]
    lines = coach._format_block_combat(
        your_cards, opp_cards, state["players"][0], 6, "Phase_Combat", set(),
        decision_context=state["decision_context"],
    )
    blob = "\n".join(lines)
    assert "Attackers:" in blob
    assert "Grizzly Bears" in blob and "Colossal Dreadmaw" in blob
    assert "Computed optimal blocks:" in blob


# ── post-check: repair vague block advice ────────────────────────────────


def test_postcheck_repairs_vague_block_advice():
    """The exact issue-#420 failure: 'block with Veteran Survivor' with two
    attackers on board must come back naming an attacker."""
    coach = _make_coach()
    state = _block_decision_state(flag_attackers=True)
    out = coach._postprocess_advice("Block with Veteran Survivor.", state)
    assert "Veteran Survivor" in out
    # The repaired line names at least one real attacker from this combat
    assert ("Grizzly Bears" in out) or ("Colossal Dreadmaw" in out)
    assert "block" in out.lower()


def test_postcheck_repair_works_without_battlefield_flags():
    """Log path where even is_attacking flags are absent — attacker identity
    comes purely from decision_context."""
    coach = _make_coach()
    state = _block_decision_state(flag_attackers=False)
    out = coach._postprocess_advice("Block with Veteran Survivor.", state)
    assert ("Grizzly Bears" in out) or ("Colossal Dreadmaw" in out)


def test_postcheck_leaves_specific_advice_alone():
    coach = _make_coach()
    state = _block_decision_state(flag_attackers=True)
    advice = "Block Colossal Dreadmaw with Veteran Survivor."
    out = coach._postprocess_advice(advice, state)
    assert out.count("Colossal Dreadmaw") == 1  # not double-appended
    assert "Assignment:" not in out


def test_postcheck_leaves_negative_advice_alone():
    coach = _make_coach()
    state = _block_decision_state(flag_attackers=True)
    advice = "Don't block, take the damage."
    out = coach._postprocess_advice(advice, state)
    assert "Assignment:" not in out
    assert "don't block" in out.lower() or "don’t block" in out.lower()


def test_postcheck_noop_outside_block_decisions():
    coach = _make_coach()
    state = _block_decision_state(flag_attackers=True)
    state["pending_decision"] = None
    state["decision_context"] = None
    state["turn"]["phase"] = "Phase_Main1"
    state["turn"]["step"] = "Step_Main"
    state["turn"]["active_player"] = 1
    state["legal_actions"] = ["Pass"]
    out = coach._postprocess_advice("Pass priority.", state)
    assert "Assignment:" not in out


def test_solver_assignment_sentence_names_every_pair():
    """The deterministic repair sentence maps each used blocker to a named
    attacker."""
    coach = _make_coach()
    state = _block_decision_state(flag_attackers=True)
    attackers = coach._collect_block_decision_attackers(state)
    blockers = coach._collect_block_decision_blockers(state)
    sentence = coach._solver_block_assignment_sentence(
        state, attackers, blockers, "Block with Veteran Survivor."
    )
    assert sentence.startswith("Assignment:")
    assert "block " in sentence.lower()
    assert ("Grizzly Bears" in sentence) or ("Colossal Dreadmaw" in sentence)


def test_repair_respects_gre_blocker_restrictions():
    """A blocker restricted to one attacker must never be assigned to the
    other in the repaired advice."""
    coach = _make_coach()
    state = _block_decision_state(flag_attackers=True)
    # Veteran Survivor may only block Grizzly Bears
    state["decision_context"]["raw_blockers"][0]["attackerInstanceIds"] = [50]
    # Only one legal blocker in this scenario
    state["decision_context"]["legal_blockers"] = ["Veteran Survivor"]
    state["decision_context"]["legal_blocker_ids"] = [10]
    state["decision_context"]["raw_blockers"] = [
        state["decision_context"]["raw_blockers"][0]
    ]
    attackers = coach._collect_block_decision_attackers(state)
    blockers = coach._collect_block_decision_blockers(state)
    sentence = coach._solver_block_assignment_sentence(
        state, attackers, blockers, "Block with Veteran Survivor."
    )
    if sentence:  # solver may legitimately choose "no blocks" → fallback path
        assert "Colossal Dreadmaw with Veteran Survivor" not in sentence
