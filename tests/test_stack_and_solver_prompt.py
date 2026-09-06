"""Stack state + combat-solver deference must actually reach the planner prompt.

Motivation (2026-07-26): a 31B LoRA trained on play decisions matched the
untuned base exactly on the 63 strategic (non-land-drop) decisions
(13/63 vs 13/63, paired delta 0.0000). Two prompt-side causes were measured
before any retraining:

  1. The planner's rendered state carried a ``Stack: Y:A > O:B`` one-liner
     that dropped every target and implied the WRONG resolution order (GRE
     lists the stack bottom-first, so the LAST entry resolves FIRST). A model
     that cannot see what a spell targets cannot reason about responses,
     counterspells or timing at all.

  2. ``_format_block_combat`` — the only producer of the
     "Computed optimal blocks:" line — was dispatched on ``"Combat" in phase``
     alone, even though ``_in_block_decision`` right above it already treats a
     GRE ``declare_blockers`` decision context as authoritative. A block
     decision arriving with a lagging/absent phase string silently lost the
     solver line on exactly the decision it exists for.

  3. ``AUTOPILOT_SYSTEM_PROMPT`` had NO solver-deference rule at all — only a
     clause requiring voice_advice to match the blocks line. The coach system
     prompts in coach_prompts.py had one; the autopilot planner did not.

No network: these tests only exercise the pure formatters.
"""

from __future__ import annotations

from typing import Any

from arenamcp.action_planner import AUTOPILOT_SYSTEM_PROMPT
from arenamcp.coach import CoachEngine

# ── helpers ──────────────────────────────────────────────────────────────

LOCAL_SEAT = 1
OPP_SEAT = 2


def _fmt() -> CoachEngine:
    """A CoachEngine usable for pure formatting (no __init__ / no backend)."""
    return CoachEngine.__new__(CoachEngine)


def _creature(iid: int, name: str, seat: int, power: int = 2, toughness: int = 2, **kw: Any) -> dict:
    card = {
        "instance_id": iid,
        "name": name,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "type_line": "Creature - Human",
        "power": power,
        "toughness": toughness,
        "oracle_text": "",
        "turn_entered_battlefield": 1,
    }
    card.update(kw)
    return card


def _state(
    *,
    phase: str = "Phase_Main2",
    step: str = "",
    active: int = OPP_SEAT,
    stack: list[dict] | None = None,
    battlefield: list[dict] | None = None,
    decision_context: dict | None = None,
    recent_events: list[dict] | None = None,
) -> dict:
    return {
        "turn": {
            "turn_number": 6,
            "phase": phase,
            "step": step,
            "active_player": active,
            "priority_player": LOCAL_SEAT,
        },
        "players": [
            {"seat_id": LOCAL_SEAT, "is_local": True, "life_total": 14, "lands_played": 1},
            {"seat_id": OPP_SEAT, "is_local": False, "life_total": 11, "lands_played": 1},
        ],
        "battlefield": battlefield if battlefield is not None else [],
        "hand": [],
        "stack": stack or [],
        "graveyard": [],
        "legal_actions": ["Pass"],
        "recent_events": recent_events or [],
        "decision_context": decision_context,
    }


def _stack_obj(iid: int, name: str, seat: int, targeting: list[int] | None = None, **kw: Any) -> dict:
    obj = {
        "instance_id": iid,
        "grp_id": 9000 + iid,
        "name": name,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "type_line": "Instant",
        "targeting": targeting or [],
        "oracle_text": "",
    }
    obj.update(kw)
    return obj


def _planner_ctx(state: dict) -> str:
    return _fmt()._format_game_context(state, for_planner=True)


# ── (4) stack rendering ──────────────────────────────────────────────────


def test_empty_stack_emits_nothing() -> None:
    """Zero token cost on the common case."""
    lines = _fmt()._format_stack_section(_state(stack=[]), LOCAL_SEAT)
    assert lines == []


def test_stack_renders_controller_name_and_targets() -> None:
    bear = _creature(101, "Grizzly Bears", LOCAL_SEAT)
    state = _state(
        battlefield=[bear],
        stack=[_stack_obj(401, "Lightning Bolt", OPP_SEAT, targeting=[101])],
    )
    lines = _fmt()._format_stack_section(state, LOCAL_SEAT)
    assert lines[0] == "STACK (top resolves first):"
    assert "OPP" in lines[1]
    assert "Lightning Bolt" in lines[1]
    # The whole point: the model must see WHAT it targets.
    assert "targets: Grizzly Bears" in lines[1]


def test_stack_is_rendered_top_first_not_gre_order() -> None:
    """GRE lists the stack bottom-first; the TOP resolves first.

    The old ``Stack: A > B`` one-liner read as "A then B", the exact
    reverse of resolution order.
    """
    state = _state(
        stack=[
            _stack_obj(401, "Bottom Spell", LOCAL_SEAT),  # cast first, resolves LAST
            _stack_obj(402, "Top Spell", OPP_SEAT),  # cast last, resolves FIRST
        ]
    )
    lines = _fmt()._format_stack_section(state, LOCAL_SEAT)
    assert "Top Spell" in lines[1] and "resolves next" in lines[1]
    assert "Bottom Spell" in lines[2]
    assert "resolves next" not in lines[2]


def test_stack_controller_labels_you_vs_opp() -> None:
    state = _state(
        stack=[
            _stack_obj(401, "Your Spell", LOCAL_SEAT),
            _stack_obj(402, "Their Spell", OPP_SEAT),
        ]
    )
    blob = "\n".join(_fmt()._format_stack_section(state, LOCAL_SEAT))
    assert "OPP Their Spell" in blob
    assert "YOU Your Spell" in blob


def test_stack_controller_prefers_controller_over_owner() -> None:
    """A stolen/copied spell is controlled by someone other than its owner."""
    obj = _stack_obj(401, "Stolen Spell", OPP_SEAT)
    obj["controller_seat_id"] = LOCAL_SEAT  # I control it, opponent owns it
    blob = "\n".join(_fmt()._format_stack_section(_state(stack=[obj]), LOCAL_SEAT))
    assert "YOU Stolen Spell" in blob


def test_unresolvable_target_renders_as_id_not_a_guess() -> None:
    """Better an opaque #id than a confidently wrong 'targets you'."""
    state = _state(stack=[_stack_obj(401, "Bolt", OPP_SEAT, targeting=[99999])])
    blob = "\n".join(_fmt()._format_stack_section(state, LOCAL_SEAT))
    assert "targets: #99999" in blob


def test_stack_renders_chosen_modes_when_present() -> None:
    obj = _stack_obj(401, "Cryptic Command", OPP_SEAT)
    obj["chosen_modes"] = ["Counter target spell", "Draw a card"]
    blob = "\n".join(_fmt()._format_stack_section(_state(stack=[obj]), LOCAL_SEAT))
    assert "mode: Counter target spell; Draw a card" in blob


def test_stack_section_reaches_the_planner_prompt() -> None:
    """End-to-end through the real planner formatter, not just the helper."""
    bear = _creature(101, "Grizzly Bears", LOCAL_SEAT)
    ctx = _planner_ctx(
        _state(
            battlefield=[bear],
            stack=[_stack_obj(401, "Lightning Bolt", OPP_SEAT, targeting=[101])],
        )
    )
    assert "STACK (top resolves first):" in ctx
    assert "targets: Grizzly Bears" in ctx


# ── (4) action history ───────────────────────────────────────────────────


def test_resolution_events_render_as_action_history() -> None:
    ctx = _planner_ctx(
        _state(
            recent_events=[
                {"type": "resolution_complete", "card": "Wolfwillow Haven", "instance_id": 7},
            ]
        )
    )
    assert "Wolfwillow Haven resolved" in ctx


def test_resolution_start_is_suppressed_once_complete_arrives() -> None:
    """'X resolving; X resolved' is pure token waste."""
    ctx = _planner_ctx(
        _state(
            recent_events=[
                {"type": "resolution_start", "card": "Farseek", "instance_id": 7},
                {"type": "resolution_complete", "card": "Farseek", "instance_id": 7},
            ]
        )
    )
    assert "Farseek resolved" in ctx
    assert "Farseek resolving" not in ctx


def test_resolution_start_survives_while_still_resolving() -> None:
    ctx = _planner_ctx(
        _state(recent_events=[{"type": "resolution_start", "card": "Farseek", "instance_id": 7}])
    )
    assert "Farseek resolving" in ctx


def test_unresolved_card_placeholders_are_not_rendered() -> None:
    """'Card#147788 resolved' costs tokens and carries no signal."""
    ctx = _planner_ctx(
        _state(
            recent_events=[
                {"type": "resolution_complete", "card": "Card#147788", "instance_id": 7},
                {"type": "resolution_complete", "card": "Unknown", "instance_id": 8},
            ]
        )
    )
    assert "Card#147788" not in ctx
    assert "resolved" not in ctx


# ── (5) combat solver reaches the prompt ─────────────────────────────────


def _block_state(**kw: Any) -> dict:
    return _state(
        battlefield=[
            _creature(101, "Wall of Omens", LOCAL_SEAT, power=0, toughness=4),
            _creature(102, "Grizzly Bears", LOCAL_SEAT),
            _creature(201, "Goblin Raider", OPP_SEAT, power=3, toughness=1, is_attacking=True),
        ],
        active=OPP_SEAT,
        **kw,
    )


def test_block_solver_line_present_in_canonical_combat_phase() -> None:
    ctx = _planner_ctx(
        _block_state(
            phase="Phase_Combat",
            step="Step_DeclareBlock",
            decision_context={"type": "declare_blockers", "attacker_ids": [201]},
        )
    )
    assert "Computed optimal blocks:" in ctx


def test_block_solver_line_present_when_phase_lacks_combat() -> None:
    """Regression: a GRE declare_blockers decision is authoritative.

    Gating the block solver on ``"Combat" in phase`` alone dropped the
    "Computed optimal blocks:" line whenever the phase string lagged or was
    missing — precisely the decision the solver exists for.
    """
    ctx = _planner_ctx(
        _block_state(
            phase="Phase_Main1",
            step="",
            decision_context={"type": "declare_blockers", "attacker_ids": [201]},
        )
    )
    assert "Computed optimal blocks:" in ctx


def test_attack_solver_line_present_on_your_combat() -> None:
    ctx = _planner_ctx(
        _state(
            phase="Phase_Combat",
            step="Step_DeclareAttack",
            active=LOCAL_SEAT,
            battlefield=[
                _creature(101, "Grizzly Bears", LOCAL_SEAT),
                _creature(201, "Goblin Raider", OPP_SEAT, power=3, toughness=1),
            ],
            decision_context={"type": "declare_attackers", "legal_attackers": ["Grizzly Bears"]},
        )
    )
    assert "Computed optimal attack:" in ctx


# ── (5) solver deference is stated in the system prompt ──────────────────


def test_system_prompt_has_an_explicit_solver_deference_rule() -> None:
    """Before 2026-07-26 the only mention was a voice_advice clause."""
    assert "COMBAT SOLVER" in AUTOPILOT_SYSTEM_PROMPT
    assert "Computed optimal blocks:" in AUTOPILOT_SYSTEM_PROMPT
    assert "Computed optimal attack:" in AUTOPILOT_SYSTEM_PROMPT
    # The instruction must be an obligation, not a hint.
    assert "MUST match that line exactly" in AUTOPILOT_SYSTEM_PROMPT
    # And deviation must be justified, not vibes-based.
    assert "name that specific reason" in AUTOPILOT_SYSTEM_PROMPT


def test_system_prompt_explains_the_stack_section() -> None:
    assert "STACK (top resolves first):" in AUTOPILOT_SYSTEM_PROMPT
    assert "targets" in AUTOPILOT_SYSTEM_PROMPT
    # It must tell the model the response window closes on resolution.
    assert "respond" in AUTOPILOT_SYSTEM_PROMPT.lower()
