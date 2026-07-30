"""A combat record's answer must be the decision the search actually made.

`build_magezero_combat.py` derives the answer from ``,attacking``/``,blocking``
markers on the board. Those markers can be left over from an EARLIER declaration
in the same turn, so a row whose own decision was a priority action (Pass, a mana
ability, casting a spell) would still produce a `declare_attackers` answer naming
whatever happened to be marked — with `mcts_counts` belonging to that priority
decision.

Measured on mz_train_smoke.log before these guards: **89 of 90** emitted records
had a source `chosen` of "Pass" or a mana ability while answering
`declare_attackers` with specific creatures. Invented supervision is strictly
worse than an empty corpus, because it looks like data.

Two independent guards, tested here:
  * the parser classifies a row as combat only if its own MENU offers a
    declaration (XMage grants ordinary priority windows *inside* the
    declare-attackers step, and those carry a priority menu);
  * the converter refuses any row whose CHOSEN action is not a declaration.
"""

from __future__ import annotations

import pytest
from tools.training.build_magezero_combat import is_combat_declaration
from tools.training.parse_magezero_log import classify_kind

# ---------------------------------------------------------------------------
# Converter guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "chosen",
    [
        "Pass",
        "{1}{U}: Untap {this}.",
        "{T}: Draw a card, then discard a card.",
        "Cast Malcolm, Alluring Scoundrel",
        "Play Island",
        "",
    ],
)
def test_priority_actions_are_not_combat_declarations(chosen):
    """These are the six most common `chosen` values on phase-labelled rows."""
    assert not is_combat_declaration(chosen)


@pytest.mark.parametrize(
    "chosen",
    [
        "Block Grizzly Bears with Wall of Omens",
        "Attack with Kitsa, Otterball Elite",
        "attack with everything",
        "  Block Cecil, Dark Knight with Skrelv",
    ],
)
def test_real_declarations_are_accepted(chosen):
    assert is_combat_declaration(chosen)


def test_oracle_text_mentioning_blocking_is_not_a_declaration():
    """The anchor matters: this oracle text is a common `chosen` value.

    An unanchored search for "block" would treat it as a block declaration.
    """
    chosen = (
        "{W/P}, {T}: Choose a color. Another target creature you control gains "
        "toxic 1 and hexproof from that color until end of turn. It can't be "
        "blocked by creatures of that color this turn."
    )
    assert not is_combat_declaration(chosen)


# ---------------------------------------------------------------------------
# Parser classification guard
# ---------------------------------------------------------------------------


def _pool(*names: str) -> list[tuple[str, float, int]]:
    return [(n, 0.1, 100) for n in names]


def test_priority_window_inside_declare_attackers_is_priority():
    """THE regression: the phase alone must not make a row 'attackers'.

    Of 1,274 rows the phase-based rule labelled "attackers", not one had an
    attack declaration as its chosen action.
    """
    kind = classify_kind("DECLARE_ATTACKERS", _pool("Pass", "Cast Bounce Off"))
    assert kind == "priority", f"expected priority, got {kind}"


def test_priority_window_inside_declare_blockers_is_priority():
    kind = classify_kind("DECLARE_BLOCKERS", _pool("Pass", "{1}{U}: Untap {this}."))
    assert kind == "priority", f"expected priority, got {kind}"


def test_genuine_attack_declaration_is_classified_attackers():
    kind = classify_kind("DECLARE_ATTACKERS", _pool("Attack with Kitsa, Otterball Elite", "Pass"))
    assert kind == "attackers"


def test_genuine_block_declaration_is_classified_blockers():
    kind = classify_kind("DECLARE_BLOCKERS", _pool("Block Cecil, Dark Knight with Skrelv", "Pass"))
    assert kind == "blockers"


def test_binary_still_wins_over_phase():
    """CHOOSE_USE windows stay binary even in a combat step."""
    assert classify_kind("DECLARE_ATTACKERS", _pool("true", "false")) == "binary"


def test_non_combat_phase_unaffected():
    assert classify_kind("PRECOMBAT_MAIN", _pool("Play Island", "Pass")) == "priority"
    assert classify_kind("UPKEEP", _pool("Cast Bounce Off", "Pass")) == "priority"


def test_oracle_text_in_menu_does_not_promote_to_combat():
    """A menu whose only 'block' mention is inside oracle text stays priority."""
    oracle = (
        "{W/P}, {T}: Choose a color. Another target creature you control gains "
        "toxic 1 and hexproof from that color until end of turn. It can't be "
        "blocked by creatures of that color this turn."
    )
    assert classify_kind("DECLARE_BLOCKERS", _pool("Pass", oracle)) == "priority"
