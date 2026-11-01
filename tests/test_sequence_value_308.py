"""G04's supplied mechanics, not an invented replay of its missing full packet."""

import json
from copy import deepcopy

import pytest
from tests.test_public_sequence_308 import state
from tests.test_tactical_timing_backlog import card, cast, menu

from arenamcp.sequence_value import sequence_values


def position():
    s = state()
    s["battlefield"][1].update(power=1, toughness=1)
    s["battlefield"].append(
        card(95, "Orc Army", "", "Token Creature — Orc Army", seat=2, power=2, toughness=2)
    )
    return s


def test_helix_branches_preserve_life_and_discard_instead_of_calling_it_a_blank():
    s = position()
    values = sequence_values(menu(cast(30)), s)
    elf = [v for v in values if "Protected creature" in v.play]
    assert len(elf) == 2
    assert {v.life for v in elf} == {3}
    assert [(v.removed, v.survives, v.opponent_discards, v.tapped) for v in elf] == [
        ((90,), (), 0, ()),
        ((), (90,), 1, (90,)),
    ]
    punisher = [v for v in values if "Draw punisher" in v.play]
    assert len(punisher) == 1 and punisher[0].removed == (80,) and punisher[0].life == 3
    # No arbitrary preference between a live threat/discard and another removed engine.
    assert all(not hasattr(v, "score") for v in values)


def test_unknown_use_has_both_branches_and_punisher_has_both_relevant_targets():
    s = position()
    original = deepcopy(s)
    values = sequence_values(menu(cast(31)), s)
    assert len(values) == 3
    assert any(v.draws == 0 and v.survives == (31,) and v.army_counters == 0 for v in values)
    assert any(v.draws == 1 and v.removed == (31,) and v.army_counters == 1 and v.life == 0 for v in values)
    assert any(v.draws == 1 and v.survives == (31,) and v.army_counters == 1 and v.life == -1 for v in values)
    assert s == original


def test_observed_use_removes_draw_and_amass_values_without_discouraging_body():
    values = sequence_values(menu(cast(31)), position(), used_draw_triggers={70})
    assert len(values) == 1
    assert values[0].draws == values[0].army_counters == values[0].life == 0
    assert values[0].survives == (31,)


def test_dead_punisher_does_not_punish_later_entry():
    s = position()
    s["battlefield"] = [c for c in s["battlefield"] if c["instance_id"] != 80]
    assert sequence_values(menu(cast(31)), s) == ()


@pytest.mark.parametrize("keyword", ["Hexproof", "Shroud", "Indestructible"])
def test_creature_protection_preserves_legitimate_draw_line(keyword):
    s = position()
    s["hand"][1]["oracle_text"] = keyword
    values = sequence_values(menu(cast(31)), s)
    assert values and not any(v.removed for v in values)
    assert any(v.draws == 1 and v.survives == (31,) for v in values)


def test_two_toughness_and_above_power_cap_do_not_invent_a_dead_body():
    s = position()
    s["hand"][1]["toughness"] = 2
    assert not any(v.removed for v in sequence_values(menu(cast(31)), s))
    s["hand"][1]["power"] = 3
    assert not sequence_values(menu(cast(31)), s)


@pytest.mark.parametrize("hand", [None, 0])
def test_no_discard_availability_assertion_without_known_card(hand):
    s = position()
    s["opponent_hand_count"] = hand
    assert not any(v.opponent_discards for v in sequence_values(menu(cast(30)), s))


@pytest.mark.parametrize(
    "text",
    [
        "Prevent all damage that would be dealt to creatures you control.",
        "Creatures you control get +1/+1.",
        "Your opponents can't gain life.",
        "If you would draw a card, instead gain 1 life.",
        "Creatures entering the battlefield don't cause abilities to trigger.",
    ],
)
def test_unmodeled_replacements_lords_and_suppression_abstain(text):
    s = position()
    s["battlefield"].append(card(99, "Modifier", text, "Enchantment"))
    assert not sequence_values(menu(cast(30), cast(31)), s)


def test_unknown_stats_extra_engines_and_entry_effect_abstain():
    s = position()
    s["hand"][1].pop("power")
    assert not sequence_values(menu(cast(31)), s)
    s = position()
    s["battlefield"].append({**s["battlefield"][0], "instance_id": 71})
    assert not sequence_values(menu(cast(31)), s)
    s = position()
    s["hand"][1]["oracle_text"] = "When Small creature enters, you gain 2 life."
    assert not sequence_values(menu(cast(31)), s)


def test_values_reach_initial_typed_prompt_and_trace_even_with_acknowledged_tradeoff(monkeypatch):
    from tests.test_typed_decision_context import _CapturingBackend

    from arenamcp.action_planner import ActionPlanner

    backend = _CapturingBackend(
        json.dumps(
            {
                "option_ids": ["idx:31"],
                "reasoning": "Accept Draw punisher damage and amass for the card; this creature is expendable.",
            }
        )
    )
    planner = ActionPlanner(backend=backend)
    monkeypatch.setattr(planner, "_line_prompt_context", lambda *a: (None, "", {}))
    monkeypatch.setattr(planner, "_grounded_plan_block", lambda *a, **kw: "")
    assert planner._llm_decision_options(menu(cast(30), cast(31)), position()) == ["idx:31"]
    assert all("BOUNDED PUBLIC OUTCOMES" in p for p in backend.prompts)
    assert any("opponent discards 1" in p and "removed IDs [31]" in p for p in backend.prompts)
    values = planner._last_decision_trace["public_sequence_values"]
    assert any(v["removed"] == (31,) and v["draws"] == 1 for v in values)
    json.dumps(values)  # evidence is serializable, not just prose


def test_recorded_g04_names_and_oracle_shapes_retain_the_measured_tradeoff():
    # These are the audit's public mechanics, not its unavailable complete board.
    s = position()
    s["battlefield"][0].update(
        name="Enduring Innocence",
        oracle_text=(
            "Lifelink\nWhenever one or more other creatures you control with power 2 or less enter, draw a card. "
            "This ability triggers only once each turn.\nWhen Enduring Innocence dies, if it was a creature, "
            "return it to the battlefield under its owner's control. It's an enchantment."
        ),
    )
    s["battlefield"][1].update(
        name="Orcish Bowmasters",
        oracle_text=(
            "Flash\nWhen Orcish Bowmasters enters the battlefield and whenever an opponent draws a card "
            "except the first one they draw in each of their draw steps, Orcish Bowmasters deals 1 damage "
            "to any target. Then amass Orcs 1."
        ),
    )
    s["battlefield"][2].update(
        name="Iron-Shield Elf",
        oracle_text=("Discard a card: Iron-Shield Elf gains indestructible until end of turn. Tap it."),
    )
    s["hand"][0].update(
        name="Lightning Helix", oracle_text=("Lightning Helix deals 3 damage to any target. You gain 3 life.")
    )
    s["hand"][1].update(
        name="Grim Lavamancer",
        oracle_text=(
            "{R}, {T}, Exile two cards from your graveyard: Grim Lavamancer deals 2 damage to any target."
        ),
    )
    values = sequence_values(menu(cast(30), cast(31)), s)
    assert any(v.opponent_discards == 1 and v.life == 3 and v.survives == (90,) for v in values)
    assert any(v.removed == (80,) and v.life == 3 for v in values)
    assert any(v.removed == (31,) and v.draws == 1 and v.army_counters == 1 for v in values)
    # A remaining burn activation is not credited to the newly dead Lavamancer.
    assert all(not hasattr(v, "future_damage") for v in values)
