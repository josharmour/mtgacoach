import pytest

from arenamcp.combat_trick_mana import missing_trick_color


def missing(*sources, cost="{G}", pool=None, attackers=()):
    local = {"seat_id": 2, "mana_pool": pool or {}}
    state = {"battlefield": [{"instance_id": i, "controller_seat_id": 2, **c} for i, c in enumerate(sources)]}
    return missing_trick_color(state, local, {"mana_cost": cost}, set(attackers))


def source(text="", kind="Creature", **extra):
    return {"oracle_text": text, "type_line": kind, **extra}


def test_wrong_color_and_restricted_ability_mana_cannot_cast_green_spell():
    assert missing(source("{T}: Add {C}."))
    assert missing(
        source(
            "{T}: Add two mana of any one color. "
            "Spend this mana only to activate abilities of creature sources."
        )
    )
    assert not missing(source("{T}: Add {G}."))


def test_attacker_cannot_both_attack_and_tap_for_protection():
    assert missing(source("{T}: Add {G}."), attackers=[0])
    assert not missing(source("Vigilance\n{T}: Add {G}."), attackers=[0])
    assert not missing(source("{T}: Add {G}."), attackers=[1])


def test_tapped_and_basic_sources_and_floating_mana():
    assert missing(source("", "Basic Land — Forest", is_tapped=True))
    assert not missing(source("", "Basic Land — Forest"))
    assert not missing(pool={"G": 1})
    assert missing(pool={"U": 2})
    assert not missing(pool={"unknown": 1})


@pytest.mark.parametrize(
    "text",
    [
        "{T}: Add one mana of any color.",
        "{T}, Sacrifice this artifact: Add one mana of any color.",
        "{1}: Untap target land.",
        "Instant spells you cast cost {1} less to cast.",
        "Whenever you attack, create a Treasure token.",
        "You may spend mana as though it were mana of any type.",
        "{T}: Add mana equal to the colors among permanents you control.",
    ],
)
def test_uncertain_or_alternative_source_never_proves_impossibility(text):
    assert not missing(source(text))


@pytest.mark.parametrize("cost", ["{G/U}", "{G/P}", "{X}{G}", "", "{0}", "{2}"])
def test_nonordinary_or_colorless_cost_is_outside_proof(cost):
    assert not missing(cost=cost)


def test_quantities_and_exclusive_colors_are_deliberately_not_rejected():
    assert not missing(source("{T}: Add {G} or {U}."), cost="{G}{U}")
    assert not missing(source("{T}: Add {G}."), cost="{G}{G}")


def test_independent_unrestricted_ability_not_lost_to_restriction():
    assert not missing(
        source("{T}: Add {G}.\n{T}: Add {G}{G}. Spend this mana only to activate abilities of creatures.")
    )


@pytest.mark.parametrize(
    "text",
    [
        "Add {G}{G}.",
        "Untap target land.",
        "This spell costs {G} less to cast if you control a creature.",
    ],
)
def test_visible_hand_payment_chain_is_not_proven_impossible(text):
    local = {"seat_id": 2}
    state = {"battlefield": [], "hand": [{"oracle_text": text}]}
    assert not missing_trick_color(state, local, {"mana_cost": "{G}"}, set())


def test_restriction_with_additional_spell_permission_is_not_excluded():
    assert not missing(
        source(
            "{T}: Add one mana of any color. "
            "Spend this mana only to activate abilities or cast creature spells."
        )
    )
