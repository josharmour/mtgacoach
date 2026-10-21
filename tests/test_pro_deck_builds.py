"""The counted deck build against a pro's hand-built 40 (2026-10-18, Alchemy FRA).

Two Premier Drafts the same pro drafted and built by hand:

* tests/fra_alchemy_draft_2026_10_18 (4-1): Selesnya ramp + lifegain. Heartwood
  Crafter x2, Loot and Garruk ramp into Emrakul, Ghalta and Ruric Thar; Dina,
  Confidant, Valgavoth and Flourishing Forces are the lifegain engine; Hexhaven
  Dueling Arena re-prepares the "enters prepared" creatures.
* tests/fra_alchemy_draft2_2026_10_18 (5-1, later games by our autopilot): Azorius
  planeswalkers splashing Mind Meanderer off one Forest and Vigorbloom Annex.

The build runs as the draft autoplay runs it (DraftEventDriver._decide_deck: the
drafted lane, the counted builder, the 17Lands deck model) minus the LLM refine.
It must agree with the pro on the structural choices: colors and splash, the
engine cards, the top end the ramp is for, and the land count. Lessons not learned
yet are strict xfails, so learning one forces the marker off.
"""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

import pytest
from tests import fra_alchemy_draft2_2026_10_18 as pro2
from tests import fra_alchemy_draft_2026_10_18 as pro1

import arenamcp.draft_event as draft_event
import arenamcp.limited_formats as limited_formats
from arenamcp.draft_event import DraftEventDriver
from arenamcp.limited_deck import _effective_cmc, _ramp

PRIMER = pro1.primer()
RARITY = {"C": "common", "U": "uncommon", "R": "rare", "M": "mythic"}


@pytest.fixture(scope="module", autouse=True)
def _isolated(tmp_path_factory):
    """Module-scoped, so it is in place before the module-scoped builds below run."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(limited_formats, "_event_expansions", {})
        # No saved draft runs from this machine: the lane comes from the pool.
        patch.setattr(draft_event, "DRAFT_RUN_DIR", tmp_path_factory.mktemp("draft_runs"))
        yield


def _card(grp_id: int) -> dict:
    card = PRIMER.card(grp_id)
    return {
        "grp_id": grp_id,
        "name": card.name,
        "type_line": card.types,
        "oracle_text": card.oracle,
        "mana_cost": pro1.COSTS.get(grp_id, ""),
        "gih_wr": card.gih_wr,
        "rarity": RARITY.get(card.rarity, ""),
        "cmc": card.cmc,
    }


def _build(fixture) -> dict:
    pool = [g for g in fixture.pool() if g not in fixture.BASICS]
    service = SimpleNamespace(get=lambda code: PRIMER, ensure=lambda code: None, quick=lambda code: PRIMER)
    driver = DraftEventDriver(
        bridge_fn=lambda: None,
        tracker_fn=lambda: None,
        primer_service=service,
        pool_cards_fn=lambda ids, code: [_card(g) for g in ids],
    )
    driver.run.set_code = "FRA"
    decision = driver._decide_deck(pool)
    assert decision is not None and decision["source"].startswith("counted build")
    return decision["build"]


def _names(build: dict) -> Counter:
    return Counter({pro1.NAMES[entry["grp_id"]]: entry["count"] for entry in build["main_deck"]})


def _pro(fixture) -> tuple[Counter, Counter]:
    """(the pro's nonbasic cards, his basics by color)."""
    colors = {"Plains": "W", "Island": "U", "Swamp": "B", "Mountain": "R", "Forest": "G"}
    cards, basics = Counter(), Counter()
    for grp_id, count in fixture.MAIN_DECK.items():
        if grp_id in fixture.BASICS:
            basics[colors[fixture.BASICS[grp_id]]] += count
        else:
            cards[pro1.NAMES[grp_id]] += count
    return cards, basics


def _land_count(build: dict) -> int:
    lands = sum(
        entry["count"] for entry in build["main_deck"] if "Land" in PRIMER.card(entry["grp_id"]).types
    )
    return lands + sum(build["basic_lands"].values())


@pytest.fixture(scope="module")
def selesnya(_isolated) -> dict:
    return _build(pro1)


@pytest.fixture(scope="module")
def azorius(_isolated) -> dict:
    return _build(pro2)


# -- rules the lessons rest on ------------------------------------------------


def test_restricted_mana_is_half_a_ramp_piece_and_a_cast_trigger_untap_is_none():
    assert _ramp(_card(pro1.ID["Loot, the Nexus"])) == 1.0
    assert _ramp(_card(pro1.ID["Garruk, Curse Breaker"])) == 1.0  # untap two lands
    # "This mana can't be spent to cast spells from your hand": exile casts only.
    assert _ramp(_card(pro1.ID["Heartwood Crafter"])) == 0.5
    # Emrakul ramps from hand (exile: a land taps for {C}{C}); its cast trigger does not count.
    assert _ramp(_card(pro1.ID["Emrakul, the Exigent Doom"])) == 1.0
    assert _ramp(_card(pro1.ID["Teyo, Lightshield Expert"])) == 0.0


def test_off_color_twobrid_symbols_cost_two_and_cost_reductions_count():
    karn = _card(pro1.ID["Karn, Gilded Guardian"])  # {2/W}{2/U}{2/B}{2/R}{2/G}
    assert _effective_cmc(karn, ("W", "U")) == 8
    assert _effective_cmc(karn, tuple("WUBRG")) == 5
    ghalta = _card(pro1.ID["Ghalta the Unstoppable"])  # costs {X} less, X = greatest power
    assert _effective_cmc(ghalta, ("W", "G")) == 6


# -- pool 1: Selesnya ramp + lifegain (4-1) -----------------------------------


def test_selesnya_build_plays_the_pros_colors_and_engine(selesnya):
    assert selesnya["quality"]["colors"] == "WG" and not selesnya["quality"]["splash"]
    deck = _names(selesnya)
    for card in (
        "Dina, Flesh Sower",
        "Enlightened Confidant",
        "Valgavoth, the Lifeline",
        "Flourishing Forces",
        "Germinate Recruits",
        "Garruk, Curse Breaker",
        "Loot, the Nexus",
        "Teyo, Lightshield Expert",
    ):
        assert deck[card] == 1, card
    assert deck["Heartwood Crafter"] == 2


def test_selesnya_ramp_goes_into_a_top_end(selesnya):
    """Ramp needs something to ramp into: Emrakul and Ruric Thar, not a twelfth two-drop."""
    deck = _names(selesnya)
    assert deck["Emrakul, the Exigent Doom"] == 1
    assert deck["Ruric Thar, Magecrusher"] == 1
    big = [name for name in deck if (PRIMER.card(pro1.ID[name]).cmc or 0) >= 7]
    assert len(big) >= 2


def test_selesnya_mana_matches_the_pro(selesnya):
    pro_cards, pro_basics = _pro(pro1)
    assert abs(_land_count(selesnya) - 17) <= 1
    deck = _names(selesnya)
    # The colorless land that re-prepares his four "enters prepared" creatures.
    assert deck["Hexhaven Dueling Arena"] == 1 and deck["Vigorbloom Annex"] == 1
    assert sum(selesnya["basic_lands"].values()) == sum(pro_basics.values())


@pytest.mark.xfail(
    strict=True, reason="Ghalta (49.8% GIH) loses its slot to 55%+ cards; the pro's third ramp threat"
)
def test_selesnya_runs_the_pros_three_big_threats(selesnya):
    assert _names(selesnya)["Ghalta the Unstoppable"] == 1


@pytest.mark.xfail(strict=True, reason="pip-weighted basics give 5 Plains for early W two-drops; pro ran 7/8")
def test_selesnya_basics_split_like_the_pro(selesnya):
    _cards, pro_basics = _pro(pro1)
    for color, count in pro_basics.items():
        assert abs(selesnya["basic_lands"].get(color, 0) - count) <= 1, color


# -- pool 2: Azorius planeswalkers splashing green (5-1) -------------------------


def test_azorius_build_is_the_pros_forty(azorius):
    pro_cards, pro_basics = _pro(pro2)
    assert azorius["quality"]["colors"] == "WU" and azorius["quality"]["splash"] == "G"
    assert _names(azorius) == pro_cards
    assert _land_count(azorius) == 17
    assert azorius["basic_lands"].get("G") == pro_basics["G"] == 1
    for color in "WU":
        assert abs(azorius["basic_lands"][color] - pro_basics[color]) <= 1


def test_azorius_build_plays_the_walkers_and_skips_the_eight_mana_karn(azorius):
    deck = _names(azorius)
    for walker in ("Chandra, Chill of Compliance", "Ajani Resolute", "Saheeli, Consul of Oversight"):
        assert deck[walker] == 1
    assert deck["Karn, Gilded Guardian"] == 0
    assert deck["Mind Meanderer"] == 1
