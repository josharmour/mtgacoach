"""CastingTimeOptions with the model down finishes the cast (ledger #121) and says so (#119).

#121, standalone.log 2026-10-19 (match 56fc394b, Cloudflare outage 13:05 to 13:07):
the line pick cast Burst Lightning ("Kill Essence Channeler with Burst
Lightning first"). At its kicker step:

    13:05:20.687 plan_decision_options LLM path failed: no first token within 8.0s
    13:05:20.710 typed-decision CastingTimeOptions: declined (cancel)
    13:05:20.710 Self-cancel guard: cast 'burst lightning' was cancelled by the
                 autopilot (CastingTimeOptions cancelled); withholding it for the rest of turn 6

The same happened at 13:06:18, 13:06:42 and 13:07:20. The menu below is that
game's own (match packet 56fc394b, request [300, 420]): Done (idx:0) and
Kicker (idx:1, sourceId 408, {R} plus four generic).

#119, 12:48:55 (match 2c67eed8): Done was spoken as "Passing", and "resolve
Burst Lightning for 2" in the rationale became "Passing priority Burst
Lightning for 2".
"""

from __future__ import annotations

import copy
import time
from types import SimpleNamespace
from typing import Any

from tests.test_llm_down_fallback import _Backend, _Bridge, _engine

from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner
from arenamcp.casting_options import casting_fallback_choice, casting_option_speech
from arenamcp.decisions import DecisionOption, PendingDecision, build_pending_decision
from arenamcp.narration import submission_narration

LOCAL, OPP = 1, 2
BURST = 408
BURST_TEXT = (
    "Kicker {o4}\nCARDNAME deals 2 damage to any target. If this spell was kicked, it deals 4 damage instead."
)

# The bridge's CastingTimeOptions request for Burst Lightning #408 (match packet 56fc394b).
POLL = {
    "has_pending": True,
    "request_type": "CastingTimeOptions",
    "request_class": "CastingTimeOptionRequest",
    "game_state_id": 300,
    "msg_id": 420,
    "can_pass": True,
    "can_cancel": True,
    "actions": [
        {
            "actionType": "CastingTimeOption",
            "choiceKind": "done",
            "requestClass": "CastingTimeOption_DoneRequest",
            "childIndex": 0,
            "label": "Done",
            "manaCost": [{"color": '[ "Red" ]', "count": 1}],
        },
        {
            "actionType": "CastingTimeOption",
            "choiceKind": "kicker",
            "requestClass": "CastingTimeOption_KickerRequest",
            "childIndex": 1,
            "label": "Kicker",
            "grpId": 2852,
            "sourceId": BURST,
            "manaCost": [{"color": '[ "Red" ]', "count": 1}, {"color": '[ "Generic" ]', "count": 4}],
        },
    ],
}


def _decision() -> PendingDecision:
    return build_pending_decision(copy.deepcopy(POLL))


def _land(iid: int) -> dict[str, Any]:
    return {
        "instance_id": iid,
        "name": "Mountain",
        "type_line": "Basic Land — Mountain",
        "oracle_text": "({T}: Add {R}.)",
        "card_types": ["CardType_Land"],
        "controller_seat_id": LOCAL,
        "owner_seat_id": LOCAL,
        "is_tapped": False,
        "turn_entered_battlefield": 1,
    }


def _creature(iid: int, name: str, power: int, toughness: int) -> dict[str, Any]:
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Creature — Bat",
        "card_types": ["CardType_Creature"],
        "power": power,
        "toughness": toughness,
        "controller_seat_id": OPP,
        "owner_seat_id": OPP,
        "turn_entered_battlefield": 3,
    }


def _state(mountains: int = 3, their_life: int = 16, creatures: list[dict] | None = None) -> dict[str, Any]:
    """T6 13:05:12: Burst Lightning #408 on the stack, our untapped Mountains, their board."""
    burst = {
        "instance_id": BURST,
        "grp_id": 93905,
        "name": "Burst Lightning",
        "type_line": "Instant",
        "mana_cost": "{R}",
        "oracle_text": BURST_TEXT,
        "object_kind": "CARD",
        "controller_seat_id": LOCAL,
        "owner_seat_id": LOCAL,
    }
    return {
        "local_seat_id": LOCAL,
        "opponent_seat_id": OPP,
        "turn": {"turn_number": 6, "active_player": LOCAL, "priority_player": LOCAL, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": LOCAL, "life_total": 20, "is_local": True},
            {"seat_id": OPP, "life_total": their_life, "is_local": False},
        ],
        "battlefield": [_land(340 + n) for n in range(mountains)]
        + (creatures if creatures is not None else [_creature(301, "Essence Channeler", 2, 1)]),
        "hand": [],
        "graveyard": [],
        "stack": [burst],
        "pending_decision": "Casting Option",
        "decision_context": {"type": "casting_time_options"},
    }


def _down_planner() -> tuple[ActionPlanner, _Backend]:
    backend = _Backend(error=AssertionError("the model must not be called"), available=False)
    return ActionPlanner(backend=backend, timeout=15.0), backend


def _intend(planner: ActionPlanner, target: str, target_id: int = 0) -> None:
    """What _remember_cast_intent keeps when the line pick casts Burst Lightning."""
    planner._cast_intent = {
        "card": "Burst Lightning",
        "said": "Kill Essence Channeler with Burst Lightning first",
        "line_target": target,
        "line_target_id": target_id,
        "at": time.monotonic(),
    }


# --- #121: the planner's fallback --------------------------------------------------------


def test_model_down_casts_burst_unkicked_instead_of_cancelling():
    planner, backend = _down_planner()
    _intend(planner, "Essence Channeler", 301)
    state = _state(mountains=3)
    picked = planner.plan_decision_options(_decision(), state)
    assert picked == ["idx:0"]  # Done: cast it as started
    assert backend.calls == []
    assert planner.last_llm_failure == "unavailable"
    assert planner.get_decision_reasoning(picked).startswith(
        "Model unavailable; cast Burst Lightning unkicked"
    )


def test_unaffordable_kicker_is_never_taken_even_when_lethal():
    planner, _ = _down_planner()
    _intend(planner, "opponent")
    # Kicked Burst would be lethal at 4 life, but 4 Mountains cannot pay {4}{R}.
    assert planner.plan_decision_options(_decision(), _state(mountains=4, their_life=4)) == ["idx:0"]


def test_affordable_kicker_is_taken_when_the_line_needs_it_for_lethal():
    planner, _ = _down_planner()
    _intend(planner, "opponent")
    picked = planner.plan_decision_options(_decision(), _state(mountains=5, their_life=4))
    assert picked == ["idx:1"]
    assert "lethal (they are at 4)" in planner.get_decision_reasoning(picked)
    # Plain damage already lethal: no reason to spend the kicker.
    planner, _ = _down_planner()
    _intend(planner, "opponent")
    assert planner.plan_decision_options(_decision(), _state(mountains=5, their_life=2)) == ["idx:0"]


def test_affordable_kicker_is_taken_for_a_line_target_only_four_damage_kills():
    ogre = _creature(302, "Sim Ogre", 3, 3)
    planner, _ = _down_planner()
    _intend(planner, "Sim Ogre", 302)
    assert planner.plan_decision_options(_decision(), _state(mountains=5, creatures=[ogre])) == ["idx:1"]
    # The line's target dies to 2: Done keeps the mana for the rest of the line.
    planner, _ = _down_planner()
    _intend(planner, "Essence Channeler", 301)
    assert planner.plan_decision_options(_decision(), _state(mountains=5)) == ["idx:0"]
    # No remembered line target: cast normally.
    planner, _ = _down_planner()
    assert planner.plan_decision_options(_decision(), _state(mountains=5, creatures=[ogre])) == ["idx:0"]


def test_a_model_timeout_mid_outage_casts_too():
    """13:05:20: the call timed out (FirstTokenTimeout) before the circuit opened."""
    from arenamcp.backends.proxy import BackendError

    planner = ActionPlanner(
        backend=_Backend(
            error=BackendError("no first token within 8.0s (FirstTokenTimeout)", kind="first_token_timeout")
        ),
        timeout=15.0,
    )
    picked = planner.plan_decision_options(_decision(), _state(mountains=3))
    assert picked == ["idx:0"]
    assert planner.last_llm_failure == "unavailable"


def _modal_decision(min_select: int = 1) -> PendingDecision:
    """Iroh's Demonstration's two modes (one casting child), as the Mac bridge sends them."""
    meta = {"choiceKind": "modal", "childIndex": 0, "sourceId": 410, "min": min_select, "max": min_select}
    return PendingDecision(
        (310, 430),
        "CastingTimeOptions",
        (
            DecisionOption(
                "idx:0",
                "Mode 1: deals 1 damage to each creature your opponents control.",
                meta=dict(meta, optionIndex=0),
            ),
            DecisionOption(
                "idx:1", "Mode 2: deals 4 damage to target creature.", meta=dict(meta, optionIndex=1)
            ),
        ),
        min_select=min_select,
        max_select=min_select,
        can_cancel=True,
    )


def test_a_required_mode_without_a_line_is_the_first_legal_mode():
    assert casting_fallback_choice(_modal_decision(), {})[0] == ["idx:0"]
    # "Choose two": both modes of that child together, never a cancel.
    assert casting_fallback_choice(_modal_decision(min_select=2), {})[0] == ["idx:0", "idx:1"]


def test_a_required_mode_follows_the_per_mode_line_search():
    def line(score: tuple, outcome: str) -> SimpleNamespace:
        return SimpleNamespace(score=score, outcome_text=lambda: outcome)

    comparison = SimpleNamespace(
        complete=True,
        contingent=[],
        modes={"idx:0": "1 to each of theirs", "idx:1": "4 damage to target creature"},
        lines={
            "idx:0": line((1, 0, 3.0), "survives"),
            "idx:1": line((1, 0, 5.0), "survives, kills Trostani"),
        },
    )
    picked, why = casting_fallback_choice(_modal_decision(), {}, comparison=comparison)
    assert picked == ["idx:1"]
    assert "4 damage to target creature" in why
    # An unmodelled mode makes the comparison no guide: first legal mode.
    comparison.modes["idx:0"] = "other"
    assert casting_fallback_choice(_modal_decision(), {}, comparison=comparison)[0] == ["idx:0"]


def test_x_takes_the_largest_value_our_mana_pays_for():
    meta = {"choiceKind": "numeric_input", "childIndex": 0, "sourceId": 411}
    decision = PendingDecision(
        (320, 440),
        "CastingTimeOptions",
        tuple(DecisionOption(f"idx:{x}", f"X = {x}", meta=dict(meta, numericValue=x)) for x in range(6)),
        can_cancel=True,
    )
    state = _state(mountains=3)
    state["stack"] = [
        {
            "instance_id": 411,
            "name": "Sim Blaze",
            "type_line": "Sorcery",
            "mana_cost": "{X}{R}",
            "oracle_text": "Sim Blaze deals X damage to any target.",
            "controller_seat_id": LOCAL,
            "owner_seat_id": LOCAL,
        }
    ]
    assert casting_fallback_choice(decision, state)[0] == ["idx:2"]  # 3 Mountains: {R} plus X=2


# --- #121: the autopilot submits it -------------------------------------------------------


class _CastingBridge(_Bridge):
    def __init__(self, pending):
        super().__init__(pending)
        self.submitted: list = []
        self.cancelled = False

    def submit_action_by_index(self, index, expected=None):
        self.submitted.append((index, (expected or {}).get("choiceKind")))
        return True

    def cancel_action(self):
        self.cancelled = True
        return True


def test_autopilot_submits_done_while_the_circuit_is_open(monkeypatch):
    state = _state(mountains=3)
    state.update(_bridge_connected=True, _bridge_request_type="CastingTimeOptions")
    bridge = _CastingBridge(copy.deepcopy(POLL))
    planner, backend = _down_planner()
    _intend(planner, "Essence Channeler", 301)
    engine = _engine(monkeypatch, planner, bridge, state)
    spoken: list[str] = []
    engine._notify = lambda kind, text: spoken.append(text)
    assert engine._try_typed_decision_path(copy.deepcopy(state), "decision_required") is True
    assert bridge.submitted == [(0, "done")]
    assert not bridge.cancelled
    assert backend.calls == []
    assert spoken and spoken[-1].startswith("Casting Burst Lightning unkicked.")


def test_the_decline_sentinel_is_gone_from_the_mechanical_pick():
    assert ActionPlanner.deterministic_option_pick(_decision()) == ["idx:0"]
    assert ActionPlanner.deterministic_option_pick(_modal_decision()) == ["idx:0"]
    assert DECLINE_DECISION not in ActionPlanner.deterministic_option_pick(_modal_decision(min_select=2))


# --- #119: the spoken words -------------------------------------------------------------


def test_done_is_spoken_as_the_cast_not_a_pass():
    decision, state = _decision(), _state()
    assert casting_option_speech(decision, ["idx:0"], state) == "Cast Burst Lightning unkicked."
    assert casting_option_speech(decision, ["idx:1"], state) == "Cast Burst Lightning kicked."
    # A mode keeps its own label.
    assert casting_option_speech(_modal_decision(), ["idx:1"], {}) == ""


def test_resolve_in_the_rationale_is_not_rewritten():
    # 12:48:55: the model's reasoning for Done on Burst Lightning's kicker step.
    reasoning = (
        "Only 3 mana is reachable this turn, so the {4}{R} kicker is unpayable; "
        "resolve Burst Lightning for 2 to kill Helpful Hunter."
    )
    spoken = submission_narration("Cast Burst Lightning unkicked.", reasoning)
    assert spoken == (
        "Casting Burst Lightning unkicked. Only 3 mana is reachable this turn, so the {4}{R} kicker is "
        "unpayable; resolve Burst Lightning for 2 to kill Helpful Hunter."
    )
    assert "Passing" not in spoken
    # The action label itself still says what Arena's Resolve does.
    assert submission_narration("Resolve.").startswith("Passing priority")
