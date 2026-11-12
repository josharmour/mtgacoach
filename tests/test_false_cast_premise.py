"""Fable Nov 2026-11-12: impossible cast premises re-evaluate the pick, not the prose.

Recorded autoplay decisions rested on premises the rules contradict: a
summoning-sick creature claimed to attack this turn (Skyward Spider T8,
Spellbook Vendor T12) and a creature with no entry/cast ability claimed to put
an Eerie counter on another creature immediately (Optimistic Scavenger T16).
The pre-existing summoning-sickness validator only rewrote the reasoning; the
selected action stood. These tests pin the new deterministic detector
(:mod:`arenamcp.cast_premises`) and its second-look wiring through the typed
decision boundary, offline with mocked model outputs.
"""

import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner, SecondLookError
from arenamcp.cast_premises import _classify_haste_clause, cast_premise_concerns
from arenamcp.decisions import decision_from_dict
from arenamcp.game_plan import decision_claim_notes

FIXTURE = json.loads((Path(__file__).parent / "fixtures/false_cast_premise_20261112.json").read_text())
CASES = {case["name"]: case for case in FIXTURE["cases"]}


def _decision(name):
    return decision_from_dict(CASES[name]["decision"])


def _cast_instance_id(name):
    case = CASES[name]
    chosen = case["chosen_option_ids"][0]
    for option in case["decision"]["options"]:
        if option["option_id"] == chosen:
            return option["meta"].get("instanceId")
    raise AssertionError(f"no cast option among {case['chosen_option_ids']}")


@pytest.mark.parametrize("name", ["skyward_spider_t8", "spellbook_vendor_t12"])
def test_recorded_summoning_sick_attack_premise_is_flagged(name):
    """Original failure: the pick's stated outcome is impossible this turn."""
    case = CASES[name]
    concerns = cast_premise_concerns(
        _decision(name), case["state"], case["chosen_option_ids"], case["reasoning"]
    )
    assert len(concerns) == 1
    assert "summoning sick" in concerns[0]
    assert "can't attack" in concerns[0]


def test_recorded_false_entry_effect_premise_is_flagged():
    """Original failure: an Eerie counter attributed to the creature's own entry."""
    case = CASES["optimistic_scavenger_t16"]
    concerns = cast_premise_concerns(
        _decision("optimistic_scavenger_t16"),
        case["state"],
        case["chosen_option_ids"],
        case["reasoning"],
    )
    assert len(concerns) == 1
    assert "no enters-the-battlefield or when-you-cast ability" in concerns[0]
    assert "Eerie" in concerns[0]


def test_preexisting_validator_caught_the_sick_claim_but_left_the_pick():
    """Documents the regression baseline: rationale rewritten, action unchanged."""
    case = CASES["skyward_spider_t8"]
    notes = decision_claim_notes(
        case["reasoning"], case["state"], cast_ids={_cast_instance_id("skyward_spider_t8")}
    )
    assert notes and "summoning sick" in notes[0]
    # The recorded post-correction explanation keeps the same chosen option.
    # Embedded from the retained evidence record (private absolute evidence
    # path deliberately not read at test time).
    recorded_explanation = (
        "Casting Skyward Spider. Rules correction: Skyward Spider is summoning "
        "sick: it can't attack or activate its own {T}/{Q} abilities this turn."
    )
    assert "Casting Skyward Spider" in recorded_explanation
    assert "summoning sick" in recorded_explanation


def test_genuine_haste_attack_now_is_not_flagged():
    """Adversarial positive: a hasty creature really can attack this turn."""
    case = copy.deepcopy(CASES["skyward_spider_t8"])
    for card in case["state"]["hand"]:
        if card.get("instance_id") == _cast_instance_id("skyward_spider_t8"):
            card["oracle_text"] = (card.get("oracle_text") or "") + "\nHaste"
    assert (
        cast_premise_concerns(
            _decision("skyward_spider_t8"),
            case["state"],
            case["chosen_option_ids"],
            case["reasoning"],
        )
        == []
    )


def test_other_unsick_creature_attacking_is_not_flagged():
    """Adversarial positive: an existing untapped creature may attack freely."""
    case = CASES["optimistic_scavenger_t16"]
    reasoning = (
        "Casting Optimistic Scavenger. Veteran Survivor attacks this turn; "
        "the Scavenger blocks from next turn onward."
    )
    assert (
        cast_premise_concerns(
            _decision("optimistic_scavenger_t16"),
            case["state"],
            case["chosen_option_ids"],
            reasoning,
        )
        == []
    )


def test_ordinary_resolving_consequences_are_not_entry_effect_claims():
    """Review round 3: ordinary consequences of a creature resolving —
    becoming a blocker, raising the creature count, static board impact —
    need no ability, so generic immediacy must never refute them."""
    case = CASES["optimistic_scavenger_t16"]
    for reasoning in (
        "Casting Optimistic Scavenger immediately gives us another blocker.",
        "Casting Optimistic Scavenger immediately adds one to our creature count.",
        "Casting Optimistic Scavenger immediately makes our board much harder to attack into.",
    ):
        concerns = cast_premise_concerns(
            _decision("optimistic_scavenger_t16"),
            case["state"],
            case["chosen_option_ids"],
            reasoning,
        )
        # Only the entry-effect class is under test here; the pre-existing
        # summoning-sickness validator (premise 1) is unchanged by this fix.
        entry_effects = [c for c in concerns if "does not produce that effect by itself" in c]
        assert entry_effects == [], (reasoning, entry_effects)


@pytest.mark.parametrize(
    "reasoning",
    [
        "Casting Optimistic Scavenger immediately puts a +1/+1 counter on their best creature.",
        "Casting Optimistic Scavenger first places a stun counter on their biggest attacker.",
        "Casting Optimistic Scavenger immediately adds a counter to Veteran Survivor.",
    ],
)
def test_counter_placement_claims_are_still_refuted(reasoning):
    """Guard against over-narrowing: a counter-placement claim attributed to
    the cast itself is still refuted when nothing supports it. Counters are
    the one effect class static continuous effects cannot produce without an
    event, so the impossibility is positively established."""
    case = CASES["optimistic_scavenger_t16"]
    concerns = cast_premise_concerns(
        _decision("optimistic_scavenger_t16"),
        case["state"],
        case["chosen_option_ids"],
        reasoning,
    )
    assert len(concerns) == 1
    assert "no enters-the-battlefield or when-you-cast ability" in concerns[0]


def test_static_buff_via_token_mention_is_not_refuted():
    """Review round 4 blocker: Master of Etherium's own static ability makes
    'immediately gives our artifact creature tokens +1/+1' TRUE with no
    trigger. Mentioning tokens (as buff recipients) does not assert token
    creation, and stat buffs need no entry/when-cast ability."""
    case = copy.deepcopy(CASES["optimistic_scavenger_t16"])
    for card in case["state"]["hand"]:
        if card.get("instance_id") == _cast_instance_id("optimistic_scavenger_t16"):
            card["name"] = "Master of Etherium"
            card["type_line"] = "Artifact Creature — Construct"
            card["oracle_text"] = "Other artifact creatures you control get +1/+1."
    reasoning = "Casting Master of Etherium immediately gives our artifact creature tokens +1/+1."
    concerns = cast_premise_concerns(
        _decision("optimistic_scavenger_t16"),
        case["state"],
        case["chosen_option_ids"],
        reasoning,
    )
    entry_effects = [c for c in concerns if "does not produce that effect by itself" in c]
    assert entry_effects == [], (reasoning, entry_effects)


def test_servo_token_on_board_does_not_make_buff_a_creation_claim():
    """Same probe with an explicit Servo token on the battlefield: the token
    noun in the reasoning refers to the existing token, so the claim stays a
    static buff and is never refuted."""
    case = copy.deepcopy(CASES["optimistic_scavenger_t16"])
    for card in case["state"]["hand"]:
        if card.get("instance_id") == _cast_instance_id("optimistic_scavenger_t16"):
            card["name"] = "Master of Etherium"
            card["type_line"] = "Artifact Creature — Construct"
            card["oracle_text"] = "Other artifact creatures you control get +1/+1."
    case["state"]["battlefield"].append(
        {
            "zoneId": "Battlefield",
            "instance_id": 900008,
            "name": "Servo",
            "type_line": "Artifact Creature — Servo",
            "oracle_text": "",
        }
    )
    reasoning = "Casting Master of Etherium immediately gives our artifact creature tokens +1/+1."
    concerns = cast_premise_concerns(
        _decision("optimistic_scavenger_t16"),
        case["state"],
        case["chosen_option_ids"],
        reasoning,
    )
    entry_effects = [c for c in concerns if "does not produce that effect by itself" in c]
    assert entry_effects == [], (reasoning, entry_effects)


@pytest.mark.parametrize(
    "reasoning",
    [
        "Casting Optimistic Scavenger immediately creates a Treasure token.",
        "Casting Optimistic Scavenger first draws us a card.",
        "Casting Optimistic Scavenger immediately deals 2 damage to their best creature.",
        "Casting Optimistic Scavenger immediately gives our other creatures +1/+1.",
    ],
)
def test_unrepresented_effect_claims_stay_unknown_not_refuted(reasoning):
    """Review round 4 conservatism: token/card/damage creation and stat-buff
    claims are left UNKNOWN (a static or unrepresented effect could support
    them) — the detector never refutes what it cannot positively disprove."""
    case = CASES["optimistic_scavenger_t16"]
    concerns = cast_premise_concerns(
        _decision("optimistic_scavenger_t16"),
        case["state"],
        case["chosen_option_ids"],
        reasoning,
    )
    entry_effects = [c for c in concerns if "does not produce that effect by itself" in c]
    assert entry_effects == [], (reasoning, entry_effects)


def test_future_timed_trigger_claim_is_out_of_scope():
    """Conservative scope: scheduled triggers are not immediate-entry claims."""
    case = CASES["optimistic_scavenger_t16"]
    reasoning = (
        "Casting Optimistic Scavenger; at the beginning of combat it creates "
        "a Role token for Veteran Survivor."
    )
    assert (
        cast_premise_concerns(
            _decision("optimistic_scavenger_t16"),
            case["state"],
            case["chosen_option_ids"],
            reasoning,
        )
        == []
    )


def test_enchantment_entry_counter_claim_is_not_attributed_to_the_cast():
    """Adversarial positive: a real Eerie enchantment entry owns that trigger."""
    case = CASES["optimistic_scavenger_t16"]
    reasoning = (
        "Casting Optimistic Scavenger. When the Eerie enchantment enters later, "
        "it puts a +1/+1 counter on Veteran Survivor."
    )
    assert (
        cast_premise_concerns(
            _decision("optimistic_scavenger_t16"),
            case["state"],
            case["chosen_option_ids"],
            reasoning,
        )
        == []
    )


def test_unknown_oracle_text_yields_no_entry_effect_concern():
    """Unknown oracle text stays conservative: no concern is invented."""
    case = copy.deepcopy(CASES["optimistic_scavenger_t16"])
    for card in case["state"]["hand"]:
        if card.get("instance_id") == _cast_instance_id("optimistic_scavenger_t16"):
            card["oracle_text"] = ""
    assert (
        cast_premise_concerns(
            _decision("optimistic_scavenger_t16"),
            case["state"],
            case["chosen_option_ids"],
            case["reasoning"],
        )
        == []
    )


def test_visible_cast_trigger_support_suppresses_entry_premise():
    """External support: a Beast Whisperer draw really fires on this cast."""
    case = copy.deepcopy(CASES["optimistic_scavenger_t16"])
    case["state"]["battlefield"].append(
        {
            "zoneId": "Battlefield",
            "instance_id": 900001,
            "name": "Beast Whisperer",
            "type_line": "Creature — Elf Druid",
            "oracle_text": "Whenever you cast a creature spell, draw a card.",
        }
    )
    assert (
        cast_premise_concerns(
            _decision("optimistic_scavenger_t16"),
            case["state"],
            case["chosen_option_ids"],
            case["reasoning"],
        )
        == []
    )


def test_visible_enter_trigger_matching_types_suppresses_entry_premise():
    """External support: a permanent triggering on creature entry covers the cast."""
    case = copy.deepcopy(CASES["optimistic_scavenger_t16"])
    case["state"]["battlefield"].append(
        {
            "zoneId": "Battlefield",
            "instance_id": 900002,
            "name": "Entry Sage",
            "type_line": "Creature — Wizard",
            "oracle_text": "Whenever a creature enters, draw a card.",
        }
    )
    assert (
        cast_premise_concerns(
            _decision("optimistic_scavenger_t16"),
            case["state"],
            case["chosen_option_ids"],
            case["reasoning"],
        )
        == []
    )


def test_enter_trigger_on_other_types_does_not_support_the_cast():
    """Precision: a land-entry trigger does not rescue a creature-cast claim."""
    case = copy.deepcopy(CASES["optimistic_scavenger_t16"])
    case["state"]["battlefield"].append(
        {
            "zoneId": "Battlefield",
            "instance_id": 900003,
            "name": "Landfall Loremaster",
            "type_line": "Creature — Wizard",
            "oracle_text": "Whenever a land enters, draw a card.",
        }
    )
    concerns = cast_premise_concerns(
        _decision("optimistic_scavenger_t16"),
        case["state"],
        case["chosen_option_ids"],
        case["reasoning"],
    )
    assert len(concerns) == 1
    assert "no enters-the-battlefield or when-you-cast ability" in concerns[0]


def test_global_haste_grantor_suppresses_sick_attack_premise():
    """External support: 'Creatures you control have haste' backs the attack."""
    case = copy.deepcopy(CASES["skyward_spider_t8"])
    case["state"]["battlefield"].append(
        {
            "zoneId": "Battlefield",
            "instance_id": 900004,
            "name": "Fervent Banner",
            "type_line": "Artifact — Equipment",
            "oracle_text": "Creatures you control have haste.",
        }
    )
    assert (
        cast_premise_concerns(
            _decision("skyward_spider_t8"),
            case["state"],
            case["chosen_option_ids"],
            case["reasoning"],
        )
        == []
    )


def test_scoped_haste_wording_stays_conservative_like_the_claim_validator():
    """'Enchanted creature ... has haste' is scoped support, not a global grant.

    The pre-existing claim validator (_keyword_truth) already treats any
    haste-granting battlefield text as possibly applying, so the summoning-sick
    fact is withheld and no concern may be invented — the detector matches.
    """
    case = copy.deepcopy(CASES["skyward_spider_t8"])
    case["state"]["battlefield"].append(
        {
            "zoneId": "Battlefield",
            "instance_id": 900005,
            "name": "Scrapyard Steel",
            "type_line": "Enchantment — Aura",
            "oracle_text": "Enchanted creature gets +1/+1 and has haste.",
        }
    )
    assert (
        cast_premise_concerns(
            _decision("skyward_spider_t8"),
            case["state"],
            case["chosen_option_ids"],
            case["reasoning"],
        )
        == []
    )


def test_haste_classifier_distinguishes_global_from_scoped_grants():
    """Unit precision behind the board scan: global=True, scoped/self=False."""
    assert _classify_haste_clause("creatures you control have haste", "") is True
    assert _classify_haste_clause("permanents you control gain haste", "") is True
    assert _classify_haste_clause("target creature gains haste until end of turn", "") is True
    assert _classify_haste_clause("enchanted creature gets +1/+1 and has haste", "") is False
    assert _classify_haste_clause("scrapyard steel has haste", "scrapyard steel") is False
    assert _classify_haste_clause("haste", "") is False
    assert _classify_haste_clause("whenever a land enters, draw a card", "") is False


def test_unreadable_battlefield_oracle_keeps_conservative_silence():
    """A battlefield object without oracle text suppresses both premise classes."""
    entry_case = copy.deepcopy(CASES["optimistic_scavenger_t16"])
    entry_case["state"]["battlefield"].append(
        {"zoneId": "Battlefield", "instance_id": 900006, "name": "Mystery Permanent"}
    )
    assert (
        cast_premise_concerns(
            _decision("optimistic_scavenger_t16"),
            entry_case["state"],
            entry_case["chosen_option_ids"],
            entry_case["reasoning"],
        )
        == []
    )
    sick_case = copy.deepcopy(CASES["skyward_spider_t8"])
    sick_case["state"]["battlefield"].append(
        {"zoneId": "Battlefield", "instance_id": 900007, "name": "Mystery Permanent"}
    )
    assert (
        cast_premise_concerns(
            _decision("skyward_spider_t8"),
            sick_case["state"],
            sick_case["chosen_option_ids"],
            sick_case["reasoning"],
        )
        == []
    )


def _concerns_for(case):
    decision = _decision(case["name"])

    def concerns_for(chosen, data):
        return [
            ("false_cast_premise", text)
            for text in cast_premise_concerns(decision, case["state"], chosen, data.get("reasoning"))
        ]

    return decision, concerns_for


def test_second_look_reasks_false_cast_premise_and_clean_revision_stands(monkeypatch):
    """Through the planner: bad premise forces one re-ask; the revision stands."""
    case = CASES["skyward_spider_t8"]
    decision, concerns_for = _concerns_for(case)
    backend = Mock()
    calls = []

    def complete(*args, **kwargs):
        calls.append(args[1])
        if len(calls) == 1:
            return json.dumps(
                {
                    "option_ids": case["chosen_option_ids"],
                    "reasoning": case["reasoning"],
                }
            )
        return json.dumps(
            {
                "option_ids": case["chosen_option_ids"],
                "reasoning": "Casting Skyward Spider develops a flier; it blocks from next turn.",
            }
        )

    backend.complete.side_effect = complete
    planner = ActionPlanner(backend, separate_strategy_planning=True)
    monkeypatch.setattr(planner, "_sized_budget", lambda *args: 1.0)
    assert planner.plan_decision_options(decision, case["state"]) == case["chosen_option_ids"]
    assert len(calls) == 2
    assert "SECOND LOOK" in calls[1]
    assert "summoning sick" in calls[1]
    trace = planner._last_decision_trace["second_look"]
    assert any("summoning sick" in text for text in trace["concerns"])
    assert trace.get("final") != "fallback"


def test_lingering_premise_in_revision_goes_to_explicit_fallback():
    """A revised answer still resting on the premise cannot stand: fallback."""
    case = CASES["optimistic_scavenger_t16"]
    decision, concerns_for = _concerns_for(case)
    planner = ActionPlanner(Mock(), separate_strategy_planning=True)
    planner._last_decision_trace = {}
    data = {
        "option_ids": case["chosen_option_ids"],
        "reasoning": case["reasoning"],
    }

    def ask(message, **kwargs):
        return json.dumps(data)

    def parse(raw):
        payload = json.loads(raw)
        return payload, payload["option_ids"]

    with pytest.raises(SecondLookError) as excinfo:
        planner._second_look(
            decision,
            case["state"],
            data,
            case["chosen_option_ids"],
            concerns_for,
            ask,
            parse,
            "pick",
            False,
        )
    assert "false cast premise" in str(excinfo.value)
    trace = planner._last_decision_trace["second_look"]
    assert trace["final"] == "fallback"
    assert trace["refuted_claims"]


def test_high_effort_pick_with_false_premise_is_reasked_not_rubber_stamped():
    """High-effort picks are re-asked over a false premise; clean answer stands."""
    case = CASES["spellbook_vendor_t12"]
    decision, concerns_for = _concerns_for(case)
    planner = ActionPlanner(Mock(), separate_strategy_planning=True)
    planner._last_decision_trace = {}
    bad_data = {
        "option_ids": case["chosen_option_ids"],
        "reasoning": case["reasoning"],
    }
    good_data = {
        "option_ids": case["chosen_option_ids"],
        "reasoning": "Casting Spellbook Vendor builds a body and a Role; it blocks from next turn.",
    }
    answers = iter([json.dumps(good_data)])

    def ask(message, **kwargs):
        return next(answers)

    def parse(raw):
        payload = json.loads(raw)
        return payload, payload["option_ids"]

    new_data, new_chosen = planner._second_look(
        decision,
        case["state"],
        bad_data,
        case["chosen_option_ids"],
        concerns_for,
        ask,
        parse,
        "pick",
        True,
    )
    assert new_chosen == case["chosen_option_ids"]
    assert new_data["reasoning"] == good_data["reasoning"]


def test_high_effort_pick_still_on_premise_raises_instead_of_standing():
    """Same as above but the revision repeats the premise: explicit fallback."""
    case = CASES["spellbook_vendor_t12"]
    decision, concerns_for = _concerns_for(case)
    planner = ActionPlanner(Mock(), separate_strategy_planning=True)
    planner._last_decision_trace = {}
    data = {
        "option_ids": case["chosen_option_ids"],
        "reasoning": case["reasoning"],
    }

    def ask(message, **kwargs):
        return json.dumps(data)

    def parse(raw):
        payload = json.loads(raw)
        return payload, payload["option_ids"]

    with pytest.raises(SecondLookError):
        planner._second_look(
            decision,
            case["state"],
            data,
            case["chosen_option_ids"],
            concerns_for,
            ask,
            parse,
            "pick",
            True,
        )


def test_plan_decision_options_repeated_false_response_falls_back_deterministically(
    monkeypatch,
):
    """Submission boundary: a revision that repeats the false premise never stands.

    Through ``plan_decision_options`` itself: the first answer rests on the
    impossible premise, the re-asked answer repeats it, and the planner takes
    the deterministic board-math fallback (``last_llm_failure == "set_aside"``)
    instead of submitting the premise-backed pick.
    """
    case = CASES["skyward_spider_t8"]
    decision, concerns_for = _concerns_for(case)
    backend = Mock()
    calls = []

    def complete(*args, **kwargs):
        calls.append(args[1])
        return json.dumps(
            {
                "option_ids": case["chosen_option_ids"],
                "reasoning": case["reasoning"],
            }
        )

    backend.complete.side_effect = complete
    planner = ActionPlanner(backend, separate_strategy_planning=True)
    monkeypatch.setattr(planner, "_sized_budget", lambda *args: 1.0)
    result = planner.plan_decision_options(decision, case["state"])

    # The premise-backed answer was re-asked exactly once, then set aside.
    assert len(calls) == 2
    assert "SECOND LOOK" in calls[1]
    assert planner.last_llm_failure == "set_aside"
    trace = planner._last_decision_trace
    assert trace["llm_failure"] == "set_aside"
    assert trace["second_look"]["final"] == "fallback"
    assert trace["second_look"]["refuted_claims"]
    # Whatever the deterministic fallback returned, it is a legal selection.
    if result:
        assert decision.selection_is_valid(result)
