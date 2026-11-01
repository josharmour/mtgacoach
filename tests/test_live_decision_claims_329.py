"""Retained #329 menus/reasons with bounded public-state reconstructions.

The match bundles contain decisions but replay_path=None, not complete input
snapshots. Source rules below were checked against the installed Arena card DB.
Raw 9d135049 state337 supplies stack1061; 9376b418 state326 proves land902→904.
"""

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner, SecondLookError
from arenamcp.decision_claims import action_claim_mismatch
from arenamcp.decision_consistency import consistent_action
from arenamcp.decisions import DecisionOption, PendingDecision

FIXTURES = Path(__file__).parent / "fixtures" / "live_329"
SAFEKEEPING = {
    "name": "Tamiyo's Safekeeping",
    "grp_id": 79648,
    "instance_id": 972,
    "type_line": "Instant",
    "mana_cost": "{G}",
    "oracle_text": "Target permanent you control gains hexproof and indestructible until end of turn. You gain 2 life.",
}
TOOTH = {
    "name": "Tooth and Nail",
    "grp_id": 57281,
    "instance_id": 834,
    "type_line": "Sorcery",
    "mana_cost": "{5}{G}{G}",
    "oracle_text": "Choose one —\nSearch your library for up to two creature cards, reveal them, put them into your hand, then shuffle.\nPut up to two creature cards from your hand onto the battlefield.\nEntwine {o2}",
}
LAND = {
    "name": "Tanglespan Bridgeworks",
    "grp_id": 90825,
    "type_line": "Land",
    "oracle_text": "As this land enters, you may pay 3 life. If you don't, it enters tapped.\n{oT}: Add {oG}.",
}
FRONT = {
    "name": "Bridgeworks Battle",
    "grp_id": 90824,
    "instance_id": 902,
    "type_line": "Sorcery",
    "mana_cost": "{2}{G}",
    "oracle_text": "Target creature you control gets +2/+2 until end of turn. It fights up to one target creature you don't control.",
    "related_faces": [LAND],
}


def retained(stem, index=0):
    record = json.loads((FIXTURES / (stem + ".json")).read_text())["records"][index]
    raw = record["pending_decision"]
    decision = PendingDecision(
        **{
            **raw,
            "request_id": tuple(raw["request_id"]),
            "options": tuple(DecisionOption(**o) for o in raw["options"]),
        }
    )
    local = record["local_seat_id"]
    life, opponent_life = (
        (12, 32)
        if stem.startswith("9d")
        else (34, 23)
        if stem.startswith("811")
        else (26 if index == 0 else 23, 20)
    )
    source = SAFEKEEPING if stem.startswith("9d") else TOOTH if stem.startswith("811") else FRONT
    state = {
        "match_id": stem[:8],
        "local_seat_id": local,
        "turn": record["turn"],
        "players": [
            {"seat_id": local, "is_local": True, "life_total": life},
            {"seat_id": 3 - local, "life_total": opponent_life},
        ],
        "hand": [{**deepcopy(source), "controller_seat_id": local, "owner_seat_id": local}],
        "battlefield": [],
        "command": [],
        "stack": [],
    }
    if stem.startswith("9d"):
        state["stack"] = [
            {
                "instance_id": 1061,
                "grp_id": 107785,
                "controller_seat_id": 2,
                "name": "Change the Narrative",
                "type_line": "Enchantment",
                "oracle_text": "Planeswalker spells you cast cost {o1} less to cast.\nAt the beginning of your upkeep, conjure a random planeswalker card into your hand. It perpetually gains prismatic.",
            }
        ]
    return decision, state, record["chosen_options"], record["reasoning"]


@pytest.mark.parametrize("stem", ["9d135049-protection", "811c7117-cast-hold", "9376b418-mdfc"])
def test_retained_cases_bypass_old_guard_but_receive_factual_recheck(stem):
    d, s, chosen, reason = retained(stem)
    assert consistent_action(d, s, chosen, reason) is None
    issue = action_claim_mismatch(d, s, chosen, reason)
    assert issue
    if stem.startswith("9d"):
        assert "You gain 2 life" in issue.reason
        assert s["stack"][0]["name"] == "Change the Narrative"
    elif stem.startswith("811"):
        assert "Search your library" in issue.reason and "Entwine" in issue.reason
    else:
        assert "Tanglespan Bridgeworks" in issue.land_fact


@pytest.mark.parametrize(
    "reason",
    [
        "Cast Tamiyo's Safekeeping now for 2 life; it moves us out of burn range.",
        "Cast Tamiyo's Safekeeping to protect our blocker from the removal currently on the stack.",
        "Cast Tamiyo's Safekeeping for the prowess trigger, then hold the other copy.",
        "If holding Tamiyo's Safekeeping were useful I would pass, but use it for life now.",
        "Do not hold Tamiyo's Safekeeping; use it now.",
        "Hold Tooth and Nail for next turn; protect our creature now.",
        "After this cast, hold Tamiyo's Safekeeping's second copy for their turn.",
    ],
)
def test_legitimate_current_cast_future_holds_and_other_cards_are_unchanged(reason):
    d, s, chosen, _ = retained("9d135049-protection")
    assert action_claim_mismatch(d, s, chosen, reason) is None


@pytest.mark.parametrize(
    "reason",
    [
        "Play this land to cast the commander next turn.",
        "Play Tanglespan Bridgeworks now, then cast the commander if it becomes available.",
        "Do not cast the commander; play the land.",
        "If casting the commander were offered, choose it; play this land now.",
        "Casting the commander next turn needs this land now.",
        "Cast the commander after playing this land and resolving its entry payment.",
    ],
)
def test_land_can_support_a_future_cast_without_claiming_to_be_one(reason):
    d, s, chosen, _ = retained("9376b418-mdfc")
    assert action_claim_mismatch(d, s, chosen, reason) is None


def parse(reply):
    return reply, reply["option_ids"]


@pytest.mark.parametrize(
    ("stem", "reason"),
    [
        (
            "9d135049-protection",
            "Cast Tamiyo's Safekeeping now for the 2 life; the protection is incidental.",
        ),
        (
            "811c7117-cast-hold",
            "Cast Tooth and Nail with entwine to search for two creatures, then put them onto the battlefield.",
        ),
    ],
)
def test_one_recheck_can_retain_same_cast_for_real_value(stem, reason):
    d, s, chosen, original = retained(stem)
    planner = ActionPlanner(backend=Mock())
    planner._last_decision_trace = {}
    ask = Mock(return_value={"option_ids": chosen, "reasoning": reason})
    data, picks = planner._recheck_action_claim(d, s, {"reasoning": original}, chosen, ask, parse, "prompt")
    assert picks == chosen and data["reasoning"] == reason
    ask.assert_called_once()
    assert "actual rules" in ask.call_args.args[0]


def test_cast_recheck_may_hold_and_unresolved_cast_uses_current_board_fallback():
    d, s, chosen, reason = retained("9d135049-protection")
    planner = ActionPlanner(backend=Mock())
    planner._last_decision_trace = {}
    revised = {
        "option_ids": ["pass"],
        "reasoning": "Hold Safekeeping; no immediate protection or life gain is needed.",
    }
    assert planner._recheck_action_claim(
        d, s, {"reasoning": reason}, chosen, Mock(return_value=revised), parse, "p"
    )[1] == ["pass"]
    ask = Mock(return_value={"option_ids": chosen, "reasoning": reason})
    with pytest.raises(SecondLookError, match="Cast/hold rationale unresolved"):
        planner._recheck_action_claim(d, s, {"reasoning": reason}, chosen, ask, parse, "p")


@pytest.mark.parametrize("failure", ["repeat", "timeout", "illegal"])
def test_land_fallback_preserves_play_and_removes_fictitious_cast_from_memory(failure):
    d, s, chosen, reason = retained("9376b418-mdfc")
    planner = ActionPlanner(backend=Mock())
    planner._last_decision_trace = {}
    ask = Mock(
        return_value={"option_ids": chosen if failure != "illegal" else ["idx:999"], "reasoning": reason}
    )
    if failure == "timeout":
        ask.side_effect = TimeoutError("bounded recheck expired")
    data, picks = planner._recheck_action_claim(d, s, {"reasoning": reason}, chosen, ask, parse, "p")
    assert picks == chosen
    assert "Play Tanglespan Bridgeworks as a land" in data["reasoning"]
    planner._memory().record(s, d, picks, data["reasoning"])
    memory = planner.recent_decisions_text()
    assert "creates three bodies" not in memory and "Casting the commander" not in memory
    assert "does not cast the commander" in memory
    assert planner._last_decision_trace["action_claim_check"]["original_reasoning"] == reason


def test_unknown_source_or_mismatched_face_abstains():
    d, s, chosen, reason = retained("9376b418-mdfc")
    s["hand"][0]["related_faces"] = []
    assert action_claim_mismatch(d, s, chosen, reason) is None
    s["hand"] = []
    assert action_claim_mismatch(d, s, chosen, reason) is None


def test_actual_typed_pipeline_records_land_identity_before_later_prompt(monkeypatch):
    from tests.test_typed_decision_context import _CapturingBackend

    d, s, chosen, reason = retained("9376b418-mdfc")
    backend = _CapturingBackend(json.dumps({"option_ids": chosen, "reasoning": reason}))
    planner = ActionPlanner(backend=backend)
    monkeypatch.setattr(planner, "_line_prompt_context", lambda *args: (None, "", {}))
    monkeypatch.setattr(planner, "_grounded_plan_block", lambda *args, **kwargs: "")
    assert planner.plan_decision_options(d, s) == chosen
    assert "action_claim_check" in planner._last_decision_trace
    assert "does not cast the commander" in planner.recent_decisions_text()
    assert "creates three bodies" not in planner.recent_decisions_text()
    # The next retained menu passes; feed a factual new rationale to inspect
    # its real prompt. This does not claim every possible false history is solved.
    later, after, picks, _ = retained("9376b418-mdfc", 2)
    after["hand"] = []
    after["battlefield"] = [{**LAND, "instance_id": 904, "controller_seat_id": 2}]
    backend.reply = json.dumps({"option_ids": picks, "reasoning": "Pass after the land play."})
    assert planner.plan_decision_options(later, after) == picks
    assert "does not cast the commander" in backend.prompts[-1]
    assert "creates three bodies" not in backend.prompts[-1]
