"""Free mana must be recognized without treating conditional mana as free."""

import pytest

from arenamcp.draft_guidance import FormatContext, _base_score, normalize_card


def artifact(text, cost="{0}", **fields):
    return normalize_card(
        dict(name="Test mana source", type_line="Artifact", mana_cost=cost, oracle_text=text, **fields)
    )


@pytest.mark.parametrize("color", "WUBRGC")
def test_free_unconditional_mana_is_strong_unrated_standalone_power(color):
    mana = artifact(f"{{T}}: Add {{{color}}}.")
    removal = normalize_card(
        dict(
            name="Conditional Golem maker",
            type_line="Artifact",
            mana_cost="{1}{R}",
            rarity="mythic",
            oracle_text="When this artifact enters, it deals 2 damage to any target.\n"
            "{2}, {T}, Sacrifice another artifact: Create a 3/3 Golem artifact creature token.",
        )
    )
    assert "free_repeatable_mana" in mana.tags
    assert _base_score(mana, FormatContext())[0] > _base_score(removal, FormatContext())[0]


@pytest.mark.parametrize(
    "text,cost",
    [
        ("{T}: Add {G}.", "{2}"),
        ("{T}: Add {G}.", ""),
        ("{1}, {T}: Add {G}.", "{0}"),
        ("{T}, Sacrifice this artifact: Add {G}.", "{0}"),
        ("{T}: Add {G}. Activate only if you control three or more artifacts.", "{0}"),
        ("This artifact enters tapped.\n{T}: Add {G}.", "{0}"),
        ("When this artifact enters, sacrifice it unless you discard a card.\n{T}: Add {G}.", "{0}"),
        ("{T}: Add one mana of any of the exiled card's colors.", "{0}"),
    ],
)
def test_conditional_or_costly_mana_does_not_get_free_mana_prior(text, cost):
    assert "free_repeatable_mana" not in artifact(text, cost).tags


def test_measured_rating_is_not_replaced_by_free_mana_prior():
    mana = artifact("{T}: Add {W}.", gih_wr=0.54)
    assert _base_score(mana, FormatContext()) == (50.0, None)


def test_arena_symbol_spelling_has_same_free_mana_value():
    assert _base_score(artifact("{oT}: Add {oG}."), FormatContext()) == _base_score(
        artifact("{T}: Add {G}."), FormatContext()
    )
