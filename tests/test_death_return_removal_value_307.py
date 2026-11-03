"""#307: removal valuation keeps a death-return creature's engine, not its body.

The historical packet is not bundled here; these are equivalent board regressions
built from the ledger row ("death-return engine undervalued in blocks") and the
merged #270/#284 return-credit work, not a claim of complete replay reconstruction.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from arenamcp.board_assessment import _Spell, _spell_value, engine_role
from arenamcp.combat_solver import _death_material, _material

DRAIN = "Whenever you gain life, each opponent loses that much life."
DRAW = "Whenever an opponent casts a spell, you draw a card and you lose 1 life."
RETURN = (
    "When {name} dies, if it was a creature, return it to the battlefield under its "
    "owner's control. It's an enchantment. (It's not a creature.)"
)
ANSWER = dict(
    name="Answer",
    mana_cost="{1}{B}",
    type_line="Instant",
    oracle_text="Destroy target creature.",
)


def body(name: str, engine: str, *, returns: bool = True, **extra) -> dict:
    text = engine + ("\n" + RETURN.format(name=name) if returns else "")
    return dict(
        instance_id=501,
        name=name,
        oracle_text=text,
        type_line="Enchantment Creature — Cat Glimmer",
        card_types=["CardType_Enchantment", "CardType_Creature"],
        power=2,
        toughness=2,
        controller_seat_id=2,
        owner_seat_id=2,
        **extra,
    )


def drain_body(**extra) -> dict:
    return body("Enduring Tenacity", DRAIN, _drain=1, **extra)


def draw_body(**extra) -> dict:
    return body("Enduring Innocence", DRAW, **extra)


def removal() -> _Spell:
    return _Spell(card=ANSWER, name="Answer", role="removal", mana_value=2, pips=("B",), has_x=False)


def test_removal_on_a_return_creature_is_worth_its_body_only():
    """Killing it removes the 2/2; the drain engine returns as an enchantment."""
    assert engine_role(drain_body()) == 2.0
    assert _spell_value(removal(), survival=False, theirs=[drain_body()]) == 5.0
    assert _spell_value(removal(), survival=False, theirs=[drain_body(returns=False)]) == 7.0


def test_exile_interference_counts_the_engine_as_removed():
    """Visible exile withholds the return credit, so the engine really dies."""
    blocked = drain_body(_return_credit_blocked=True)
    assert _spell_value(removal(), survival=False, theirs=[blocked]) == 7.0


@pytest.mark.parametrize(
    "role,text,value",
    [
        ("removal", "Exile target creature.", 7.0),
        ("bounce", "Return target creature to its owner's hand.", 5.0),
        ("removal", "Destroy target creature. Exile target creature with flying.", 5.0),
    ],
)
def test_return_credit_depends_on_how_the_spell_can_remove_this_target(role, text, value):
    spell = replace(removal(), role=role, card={**ANSWER, "oracle_text": text})
    assert _spell_value(spell, survival=False, theirs=[drain_body()]) == value


def test_draw_engine_return_creature_is_not_double_counted():
    """A draw engine is not in ``engine_role``; the fix must not invent a discount."""
    assert engine_role(draw_body()) == 0.0
    assert _spell_value(removal(), survival=False, theirs=[draw_body()]) == 5.0
    assert _spell_value(removal(), survival=False, theirs=[draw_body(returns=False)]) == 5.0


def test_block_valuation_still_retains_the_engine_and_loses_the_body():
    """The block half (#284) keeps the residual engine credit; only the body is lost."""
    live = drain_body(_engine_value=1)
    assert _material(live) == 10.0
    assert _death_material(live) == 6.0
    plain = drain_body(returns=False, _engine_value=1)
    assert _material(plain) == 7.0
    assert _death_material(plain) == 7.0


def test_the_most_dangerous_target_still_wins_when_it_does_not_return():
    """A plain drain body outranks a returning one: the engine dies with it."""
    plain = body("Biotech Specialist", DRAIN, returns=False, _drain=1)
    assert _spell_value(removal(), survival=False, theirs=[drain_body(), plain]) == 7.0
