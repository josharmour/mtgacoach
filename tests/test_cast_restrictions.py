"""Cast restrictions in the line search, and the tie-break behind its score (2026-10-08).

bug_20261006_185403 T10 with the model down: the line search tied "Codie,
Ravenous Codex + Theoretical Necromancer" with "Geist of Saint Thalia +
Theoretical Necromancer" at the same value and the summary text decided, so
the fallback cast the 6-mana pair. Two things changed:

* a permanent that says "You can't cast permanent spells." (Codie, Vociferous
  Codex; the Ravenous Codex of the fixture has no such clause) now binds: on
  the battlefield it makes the hand's permanents uncastable for the schedule
  and the search alike (``board_model.board_cast_bans``), and cast inside a
  line it stops the later turns' permanents (``Moves.bans``);
* an exact tie in (class, timing, value) goes to the T step that casts more
  creature bodies, then to the one that leaves more mana open
  (``line_search._deployment``).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from arenamcp import board_assessment as ba
from arenamcp.board_model import board_cast_bans, build_board_model, cast_bans, spell_banned
from arenamcp.line_search import _deployment, _order

FIXTURE = Path(__file__).parent / "fixtures" / "bug_20261006_185403_game_state.json"
VOCIFEROUS = (
    "You can't cast permanent spells.\n{o4}, {oT}: Add {oW}{oU}{oB}{oR}{oG}. When you next cast a spell "
    "this turn, exile cards from the top of your library until you exile an instant or sorcery card "
    "with lesser mana value."
)
CODIE, GEIST, NECROMANCER, LIBRARY = (
    "Codie, Ravenous Codex", "Geist of Saint Thalia", "Theoretical Necromancer", "Living Library",
)  # fmt: skip


def _main_phase() -> dict:
    """Turn 10 main phase 1 after the Swamp (as tests/test_llm_down_fallback builds it)."""
    state = json.loads(FIXTURE.read_text())
    state["turn"].update({"phase": "Phase_Main1", "step": "", "active_player": 2, "priority_player": 2})
    state["pending_decision"] = "Action Required"
    state["decision_context"] = {}
    for card in state["battlefield"]:
        if card["instance_id"] == 266:  # Yuriko, untapped before combat
            card.update(is_tapped=False, is_attacking=False)
    return state


def _search(state: dict):
    ba._CACHE.clear()
    return ba.assess(copy.deepcopy(state)).line_search


def _hand(state: dict, name: str) -> dict:
    return next(card for card in state["hand"] if card["name"] == name)


# --- reading the clause ---------------------------------------------------------------


def test_the_clause_is_read_from_the_card_and_scoped_to_its_controller():
    codie = {
        "name": "Codie, Vociferous Codex",
        "oracle_text": VOCIFEROUS,
        "type_line": "Legendary Artifact Creature",
    }
    assert cast_bans(codie, ours=True) == frozenset({"permanent"})
    assert cast_bans(codie, ours=False) == frozenset()  # "you" is their you
    shared = {
        "name": "Rule",
        "oracle_text": "Players can't cast creature spells.",
        "type_line": "Enchantment",
    }
    assert cast_bans(shared, ours=False) == frozenset({"creature"})
    conditional = {
        "name": "Teeg",
        "oracle_text": "Noncreature spells with mana value 4 or greater can't be cast.",
    }
    assert cast_bans(conditional, ours=True) == frozenset()
    ravenous = _hand(_main_phase(), CODIE)
    assert cast_bans(ravenous, ours=True) == frozenset()  # the FRA Codie restricts nothing


def test_which_spells_a_ban_stops():
    creature = {"type_line": "Creature — Spirit", "card_types": ["Creature"]}
    instant = {"type_line": "Instant", "card_types": ["Instant"]}
    artifact = {"type_line": "Artifact", "card_types": ["Artifact"]}
    assert spell_banned(creature, frozenset({"permanent"})) and spell_banned(
        artifact, frozenset({"permanent"})
    )
    assert not spell_banned(instant, frozenset({"permanent"}))
    assert spell_banned(creature, frozenset({"creature"})) and not spell_banned(
        artifact, frozenset({"creature"})
    )
    assert spell_banned(instant, frozenset({"noncreature"})) and not spell_banned(
        creature, frozenset({"noncreature"})
    )
    assert spell_banned(artifact, frozenset({"artifact"})) and not spell_banned(
        creature, frozenset({"artifact"})
    )
    assert not spell_banned(creature, frozenset())


# --- on the battlefield: the hand's permanents are uncastable ---------------------------


def test_a_vociferous_codex_on_our_board_makes_every_permanent_in_hand_uncastable():
    state = _main_phase()
    state["battlefield"].append(
        {
            "instance_id": 500, "grp_id": 76661, "name": "Codie, Vociferous Codex", "oracle_text": VOCIFEROUS,
            "type_line": "Legendary Artifact Creature — Book Construct", "mana_cost": "{3}",
            "owner_seat_id": 2, "controller_seat_id": 2, "power": 1, "toughness": 4, "is_tapped": False,
            "turn_entered_battlefield": 8, "card_types": ["Artifact", "Creature"], "object_kind": "CARD",
        }
    )  # fmt: skip
    assert board_cast_bans(state["battlefield"], 2) == frozenset({"permanent"})
    model = build_board_model(copy.deepcopy(state))
    assert {s.name: s.uncastable for s in model.spells} == {
        LIBRARY: True, CODIE: True, GEIST: True, NECROMANCER: True,
    }  # fmt: skip
    result = _search(state)
    assert all(not step.casts for line in result.lines + [result.baseline] for step in line.steps)


def test_their_codex_restricts_them_not_us():
    state = _main_phase()
    state["battlefield"].append(
        {
            "instance_id": 500, "name": "Codie, Vociferous Codex", "oracle_text": VOCIFEROUS,
            "type_line": "Legendary Artifact Creature — Book Construct", "owner_seat_id": 1,
            "controller_seat_id": 1, "power": 1, "toughness": 4, "card_types": ["Artifact", "Creature"],
        }
    )  # fmt: skip
    assert board_cast_bans(state["battlefield"], 2) == frozenset()
    assert not any(s.uncastable for s in build_board_model(copy.deepcopy(state)).spells)


# --- cast inside a line: the later turns cast no permanent -------------------------------


def test_casting_the_codex_in_a_line_ends_the_permanents_after_it():
    state = _main_phase()
    _hand(state, CODIE).update(name="Codie, Vociferous Codex", oracle_text=VOCIFEROUS)
    result = _search(state)
    codex_lines = [
        line
        for line in result.lines + [result.baseline]
        if any("Codie, Vociferous Codex" in step.casts for step in line.steps)
    ]
    assert codex_lines, [line.summary() for line in result.lines]
    for line in codex_lines:
        cast_at = next(i for i, step in enumerate(line.steps) if "Codie, Vociferous Codex" in step.casts)
        assert all(not step.casts for step in line.steps[cast_at + 1 :]), line.summary()
        # The turn after shows nothing castable either.
        assert all(step.castable == () for step in line.steps[cast_at + 1 :]), line.summary()
    # With the rest of the hand locked out, the Codex is never the best line's first cast.
    assert "Codie, Vociferous Codex" not in result.best.steps[0].casts
    assert result.best.steps[0].casts == (GEIST, NECROMANCER)


# --- the tie-break -------------------------------------------------------------------------


def test_an_exact_tie_goes_to_the_cheaper_pair_of_bodies():
    result = _search(_main_phase())
    best = result.best
    assert best.steps[0].casts == (GEIST, NECROMANCER)
    rival = next(
        line for line in result.lines if line.steps[0].casts == (CODIE, NECROMANCER) and line.steps[0].attack
    )
    assert rival.score == best.score  # the exact tie the fallback used to lose
    assert _deployment(best) == (2, 1) and _deployment(rival) == (2, 0)  # 5 of 6 mana vs all 6
    assert _order(best) > _order(rival)
