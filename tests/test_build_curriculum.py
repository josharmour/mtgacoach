"""Unit tests for the strategic-cast curriculum stager.

The staging is only trustworthy if the prompt parser is: every complexity
component is derived from the rendered prompt, so a silent parse regression
would produce a corpus of zeros that still "works". These tests pin the parse
shape, the approximate affordability model, and the score/stage mechanics.
No corpus and no card index are read.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.training.build_curriculum import (  # noqa: E402
    WEIGHTS,
    ParseError,
    assign_stages,
    auc,
    complexity_components,
    complexity_score,
    cost_pips,
    decision_features,
    is_affordable,
    parse_prompt,
    parse_sources,
    permanent_counts,
    quantile,
    reflex_pick,
)

PROMPT = """TRIGGER: Land played. What's the next play?

=== GAME ===
Candidate: (pick by number — RECONSTRUCTED CASTS ONLY, not a complete legal-action list)
  1. Cast Brightspear Zealot
  2. Cast Desculpting Blast
  3. Cast Wurmwall Sweeper
T4 YOUR | Main1 | Pri:You
Timing: ALL SPELLS
Life: You=18 Opp=15
Mana: 3 (lands: 4, sources: W U {B/G} W) [land played this turn entered TAPPED]
Land: ALREADY PLAYED this turn (Island) — no land action in this decision
On the play.
Opp hand: 5 cards

YOUR BOARD:
  Station Monitor 2/2 (Creature — Lizard Artificer) - Whenever you cast your second spell each turn, create a token.
  Drone x2 1/1 (Token Artifact Creature — Drone) - Flying
  Plains x3 (Basic Land — Plains) - ({T}: Add {W}.)

OPP BOARD:
  Sunstar Chaplain 3/2 (Creature — Human Cleric) - At the beginning of your end step, put a counter on target creature.
  Island x2 (Basic Land — Island) - ({T}: Add {U}.)

HAND:
  Brightspear Zealot {2}{W} 2/4 (Creature — Human Soldier) - Vigilance
  Desculpting Blast {1}{U} (Instant) - Return target nonland permanent to its owner's hand.
  Wurmwall Sweeper {2} 2/2 (Artifact — Spacecraft) - When this Spacecraft enters, surveil 2.
"""

CARDS = {
    "brightspear zealot": {
        "t": "Vigilance",
        "k": ["Vigilance"],
        "tl": "Creature — Human Soldier",
        "cmc": 3.0,
        "mc": "{2}{W}",
    },
    "desculpting blast": {
        "t": "Return target nonland permanent to its owner's hand.",
        "k": [],
        "tl": "Instant",
        "cmc": 2.0,
        "mc": "{1}{U}",
    },
    "wurmwall sweeper": {
        "t": "When this Spacecraft enters, surveil 2.",
        "k": [],
        "tl": "Artifact — Spacecraft",
        "cmc": 2.0,
        "mc": "{2}",
    },
}


def _record(prompt=PROMPT, pick=1):
    return {
        "id": "sc-test",
        "user": prompt,
        "response": '{"actions": [{"pick": %d}]}' % pick,
        "meta": {
            "record_key": "k",
            "game_key": "g",
            "expansion": "EOE",
            "rank": "diamond",
            "pick_index": pick,
            "pick_card": "Brightspear Zealot",
        },
    }


# --- parsing ----------------------------------------------------------------


def test_parse_prompt_extracts_menu_and_position():
    p = parse_prompt(PROMPT)
    assert p["menu"] == ["Brightspear Zealot", "Desculpting Blast", "Wurmwall Sweeper"]
    assert (p["turn"], p["mana"], p["lands"]) == (4, 3, 4)
    assert p["life"] == 18 and p["opp_life"] == 15
    assert len(p["hand"]) == 3
    assert p["sources"] == "W U {B/G} W"


def test_parse_prompt_rejects_a_renderer_change():
    with pytest.raises(ParseError):
        parse_prompt("TRIGGER: something\n\n=== GAME ===\nno menu here\n")


def test_pass_entries_are_not_counted_as_options():
    prompt = PROMPT.replace("  3. Cast Wurmwall Sweeper", "  3. Pass")
    assert parse_prompt(prompt)["menu"] == ["Brightspear Zealot", "Desculpting Blast"]


def test_permanent_counts_expands_multipliers_and_splits_lands():
    p = parse_prompt(PROMPT)
    assert permanent_counts(p["board_you"]) == (3, 3)  # Station Monitor + 2 Drones, 3 Plains
    assert permanent_counts(p["board_opp"]) == (1, 2)


def test_creature_lands_count_as_permanents_not_lands():
    assert permanent_counts(["Dryad Arbor 1/1 (Land Creature — Forest Dryad)"]) == (1, 0)


# --- affordability ----------------------------------------------------------


def test_cost_pips_ignores_generic_and_x():
    assert cost_pips("{3}{X}{U}{U}") == [{"U"}, {"U"}]


def test_affordability_respects_mana_value():
    srcs = parse_sources("W U W")
    assert not is_affordable("{2}{W}{W}", 4.0, 3, srcs)
    assert is_affordable("{2}{W}", 3.0, 3, srcs)


def test_affordability_respects_colour_and_hybrid_sources():
    assert not is_affordable("{B}", 1.0, 3, parse_sources("W U W"))
    assert is_affordable("{B}", 1.0, 3, parse_sources("W {B/G} W"))


def test_phyrexian_pips_are_always_payable():
    assert is_affordable("{B/P}", 1.0, 1, parse_sources("W"))


def test_two_pips_need_two_distinct_sources():
    assert not is_affordable("{W}{W}", 2.0, 2, parse_sources("W U"))
    assert is_affordable("{W}{W}", 2.0, 2, parse_sources("W W"))


# --- features and score -----------------------------------------------------


def test_decision_features_are_derived_from_the_position():
    f = decision_features(_record(), CARDS)
    assert f["menu_size"] == 3
    # 3 mana, sources W U {B/G} W: the {2}{W} creature and both 2-drops fit.
    assert f["n_affordable"] == 3
    assert f["n_creature_options"] == 1
    assert f["n_instant_options"] == 1
    assert f["board_nonland"] == 4  # 3 own + 1 opp
    assert f["pick_affordable"] is True
    assert f["unresolved_options"] == 0


def test_unresolved_card_names_are_flagged_not_crashed():
    f = decision_features(_record(), {})
    assert f["unresolved_options"] == 3
    assert f["n_affordable"] == 0
    assert f["affordability_fallback"] is True


def test_creature_ambiguity_is_lowest_with_exactly_one_creature():
    base = decision_features(_record(), CARDS)
    scores = {}
    for n_creature in (0, 1, 2, 3):
        f = dict(base, n_creature_options=n_creature)
        scores[n_creature] = complexity_components(f)["cre_amb"]
    assert scores[1] == 0.0
    assert scores[2] == 0.5
    assert scores[0] == scores[3] == 1.0


def test_more_affordable_options_scores_harder():
    base = decision_features(_record(), CARDS)
    low = complexity_score(complexity_components(dict(base, n_affordable=1)))
    high = complexity_score(complexity_components(dict(base, n_affordable=6)))
    assert high > low


def test_denser_board_scores_harder():
    base = decision_features(_record(), CARDS)
    quiet = complexity_score(complexity_components(dict(base, board_nonland=0)))
    busy = complexity_score(complexity_components(dict(base, board_nonland=12)))
    assert busy > quiet


def test_weights_sum_to_one_so_score_is_bounded():
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9
    base = decision_features(_record(), CARDS)
    hard = dict(
        base, n_affordable=99, board_nonland=99, n_creature_options=0, max_triggers=99, mean_oracle_chars=9999
    )
    assert complexity_score(complexity_components(hard)) == pytest.approx(1.0)


# --- staging mechanics ------------------------------------------------------


def test_quantile_interpolates():
    assert quantile([0.0, 1.0], 0.5) == pytest.approx(0.5)
    assert quantile([0.0, 1.0, 2.0, 3.0], 0.0) == 0.0


def test_assign_stages_splits_on_score_quantiles():
    rows = [{"score": i / 100.0} for i in range(300)]
    assign_stages(rows, (1 / 3, 2 / 3))
    counts = {1: 0, 2: 0, 3: 0}
    for r in rows:
        counts[r["stage"]] += 1
    assert all(90 <= c <= 110 for c in counts.values())
    assert max(r["score"] for r in rows if r["stage"] == 1) < min(r["score"] for r in rows if r["stage"] == 3)


def test_auc_is_one_for_perfect_separation_and_half_for_ties():
    assert auc([0.0, 0.1, 0.9, 1.0], [0, 0, 1, 1]) == pytest.approx(1.0)
    assert auc([0.5, 0.5, 0.5, 0.5], [0, 0, 1, 1]) == pytest.approx(0.5)


def test_reflex_prefers_the_biggest_affordable_creature():
    f = decision_features(_record(), CARDS)
    assert reflex_pick(f) == 1  # Brightspear Zealot, the only affordable creature
