"""Actual 814296ac menus, statements and fight objects on baseline 5d59144.

The bundle lacks per-decision planner snapshots. This is a bounded factual
reconstruction from retained raw objects, local oracle and exact choices.
"""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner, SecondLookError
from arenamcp.combat_keywords import ABILITY_KEYWORDS
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.fight_outcomes import fight_claim_concerns, fight_outcomes

EVIDENCE = json.loads((Path(__file__).parent / "fixtures/kogla_fight_814296ac.json").read_text())


def recorded(target=False):
    entry = EVIDENCE["decisions"][int(target)]
    raw_decision = entry["pending_decision"]
    decision = PendingDecision(
        request_id=tuple(raw_decision["request_id"]),
        request_type=raw_decision["request_type"],
        options=tuple(DecisionOption(**o) for o in raw_decision["options"]),
        min_select=raw_decision["min_select"],
        max_select=raw_decision["max_select"],
        can_pass=raw_decision["can_pass"],
        source_label=raw_decision["source_label"],
    )

    def card(iid):
        obj = EVIDENCE["raw_objects_at_251"][str(iid)]
        facts = EVIDENCE["local_card_facts"][str(obj["grpId"])]
        return {
            **facts,
            "instance_id": iid,
            "grp_id": obj["grpId"],
            "power": obj["power"]["value"],
            "toughness": obj["toughness"]["value"],
            "controller_seat_id": obj["controllerSeatId"],
            "owner_seat_id": obj["ownerSeatId"],
            "card_types": obj["cardTypes"],
            "damage": obj.get("damage", 0),
            "keywords": [
                ABILITY_KEYWORDS[a["grpId"]]
                for a in obj.get("uniqueAbilities", [])
                if a["grpId"] in ABILITY_KEYWORDS
            ],
        }

    source = card(871)
    state = {
        "local_seat_id": 1,
        "match_id": "814296ac-3f26-41ff-9d95-08d7ec00736c",
        "turn": {"turn_number": 10, "active_player": 1, "phase": "Main1"},
        "players": [{"seat_id": 1, "life_total": 20}, {"seat_id": 2, "life_total": 20}],
        "battlefield": [card(i) for i in (547, 661, 769)],
        "stack": [],
        "hand": [],
    }
    if target:
        state["battlefield"].append(source)
        state["stack"] = [
            {
                "instance_id": 876,
                "parent_instance_id": 871,
                "object_kind": "ABILITY",
                "controller_seat_id": 1,
                "oracle_text": entry["source_oracle_text"],
            }
        ]
        state["decision_context"] = {"source_id": 876, "source_parent_instance_id": 871}
    else:
        state["hand"] = [{**source, "instance_id": 550}]
    return decision, state, entry


def test_retained_cast_premise_and_actual_reciprocal_damage():
    decision, state, entry = recorded()
    outcomes = fight_outcomes(decision, state)
    warden = next(o for o in outcomes if o.target["instance_id"] == 661)
    assert (warden.source_damage, warden.target_damage) == (7, 6)
    assert warden.source_dies and warden.target_dies
    facts = fight_claim_concerns(decision, state, entry["chosen_options"], entry["reasoning"])
    assert len(facts) == 1 and "not a surviving blocker" in facts[0]


def test_retained_target_premise_hulk_survival_false_but_warden_trade_valid():
    decision, state, entry = recorded(True)
    facts = fight_claim_concerns(decision, state, entry["chosen_options"], entry["reasoning"])
    assert len(facts) == 1 and "Hulk" in facts[0]
    outcomes = {o.target["instance_id"]: o for o in fight_outcomes(decision, state)}
    assert not outcomes[547].target_dies  # GRE-granted indestructible, not merely card text
    assert outcomes[661].source_dies and outcomes[661].target_dies
    assert not fight_claim_concerns(
        decision, state, ["tgt:661"], "Trade Kogla for Warden; both die, removing their counter engine."
    )


@pytest.mark.parametrize("target", [False, True])
def test_exact_false_claim_is_rechecked_and_truthful_same_action_preserved(target):
    decision, state, entry = recorded(target)
    planner = ActionPlanner(backend=Mock())
    planner._last_decision_trace = {}
    picks = entry["chosen_options"]
    revised = {
        "option_ids": picks,
        "reasoning": "Trade Kogla for Warden; both die. Removing their counter engine is worthwhile.",
    }
    ask = Mock(return_value=json.dumps(revised))
    result, chosen = planner._recheck_damage_facts(
        decision,
        state,
        {"reasoning": entry["reasoning"]},
        picks,
        ask,
        lambda raw: (json.loads(raw), json.loads(raw)["option_ids"]),
        "",
    )
    assert ask.call_count == 1 and chosen == picks and result == revised
    assert planner._last_decision_trace["damage_fact_recheck"]["final"] == picks


def test_repeated_false_blocker_promise_uses_existing_current_board_fallback():
    decision, state, entry = recorded()
    planner = ActionPlanner(backend=Mock())
    planner._last_decision_trace = {}
    with pytest.raises(SecondLookError):
        planner._recheck_damage_facts(
            decision,
            state,
            {"reasoning": entry["reasoning"]},
            entry["chosen_options"],
            Mock(side_effect=RuntimeError("offline")),
            Mock(),
            "",
        )
    assert planner._last_decision_trace["damage_fact_recheck"]["fallback"] == "current_board"


def test_keywords_marked_damage_and_current_modified_stats():
    decision, state, _ = recorded(True)
    source = state["battlefield"][-1]
    source["keywords"] = ["indestructible"]
    assert not next(o for o in fight_outcomes(decision, state) if o.target["instance_id"] == 661).source_dies
    source["keywords"] = ["first strike", "double strike"]
    assert next(o for o in fight_outcomes(decision, state) if o.target["instance_id"] == 661).source_dies
    source["modified_toughness"] = 8
    assert not next(o for o in fight_outcomes(decision, state) if o.target["instance_id"] == 661).source_dies
    source["damage"] = 2
    assert next(o for o in fight_outcomes(decision, state) if o.target["instance_id"] == 661).source_dies
    source["damage"] = 0
    state["battlefield"][1]["keywords"] = ["deathtouch"]
    assert next(o for o in fight_outcomes(decision, state) if o.target["instance_id"] == 661).source_dies


@pytest.mark.parametrize(
    "change",
    ["unknown_stats", "missing_parent", "new_namesake", "other_stack", "prevention", "noncreature", "phased"],
)
def test_unsupported_or_changed_identity_never_invents_fight_outcome(change):
    decision, state, _ = recorded(True)
    source = state["battlefield"][-1]
    if change == "unknown_stats":
        source["modified_toughness"] = None
    elif change == "missing_parent":
        state["stack"][0].pop("parent_instance_id")
        state["decision_context"].pop("source_parent_instance_id")
    elif change == "new_namesake":
        source["instance_id"] = 999
    elif change == "other_stack":
        state["stack"].append({"instance_id": 999, "oracle_text": "Target creature gets +3/+3."})
    elif change == "prevention":
        source["oracle_text"] += "\nPrevent all damage that would be dealt to this creature."
    elif change == "noncreature":
        source["card_types"] = ["CardType_Enchantment"]
    elif change == "phased":
        source["is_phased_out"] = True
    assert not fight_outcomes(decision, state)


def test_conditional_protection_and_negative_survival_claims_are_not_rejected():
    decision, state, entry = recorded()
    for reason in (
        "Kogla's fight trades for Warden; it does not leave a blocker.",
        "If I protect Kogla with Safekeeping, the fight kills Warden and leaves a blocker.",
        "Kogla dies fighting Bill, who survives.",
    ):
        assert not fight_claim_concerns(decision, state, entry["chosen_options"], reason)


def test_typed_cast_path_compares_reciprocal_damage_before_committing(monkeypatch):
    from tests.test_typed_decision_context import _CapturingBackend

    decision, state, entry = recorded()
    backend = _CapturingBackend(
        json.dumps({"option_ids": entry["chosen_options"], "reasoning": entry["reasoning"]})
    )
    planner = ActionPlanner(backend=backend)
    monkeypatch.setattr(planner, "_line_prompt_context", lambda *a: (None, "", {}))
    monkeypatch.setattr(planner, "_grounded_plan_block", lambda *a, **kw: "")
    with pytest.raises(SecondLookError):
        planner._llm_decision_options(decision, state)
    assert any("simultaneously deals 6 back" in p for p in backend.prompts)
    assert sum("DAMAGE FACT RECHECK" in p for p in backend.prompts) == 1


def test_zero_power_deathtouch_does_not_invent_reciprocal_damage():
    decision, state, _ = recorded(True)
    target = state["battlefield"][1]
    target["modified_power"] = 0
    target["keywords"] = ["deathtouch"]
    outcome = next(o for o in fight_outcomes(decision, state) if o.target["instance_id"] == 661)
    assert outcome.target_damage == 0 and not outcome.source_dies
