"""Their drain engines: threat role, removal targets and clock (ledger #132, #133, match 16ed84d0).

Biotech Specialist ("Whenever you sacrifice an artifact, this creature deals 2 damage to target
opponent.") dealt 13 of our 20. On T7 the lines scored Iroh's Demonstration on The
Sackville-Bagginses 18, 18, 18 and blind to both engines; the threat list ranked the Bagginses
first; on T8 the facts said "they kill us in 10" and we lost 7 that turn; on T12 the concede
check said "not dead next attack" 8 s before two Biotech triggers killed us. A recurring "whenever
..."/"at the beginning of ..." trigger that deals us N damage (or makes us lose N life) is now an
engine worth N times its expected triggers a turn (a sacrifice trigger: their artifacts or
tokens, at least one) in the threat list, the removal target order, the material of the lines,
the target prompt facts, their clock, the lines' attacks of theirs and the concede projection.
"""

from __future__ import annotations

from copy import deepcopy

from tests.engine_boards_20261019 import G16E_T7, G16E_T8, G16E_T12
from tests.race_boards_20261019 import card

from arenamcp import board_assessment as ba
from arenamcp.board_model import build_board_model
from arenamcp.concede import estimate_loss
from arenamcp.line_search import DEAD, search_lines
from arenamcp.line_search_moves import Moves, drain_engine, drain_triggers
from arenamcp.play_safety import creature_state_facts


def _search(source: dict):
    survival, lethal_now = ba.search_hints(build_board_model(deepcopy(source)))
    return search_lines(
        build_board_model(deepcopy(source)), survival=survival, lethal_now=lethal_now, hard_ms=1e9
    )


def _fresh(source: dict):
    ba._CACHE.clear()
    return ba.assess(deepcopy(source))


def test_drain_engine_reads_recurring_damage_and_life_loss_to_us():
    assert drain_engine(card(1, "Biotech Specialist", 2)) == ((2, "whenever you sacrifice an artifact"),)
    assert drain_engine(card(2, "The Sackville-Bagginses", 2)) == ((1, "whenever you sacrifice a token"),)
    upkeep = {
        "name": "Pain Engine",
        "oracle_text": "At the beginning of your upkeep, each opponent loses 1 life.",
    }
    assert drain_engine(upkeep) == ((1, "at the beginning of your upkeep"),)
    # Not drains of us: their own life loss, a conditional or variable amount, an enters trigger.
    assert drain_engine(card(3, "Obsessive Pursuit", 2)) == ()
    variable = {"name": "Var", "oracle_text": "Whenever you gain life, target opponent loses that much life."}
    assert drain_engine(variable) == ()
    maybe = {"name": "Maybe", "oracle_text": "At the beginning of your end step, if you attacked this turn, "
             "each opponent loses 2 life."}  # fmt: skip
    assert drain_engine(maybe) == ()
    # Expected triggers on a turn of theirs: a sacrifice trigger once per permanent it can take.
    counts = {"artifact": 2, "token": 1, "clue": 1}
    assert drain_triggers("whenever you sacrifice an artifact", counts) == 2
    assert drain_triggers("whenever you sacrifice a token", counts) == 1
    assert drain_triggers("whenever you sacrifice a clue", counts) == 1
    assert drain_triggers("whenever you sacrifice an artifact", {}) == 1  # at least one
    assert drain_triggers("at the beginning of your upkeep", {}) == 1
    assert drain_triggers("at the beginning of each upkeep", {}) == 2
    assert drain_triggers("whenever you draw a card", {}, draws=2) == 2
    # An attack trigger is attack_pings' (Hired Claw), a death is not a turn's event.
    assert drain_triggers("whenever you attack with one or more lizards", {}) == 0
    assert drain_triggers("whenever another creature you control dies", {}) == 0


def test_t7_biotech_is_the_engine_the_threat_list_and_targets_rank_first():
    model = build_board_model(deepcopy(G16E_T7))
    bodies = {b["name"]: b for b in model.theirs}
    assert (bodies["Biotech Specialist"]["_drain"], bodies["The Sackville-Bagginses"]["_drain"]) == (2, 1)
    assert ba.engine_role(bodies["Biotech Specialist"]) > ba.engine_role(bodies["The Sackville-Bagginses"])
    # The threat list (was: Bagginses 2.6, Biotech 1.9).
    facts = _fresh(G16E_T7)
    assert [t.name for t in facts.threats[:2]] == ["Biotech Specialist", "The Sackville-Bagginses"]
    assert "drain engine: ~2 damage to us each turn of theirs" in facts.threats[0].why
    # Removal targets: the engine first, and its kill is worth more than the 2/2's.
    moves = Moves(model, survival=False, lethal_now=False, hard_ms=1e9, opponent=None)
    iroh = next(hs for hs in moves.spells if hs.name == "Iroh's Demonstration")
    kill = next(v for v in iroh.variants if v.reach is not None and "4 damage" in v.mode_text)
    targets = moves.targets(kill.reach, model.theirs, frozenset())
    assert [t["name"] for t in targets] == ["Biotech Specialist", "The Sackville-Bagginses"]
    values = {t["name"]: moves.item_value(kill, t, model.theirs) for t in targets}
    assert values["Biotech Specialist"] > values["The Sackville-Bagginses"]
    assert moves.material(bodies["Biotech Specialist"]) > moves.material(bodies["The Sackville-Bagginses"])


def test_t7_lines_count_the_drains():
    result = _search(G16E_T7)
    lines = [result.best, *result.lines, *result.first_action.values()]
    bagginses = [
        ln for ln in lines if ("Iroh's Demonstration", "The Sackville-Bagginses") in ln.steps[0].targets
    ]
    biotech = [ln for ln in lines if ("Iroh's Demonstration", "Biotech Specialist") in ln.steps[0].targets]
    # Kill the Bagginses: Biotech drains 2 a turn (was 18, 18, 18); kill Biotech: the Bagginses'
    # 2/2 and its 1 a turn (was 18, 17, 16).
    assert any(ln.lives()[:3] == [16, 14, 12] for ln in bagginses), [ln.summary() for ln in bagginses]
    assert any(ln.lives()[:3] == [15, 12, 9] for ln in biotech), [ln.summary() for ln in biotech]


def test_target_facts_name_the_engine():
    facts = creature_state_facts(card(317, "Biotech Specialist", 2), mine=False)
    assert facts["engine"] == "2 damage to you whenever they sacrifice an artifact"
    flock = creature_state_facts(card(351, "Ravenhill Flock", 2), mine=False)
    assert flock["engine"] == "+1/+1 counter whenever they draw a card"
    assert "engine" not in creature_state_facts(card(318, "Biotech Specialist", 1), mine=True)
    assert "engine" not in creature_state_facts(card(319, "Grizzly Bears", 2), mine=False)


def test_t8_their_clock_counts_the_drains():
    facts = _fresh(G16E_T8)
    # Biotech 2 a turn (the Lander), the Bagginses 1 (the Lander is a token), plus Biotech's
    # attack: they kill us in 5 (was 10; we lost 7 that turn, 18 -> 11).
    assert facts.their_clock == 5, facts.facts_line()
    assert (
        "their clock counts: Biotech Specialist drains 2 a turn, The Sackville-Bagginses drains 1 a turn"
        in (facts.facts_line())
    )
    # The lines: their drains and the attack this turn (was 18, 16, 14, 12).
    assert facts.lines[0].lives()[:4] == [15, 10, 5, 1], facts.lines[0].summary()


def test_t12_the_concede_check_sees_the_biotech_triggers():
    facts = _fresh(G16E_T12)
    # 3 through (Biotech 2/4 and Gene Pollinator) and 9 drained (two Biotechs x 2 artifacts x 2,
    # the Bagginses x 1 token): dead this turn; it said "not dead next attack" (0.00).
    assert facts.opp_lethal_on_board and facts.dead_in == 1 and facts.our_life_now_attack == -8
    estimate = estimate_loss(deepcopy(G16E_T12))
    assert estimate.reason.startswith("Dead next attack (12 through vs 4 life)")
    # A projection, not a certainty: it never concedes on the engines alone.
    assert estimate.confidence < 0.95
    assert any(
        why.startswith("their engines decide it (Biotech Specialist drains 4") for why in estimate.blockers
    )


def test_queza_drains_the_212848_board_without_its_hushbringer():
    # bug_20260928_212848 (tests/strategic_states): Queza, Augur of Agonies ("Whenever you draw a
    # card, target opponent loses 1 life ...") drains 1 a turn; at 1 life the Tyrant's 3 no longer
    # saves us (the Hushbringer tests blank Queza to keep testing Hushbringer).
    from tests.strategic_states import BUG_212848

    source = deepcopy(BUG_212848)
    for entry in source["battlefield"]:
        if entry["name"] == "Hushbringer":
            entry["oracle_text"] = "Flying\nLifelink"
    result = _search(source)
    assert all(line.cls == DEAD for line in [result.best, *result.lines, *result.first_action.values()])
    assert "Queza, Augur of Agonies drains 1 a turn" in _fresh(source).clock_engines
