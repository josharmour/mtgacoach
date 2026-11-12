"""An adjective-form "exact lethal" claim the board math contradicts is corrected (2026-11-11 T9).

19:08:36: "Lightning Strike to the face plus pumped Slickshot (3 flying) and
Cindermaw's trample gives 10 damage — exact lethal on their 7 life even if
Dazzling Angel chumps Slickshot." Opponent sat at 7 with three untapped
blockers; the best line was lethal on T11, not this turn. The noun-adjective
form ("exact lethal") slipped the exactly/precisely patterns, so no correction
fired and the attack plan inherited the false claim. The target step was then
cancelled mid-planning and the deterministic fallback picked a creature while
nothing recorded that divergence.

Fixture: the VERBATIM recorded turn-9 Strike-decision state_snapshot — Hired
Claw is still on the stack (controller seat 1) when the Strike decision runs,
exactly as recorded. The correction must fire on that real state, not on a
normalized copy with the stack drained.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from arenamcp import board_assessment
from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision, TargetSlot

CLAIM = (
    "Lightning Strike to the face plus pumped Slickshot (3 flying) and Cindermaw's trample "
    "gives 10 damage — exact lethal on their 7 life even if Dazzling Angel chumps Slickshot."
)

FIXTURE = Path(__file__).parent / "fixtures" / "plan_consistency_20261112_turn9_strike.json"

LOCAL, OPP = 1, 2


def _fixture_state():
    return json.loads(FIXTURE.read_text())["state"]


def _qualified_facts(**overrides):
    """Mocked facts that pass the stack gate (clean line search, nothing unmodelled)."""
    base = dict(
        lethal_now=False,
        posture="attack",
        lines=[],
        opp_life=7,
        line_search=SimpleNamespace(truncated=False),
        pending=[],
        unmodelled=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# --- claim detection ------------------------------------------------------------------------


def test_adjective_exact_lethal_claim_is_detected():
    planner = ActionPlanner.__new__(ActionPlanner)
    assert planner._lethal_claims(CLAIM) == [CLAIM]
    # The pre-existing adverb forms still match.
    assert planner._lethal_claims("this is exactly lethal this turn")
    assert planner._lethal_claims("this is precisely lethal this turn")


def test_negated_or_later_turn_wording_is_not_flagged():
    planner = ActionPlanner.__new__(ActionPlanner)
    assert not planner._lethal_claims("this is not exact lethal yet")
    assert not planner._lethal_claims("holding Strike for exact lethal next turn")


# --- recorded-board correction (real path: Hired Claw still on the stack) --------------------


def test_recorded_turn9_exact_lethal_claim_is_corrected_from_real_board():
    state = _fixture_state()
    # The recorded snapshot really does carry the pending cast on the stack.
    assert [card.get("name") for card in state.get("stack") or []] == ["Hired Claw"]

    facts = board_assessment.assess(state)
    assert facts.lethal_now is False  # the recorded claim was false on this board
    # The assessment qualifies itself on this state: it accounted for the whole
    # stack and found nothing it could not value.
    assert facts.pending == []
    assert facts.unmodelled == []

    planner = ActionPlanner.__new__(ActionPlanner)
    planner._last_decision_reasoning = CLAIM
    planner._last_decision_trace = {}

    planner._correct_false_lethal_claim(state, None)

    reasoning = planner._last_decision_reasoning
    assert "exact lethal" not in reasoning
    assert reasoning.startswith("Board math: not lethal this turn (they are at 7;")
    assert "T11" in reasoning  # the real best line, from the recorded state
    trace = planner.get_last_decision_trace()
    assert trace["false_lethal_claim"] == [CLAIM]
    # Decision memory keeps the model's intent plus the correction (2026-10-19:
    # stripping the intent made the target step aim at a 5/5 instead of "the face").
    kept = planner._false_lethal_memory[1]
    assert kept.startswith("Lightning Strike to the face")
    assert "Board math:" in kept


def test_true_exact_lethal_claim_survives(monkeypatch):
    line = SimpleNamespace(win_at=2, win_turn=9)
    facts = _qualified_facts(lethal_now=True, lines=[line])
    monkeypatch.setattr(board_assessment, "assess", lambda state: facts)

    planner = ActionPlanner.__new__(ActionPlanner)
    planner._last_decision_reasoning = CLAIM
    planner._last_decision_trace = {}

    planner._correct_false_lethal_claim(_fixture_state(), None)

    assert planner._last_decision_reasoning == CLAIM
    assert "false_lethal_claim" not in planner.get_last_decision_trace()


# --- conservative gate: unqualified facts never strip a claim --------------------------------


def test_correction_skips_when_line_search_did_not_run(monkeypatch):
    monkeypatch.setattr(board_assessment, "_line_search_enabled", lambda: False)

    state = _fixture_state()
    facts = board_assessment.assess(state)
    assert facts.line_search is None  # search off: no winning-line evidence at all

    planner = ActionPlanner.__new__(ActionPlanner)
    planner._last_decision_reasoning = CLAIM
    planner._last_decision_trace = {}

    planner._correct_false_lethal_claim(state, None)

    assert planner._last_decision_reasoning == CLAIM
    assert "false_lethal_claim" not in planner.get_last_decision_trace()


def test_correction_skips_while_assessment_flags_unmodelled_stack(monkeypatch):
    facts = _qualified_facts(pending=["Hired Claw (resolving)"])
    monkeypatch.setattr(board_assessment, "assess", lambda state: facts)

    planner = ActionPlanner.__new__(ActionPlanner)
    planner._last_decision_reasoning = CLAIM
    planner._last_decision_trace = {}

    planner._correct_false_lethal_claim(_fixture_state(), None)

    assert planner._last_decision_reasoning == CLAIM
    assert "false_lethal_claim" not in planner.get_last_decision_trace()


def test_correction_skips_on_truncated_line_search(monkeypatch):
    facts = _qualified_facts(line_search=SimpleNamespace(truncated=True))
    monkeypatch.setattr(board_assessment, "assess", lambda state: facts)

    planner = ActionPlanner.__new__(ActionPlanner)
    planner._last_decision_reasoning = CLAIM
    planner._last_decision_trace = {}

    planner._correct_false_lethal_claim(_fixture_state(), None)

    assert planner._last_decision_reasoning == CLAIM
    assert "false_lethal_claim" not in planner.get_last_decision_trace()


# --- generic counter-block validation --------------------------------------------------------
# Synthetic boards with invented names: a valid counter-block must hold the
# damage below lethal (so an unconditional exact-lethal claim is stripped),
# and removing the blockers must flip the same board to lethal this turn.


def _synth_counter_block_state(blockers=True):
    real = _fixture_state()
    lands = [card for card in real["battlefield"] if str(card.get("type_line", "")).startswith("Land")]
    state = {
        "match_id": "synthetic-counter-block",
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "decision_seat_id": 1,
        "turn": {"turn_number": 9, "active_player": 1, "priority_player": 1, "phase": "Main1"},
        "players": [
            {"seat_id": 2, "life_total": 7, "is_local": False},
            {"seat_id": 1, "life_total": 20, "is_local": True},
        ],
        "battlefield": list(lands)
        + [
            {
                "instance_id": 700,
                "grp_id": 800700,
                "name": "Storm Raptor",
                "type_line": "Creature — Bird",
                "power": 3,
                "toughness": 2,
                "keywords": ["Flying"],
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "is_tapped": False,
                "turn_entered_battlefield": 5,
                "object_kind": "CARD",
            },
            {
                "instance_id": 701,
                "grp_id": 800701,
                "name": "Oak Colossus",
                "type_line": "Creature — Treefolk",
                "power": 4,
                "toughness": 3,
                "keywords": ["Trample"],
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "is_tapped": False,
                "turn_entered_battlefield": 5,
                "object_kind": "CARD",
            },
        ],
        "hand": [
            {
                "instance_id": 750,
                "grp_id": 800750,
                "name": "Ember Bolt",
                "type_line": "Instant",
                "mana_cost": "{1}{R}",
                "oracle_text": "Ember Bolt deals 3 damage to any target.",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
            }
        ],
        "graveyard": [],
        "exile": [],
        "stack": [],
    }
    if blockers:
        state["battlefield"] += [
            {
                "instance_id": 800,
                "grp_id": 800800,
                "name": "Guard Crane",
                "type_line": "Creature — Bird",
                "power": 2,
                "toughness": 3,
                "keywords": ["Flying"],
                "controller_seat_id": 2,
                "owner_seat_id": 2,
                "is_tapped": False,
                "turn_entered_battlefield": 5,
                "object_kind": "CARD",
            },
            {
                "instance_id": 801,
                "grp_id": 800801,
                "name": "Stone Warden",
                "type_line": "Creature — Golem",
                "power": 2,
                "toughness": 2,
                "keywords": [],
                "controller_seat_id": 2,
                "owner_seat_id": 2,
                "is_tapped": False,
                "turn_entered_battlefield": 5,
                "object_kind": "CARD",
            },
            {
                "instance_id": 802,
                "grp_id": 800802,
                "name": "Field Medic",
                "type_line": "Creature — Cleric",
                "power": 3,
                "toughness": 1,
                "keywords": [],
                "controller_seat_id": 2,
                "owner_seat_id": 2,
                "is_tapped": False,
                "turn_entered_battlefield": 5,
                "object_kind": "CARD",
            },
        ]
    return state


def _generic_claim(damage):
    return (
        f"Attacking with Storm Raptor and Oak Colossus plus Ember Bolt to the face is "
        f"{damage} damage — exact lethal on their {damage} life."
    )


def test_counter_block_prevents_unconditional_exact_lethal_claim():
    state = _synth_counter_block_state(blockers=True)
    facts = board_assessment.assess(state)
    assert facts.lethal_now is False

    claim = _generic_claim(facts.opp_life)
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._last_decision_reasoning = claim
    planner._last_decision_trace = {}

    planner._correct_false_lethal_claim(state, None)

    assert f"exact lethal on their {facts.opp_life} life" not in planner._last_decision_reasoning
    assert planner.get_last_decision_trace()["false_lethal_claim"] == [claim]


def test_without_blockers_the_same_lethal_claim_stands():
    state = _synth_counter_block_state(blockers=False)
    facts = board_assessment.assess(state)
    assert facts.lethal_now is True

    claim = _generic_claim(facts.opp_life)
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._last_decision_reasoning = claim
    planner._last_decision_trace = {}

    planner._correct_false_lethal_claim(state, None)

    assert f"exact lethal on their {facts.opp_life} life" in planner._last_decision_reasoning


# --- target-fallback provenance + plan-commitment refresh ------------------------------------


class _CancelledBackend:
    """Model server that reports the request changed during planning."""

    def __init__(self):
        self.calls = 0

    def available(self):
        return True

    def complete(self, system, user, *args, **kwargs):
        self.calls += 1
        raise ValueError("request changed during planning")


def _creature(iid, name, power, toughness, seat=OPP):
    return {
        "instance_id": iid,
        "grp_id": 90000 + iid,
        "name": name,
        "type_line": "Creature — Angel",
        "power": power,
        "toughness": toughness,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "is_tapped": False,
        "turn_entered_battlefield": 5,
    }


STRIKE_ORACLE = "Lightning Strike deals 3 damage to any target."


def _spell(iid, name, type_line, cost, oracle, seat=LOCAL):
    return {
        "instance_id": iid,
        "grp_id": 90000 + iid,
        "name": name,
        "type_line": type_line,
        "mana_cost": cost,
        "oracle_text": oracle,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
    }


def _target_state():
    return {
        "match_id": "fe9d1f07",
        "local_seat_id": LOCAL,
        "turn": {"turn_number": 9, "active_player": LOCAL, "priority_player": LOCAL, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": OPP, "life_total": 7, "is_local": False},
            {"seat_id": LOCAL, "life_total": 20, "is_local": True},
        ],
        "battlefield": [_creature(334, "Dazzling Angel", 2, 3)],
        "hand": [],
        "graveyard": [],
        "exile": [],
        "stack": [_spell(900001, "Lightning Strike", "Instant", "{1}{R}", STRIKE_ORACLE)],
        "_bridge_request_payload": {"sourceId": 900001},
        "decision_context": {"type": "target_selection", "source_id": 900001},
    }


def _target_decision():
    return PendingDecision(
        request_id=(276, 376),
        request_type="SelectTargets",
        options=(
            DecisionOption("tgt:327", "Sun-Blessed Healer", meta={"instanceId": 327}),
            DecisionOption("tgt:334", "Dazzling Angel", meta={"instanceId": 334}),
        ),
        min_select=1,
        max_select=1,
        can_cancel=True,
        source_label="Lightning Strike",
        slots=(TargetSlot(1, 1, 1, 0, (327, 334)),),
    )


def test_selecttargets_fallback_after_cancellation_records_provenance():
    backend = _CancelledBackend()
    planner = ActionPlanner(backend, timeout=5, land_drop_first=False)
    state = _target_state()

    picked = planner.plan_decision_options(_target_decision(), state)

    trace = planner.get_last_decision_trace()
    assert trace["llm_failure"] == "bad_answer"
    # The deterministic fallback chose the target and the trace says so — a
    # cancelled target step leaves no completed decision record otherwise.
    assert trace["targeting_fallback"] == picked


FACE_CLAIM = (
    "Lightning Strike to the face plus pumped Slickshot (3 flying) and Cindermaw's trample "
    "gives 10 damage — exact lethal on their 7 life even if Dazzling Angel chumps Slickshot."
)


def test_fallback_target_divergence_reaches_decision_memory():
    """Fallback picked a creature while the cast reasoning promised the face.

    The attack step builds its prompt from decision memory; if the fallback's
    pick never lands there with reasoning naming the real target, the plan
    commits to a line nobody played (2026-11-11 T9).
    """
    backend = _CancelledBackend()
    planner = ActionPlanner(backend, timeout=5, land_drop_first=False)
    decision = _target_decision()

    picked = planner.plan_decision_options(decision, _target_state())

    trace = planner.get_last_decision_trace()
    assert trace["targeting_fallback"] == picked
    labels = {opt.option_id: opt.label for opt in decision.options}
    picked_labels = [labels[opt_id] for opt_id in picked]
    # The refresh names what was actually submitted…
    assert trace["target_refresh"] == (
        f"Deterministic fallback: Lightning Strike went to {' and '.join(picked_labels)}."
    )
    # …and it reached decision memory, so the attack step sees the real target.
    lines = planner.decisions_for_turn(9)
    assert any(
        "Deterministic fallback: Lightning Strike went to" in line
        and all(label in line for label in picked_labels)
        for line in lines
    )


def test_fallback_refresh_skipped_when_request_was_superseded():
    """A superseded (cancelled) pick is discarded by the caller — memory must not keep it."""
    import threading

    backend = _CancelledBackend()
    planner = ActionPlanner(backend, timeout=5, land_drop_first=False)
    cancel_event = threading.Event()
    cancel_event.set()
    planner._decision_cancel_event = cancel_event

    picked = planner.plan_decision_options(_target_decision(), _target_state())

    trace = planner.get_last_decision_trace()
    assert trace["targeting_fallback"] == picked
    assert "target_refresh" not in trace
    assert not any("Deterministic fallback" in line for line in planner.decisions_for_turn(9))
