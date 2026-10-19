"""Their enters pings times their token makers, and their sacrifice outlets' reach (match b62b73fc).

b62b73fc T13 (15:27:32): we were at 12 against Impact Tremors ("Whenever a creature you control
enters, this enchantment deals 1 damage to each opponent."), Goblin Bombardment ("Sacrifice a
creature: This enchantment deals 1 damage to any target.") and Krenko, Mob Boss ("{T}: Create X
1/1 red Goblin creature tokens, where X is the number of Goblins you control.") with 10 Goblins.
The facts said "race STALLED", their clock "—" and the concede check "not dead next attack" (0.00);
Krenko made 10 Goblins (10 pings) and the Bombardment sacrificed two for the last 2, without an
attack. An enters ping now counts their token makers' output, a free sacrifice outlet is its damage
times their creatures, and both feed their clock, the threat list and the concede projection.
"""

from __future__ import annotations

from copy import deepcopy

from tests.engine_boards_20261019 import GB62_T13
from tests.race_boards_20261019 import card

from arenamcp import board_assessment as ba
from arenamcp.board_model import build_board_model
from arenamcp.concede import estimate_loss
from arenamcp.line_search_moves import drain_engine, drain_triggers, sac_outlet

US, THEM = 1, 2


def _fresh(source: dict):
    ba._CACHE.clear()
    return ba.assess(deepcopy(source))


def _without(source: dict, *names: str) -> dict:
    state = deepcopy(source)
    state["battlefield"] = [c for c in state["battlefield"] if c["name"] not in names]
    return state


def test_sac_outlet_reads_free_outlets_only():
    assert sac_outlet(card(1, "Goblin Bombardment", THEM)) == 1
    loses = {"name": "Altar", "oracle_text": "Sacrifice a creature: Each opponent loses 1 life."}
    assert sac_outlet(loses) == 1
    # Their mana limits a paid outlet; an outlet that doesn't hit us is no reach.
    paid = {
        "name": "Paid",
        "oracle_text": "{1}, Sacrifice a creature: This artifact deals 2 damage to any target.",
    }
    assert sac_outlet(paid) == 0
    grow = {"name": "Grow", "oracle_text": "Sacrifice a creature: Put a +1/+1 counter on this creature."}
    assert sac_outlet(grow) == 0
    assert sac_outlet(card(2, "Krenko, Mob Boss", THEM)) == 0


def test_an_enters_ping_triggers_once_per_creature_entering():
    assert drain_engine(card(1, "Impact Tremors", THEM)) == ((1, "whenever a creature you control enters"),)
    event = "whenever a creature you control enters"
    assert drain_triggers(event, {}, entering=10) == 10
    assert drain_triggers(event, {}) == 1  # at least one a turn
    # "One or more" and "nontoken" triggers fire once for a batch of tokens; a land is a land drop.
    assert drain_triggers("whenever one or more creatures you control enter", {}, entering=10) == 1
    assert drain_triggers("whenever a nontoken creature you control enters", {}, entering=10) == 1
    assert drain_triggers("whenever a land you control enters", {}, drops=1, entering=10) == 1


def test_t13_krenko_feeds_impact_tremors_and_the_bombardment():
    model = build_board_model(deepcopy(GB62_T13))
    engines = model.engines
    # Krenko's X counts their 10 Goblins: 10 tokens enter, 10 pings, 1 a creature sacrificed.
    assert (engines.entering, engines.outlet, engines.static_drain) == (10, 1, 10)
    assert "Impact Tremors (10 a turn)" in engines.names
    assert next(b for b in model.theirs if b["name"] == "Krenko, Mob Boss")["_tokens"] == 10
    assert engines.reach(12) == 12
    # Without the token maker: one ping a turn, and the outlet still reaches with each creature.
    alone = build_board_model(_without(GB62_T13, "Krenko, Mob Boss")).engines
    assert (alone.entering, alone.outlet, alone.static_drain) == (1, 1, 1)


def test_t13_their_clock_and_threats_count_the_pings_and_the_outlet():
    facts = _fresh(GB62_T13)
    # Was: their clock "—", "race STALLED", concede 0.00; we died that turn without an attack.
    assert facts.dead_in == 1 and facts.their_clock == 1, facts.facts_line()
    assert facts.opp_lethal_on_board
    line = facts.facts_line()
    assert (
        "their clock counts: Krenko, Mob Boss makes ~10 tokens a turn, Impact Tremors (10 a turn), "
        "a sacrifice outlet (1 a creature: ~10 reach)"
    ) in line, line
    # The threat list (was: Krenko 5.9, Battle Cry Goblin 4.6, Impact Tremors 2.0 "direct damage").
    threats = {t.name: t.why for t in facts.threats[:3]}
    assert set(threats) == {"Impact Tremors", "Goblin Bombardment", "Krenko, Mob Boss"}, threats
    assert threats["Impact Tremors"] == "drain engine: ~10 damage to us each turn of theirs"
    assert threats["Goblin Bombardment"].startswith("sacrifice outlet: 1 damage a creature they sacrifice")
    assert "token engine: ~10 1/1 tokens each turn of theirs" in threats["Krenko, Mob Boss"]


def test_t13_the_concede_check_sees_the_pings_and_the_outlet():
    estimate = estimate_loss(deepcopy(GB62_T13))
    # Was "not dead next attack" (0.00).
    assert estimate.reason.startswith("Dead next attack"), estimate.reason
    # A projection, not a certainty: it never concedes on the engines alone.
    assert estimate.confidence < 0.95
    engines = next(why for why in estimate.blockers if why.startswith("their engines decide it ("))
    assert "Impact Tremors (10 a turn)" in engines and "sacrifice outlet: 1 a creature" in engines
