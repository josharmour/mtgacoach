"""Alchemy drafts and the 2026-10-18 pro draft (tests/fra_alchemy_draft_2026_10_18.py).

Arena names the event "PremierDraft_YFRA_20261013"; 17lands files it as Y26FRA.
Before 2026-10-18 the set code came back empty (only three-letter codes were
accepted), so a hand-drafted Alchemy pod got no 17lands data at all. Y26FRA hides
win rates below ~500 games, so paper cards borrow FRA's; pick order (ALSA) and pair
rates come from the Alchemy format itself.

The second half replays a draft a professional player made by hand (Selesnya
around Emrakul, Garruk and the Alchemy lifegain cards; it won its first match).
Picks where the ranker already agrees are regressions; the lessons it has not
learned yet are strict xfails, so fixing one forces the marker off.
"""

from __future__ import annotations

import sqlite3

import pytest
from tests import fra_alchemy_draft_2026_10_18 as fx

import arenamcp.draftstats as draftstats
import arenamcp.limited_formats as limited_formats
from arenamcp.draft_autopick import (
    _set_stats,
    card_value,
    color_openness,
    color_signals,
    note_openness,
    rank_pack,
)
from arenamcp.draftstate import DraftState, _handle_event_start, extract_set_code
from arenamcp.draftstats import DraftStatsCache, alchemy_expansion, merge_alchemy_rows, ratings_expansion
from arenamcp.mtgadb import MTGADatabase
from arenamcp.set_primer import SetPrimerService

PRIMER = fx.primer()


@pytest.fixture(autouse=True)
def _no_event_expansions(monkeypatch):
    monkeypatch.setattr(limited_formats, "_event_expansions", {})


# -- event name -> 17lands expansion ------------------------------------------------


def test_alchemy_event_names_the_base_set_and_the_17lands_expansion():
    assert extract_set_code(fx.EVENT_NAME) == "FRA"
    assert alchemy_expansion(fx.EVENT_NAME) == ("FRA", "Y26FRA")
    assert alchemy_expansion("PremierDraft_YEOE_20250805") == ("EOE", "Y25EOE")
    assert extract_set_code("PremierDraft_FRA_20260929") == "FRA"
    assert alchemy_expansion("PremierDraft_FRA_20260929") is None


def test_the_draft_starting_points_ratings_at_y26fra_and_a_paper_event_points_back():
    state = DraftState()
    _handle_event_start(state, {"EventName": fx.EVENT_NAME})
    assert state.set_code == "FRA"
    assert ratings_expansion("FRA") == "Y26FRA"

    _handle_event_start(state, {"EventName": "PremierDraft_FRA_20260929"})
    assert ratings_expansion("FRA") == "FRA"


# -- 17lands download and merge ---------------------------------------------------


def _row(name, mtga_id, *, gih=None, games=0, alsa=None, iwd=None):
    return {
        "name": name,
        "mtga_id": mtga_id,
        "color": "G",
        "rarity": "uncommon",
        "types": ["Creature"],
        "avg_seen": alsa,
        "avg_pick": None,
        "ever_drawn_win_rate": gih,
        "ever_drawn_game_count": games,
        "drawn_improvement_win_rate": iwd,
        "win_rate": None,
        "game_count": games,
    }


ALCHEMY_ROWS = [
    _row("Heartwood Crafter", 106339, games=164, alsa=3.88),
    _row("Dina, Flesh Sower", 107787, games=137, alsa=3.48),
    _row("Mindseeker Oculus", 106360, gih=0.652, games=655, alsa=3.5),
]
PAPER_ROWS = [
    _row("Heartwood Crafter", 106339, gih=0.5818, games=1662, alsa=4.1, iwd=0.04),
    _row("Mindseeker Oculus", 106360, gih=0.612, games=3000, alsa=3.4),
]


def test_alchemy_rows_borrow_hidden_win_rates_with_their_sample_sizes():
    merged = {row["name"]: row for row in merge_alchemy_rows(ALCHEMY_ROWS, PAPER_ROWS)}
    crafter = merged["Heartwood Crafter"]
    assert crafter["ever_drawn_win_rate"] == 0.5818
    assert crafter["ever_drawn_game_count"] == 1662
    assert crafter["format_game_count"] == 164
    assert crafter["avg_seen"] == 3.88  # pick order stays the Alchemy format's
    # Alchemy's own win rate wins once 17lands shows it.
    assert merged["Mindseeker Oculus"]["ever_drawn_win_rate"] == 0.652
    assert "format_game_count" not in merged["Mindseeker Oculus"]
    # A digital-only card keeps its pick order and stays unrated.
    assert merged["Dina, Flesh Sower"]["ever_drawn_win_rate"] is None
    assert merged["Dina, Flesh Sower"]["avg_seen"] == 3.48


class _Response:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _fake_17lands(urls):
    def get(url, timeout=0):
        urls.append(url)
        if "color_ratings" in url:
            games = 400 if "Y26FRA" in url else 9000
            return _Response(
                [
                    {"is_summary": False, "color_name": "Selesnya (GW)", "wins": games // 2, "games": games},
                    {"is_summary": False, "color_name": "Azorius (WU)", "wins": games // 2, "games": games},
                ]
            )
        return _Response(ALCHEMY_ROWS if "expansion=Y26FRA" in url else PAPER_ROWS)

    return get


def test_the_cache_downloads_y26fra_during_an_alchemy_draft(tmp_path, monkeypatch):
    urls: list[str] = []
    monkeypatch.setattr(draftstats.requests, "get", _fake_17lands(urls))
    draftstats.use_event_expansion(fx.EVENT_NAME)
    cache = DraftStatsCache(cache_dir=tmp_path)

    crafter = cache.get_draft_rating("Heartwood Crafter", "FRA")
    dina = cache.get_draft_rating("Dina, Flesh Sower", "FRA")

    assert any("expansion=Y26FRA&format=PremierDraft" in url for url in urls)
    assert crafter.gih_wr == 0.5818 and crafter.alsa == 3.88
    assert dina is not None and dina.gih_wr is None and dina.alsa == 3.48
    assert (tmp_path / "Y26FRA_PremierDraft.json").exists()


def test_pair_rates_stay_on_the_base_set_while_the_alchemy_format_is_thin(tmp_path, monkeypatch):
    urls: list[str] = []
    monkeypatch.setattr(draftstats.requests, "get", _fake_17lands(urls))
    draftstats.use_event_expansion(fx.EVENT_NAME)

    pairs = DraftStatsCache(cache_dir=tmp_path).get_color_pair_stats("FRA")

    assert any("expansion=Y26FRA" in url and "color_ratings" in url for url in urls)
    assert pairs["WG"].games == 9000  # 800 Alchemy two-color games < MIN_ALCHEMY_PAIR_GAMES


def test_an_alchemy_draft_gets_its_own_primer_built_from_the_base_code(tmp_path):
    asked: list[str] = []

    def ratings(code):
        asked.append(code)
        return merge_alchemy_rows(ALCHEMY_ROWS, PAPER_ROWS)

    service = SetPrimerService(
        backend_fn=lambda: None, ratings_fn=ratings, color_stats_fn=lambda code: {}, cache_dir=tmp_path
    )
    draftstats.use_event_expansion(fx.EVENT_NAME)

    primer = service.quick("FRA")

    assert asked == ["FRA"]
    assert primer.set_code == "Y26FRA"
    assert primer.card(107787).name == "Dina, Flesh Sower"
    assert primer.card(106339).format_games == 164


# -- Arena card text ------------------------------------------------------------------


def test_card_text_comes_once_and_without_arena_markup(tmp_path):
    path = tmp_path / "carddb.sqlite"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE Cards (GrpId INTEGER PRIMARY KEY, TitleId INTEGER, Types TEXT, Power TEXT,
            Toughness TEXT, Colors TEXT, IsToken INTEGER, ExpansionCode TEXT, AbilityIds TEXT,
            Order_Title TEXT);
        CREATE TABLE Localizations_enUS (LocId INTEGER, Loc TEXT, Formatted INTEGER);
        CREATE TABLE Abilities (Id INTEGER PRIMARY KEY, TextId INTEGER);
        """
    )
    con.execute(
        "INSERT INTO Cards VALUES (107787, 1, 'Creature', '1', '1', '1,5', 0, 'Y26', '12:38,209954:1139987', '')"
    )
    gain = "Whenever you gain life, CARDNAME perpetually gets {}."
    con.executemany(
        "INSERT INTO Localizations_enUS VALUES (?, ?, ?)",
        [
            (1, "Dina, Flesh Sower", 1),
            (38, "Lifelink", 1),
            (1139987, gain.format("+1/+1"), 0),
            (1139987, gain.format("<nobr>+1/+1</nobr>"), 1),
            (1139987, gain.format("+1/+1"), 2),
        ],
    )
    con.execute("INSERT INTO Abilities VALUES (209954, 1139987)")
    con.commit()
    con.close()

    db = MTGADatabase(db_path=path)
    try:
        assert db.get_card(107787).oracle_text.split("\n") == ["Lifelink", gain.format("+1/+1")]
        assert db.get_ability_text(209954) == gain.format("+1/+1")
    finally:
        db.close()


# -- the pro's draft ----------------------------------------------------------------


def signals_at(key: str) -> dict[str, float]:
    store: dict = {}
    for seen in fx.ORDER[: fx.ORDER.index(key) + 1]:
        pack_no, pick_no = (int(part) for part in seen[1:].split("p"))
        note_openness(store, pack_no, color_openness(fx.PACKS[seen], pick_no, PRIMER))
    return color_signals(store, int(key[1:].split("p")[0]))


def ranked(key: str):
    pack_no, pick_no = (int(part) for part in key[1:].split("p"))
    return rank_pack(
        fx.PACKS[key],
        fx.pool_before(key),
        PRIMER,
        pack_number=pack_no,
        pick_number=pick_no,
        names=fx.NAMES,
        mana_costs=fx.COSTS,
        signals=signals_at(key),
    )


def position(key: str, name: str) -> int:
    return [pick.name for pick in ranked(key)].index(name)


def test_the_fixture_is_the_draft_and_deck_arena_recorded():
    assert len(fx.ORDER) == 45 and sum(fx.MAIN_DECK.values()) == 40
    main = {fx.NAMES[grp_id] for grp_id in fx.MAIN_DECK}
    assert {"Emrakul, the Exigent Doom", "Garruk, Curse Breaker", "Dina, Flesh Sower"} <= main
    assert all(grp_id in fx.pool() for grp_id in fx.MAIN_DECK if fx.NAMES[grp_id] not in fx.BASICS.values())


def test_alchemy_cards_are_rated_by_pick_order_not_called_rarely_played():
    # 2026-10-18: against paper cards carrying FRA's ~4,000-game samples, Dina's
    # 137 games in a six-day-old format read as "11% of a typical card's games".
    stats = _set_stats(PRIMER)
    for name in ("Dina, Flesh Sower", "Flourishing Forces", "Prophet of the Planeswalker"):
        _, reason, rated = card_value(PRIMER.by_name()[name.lower()], stats)
        assert not rated and "rarely played" not in reason, (name, reason)


@pytest.mark.parametrize(
    "key, name",
    [
        ("P2p6", "Way of the Wildspeaker"),
        ("P2p7", "Marwyn, the Preserver"),
        ("P2p11", "Campus Crier"),
        ("P3p2", "Heartwood Crafter"),
        ("P3p12", "Something Worth Saving"),
    ],
)
def test_the_ranker_agrees_with_the_pro(key, name):
    assert fx.NAMES[fx.PICKS[key]] == name
    assert position(key, name) == 0


@pytest.mark.parametrize(
    "key, name",
    [
        ("P1p2", "Garruk, Curse Breaker"),
        ("P1p10", "Konstrari Improviser"),
        ("P2p4", "Teyo, Lightshield Expert"),
    ],
)
def test_the_pros_pick_is_a_close_second(key, name):
    assert position(key, name) <= 1


def test_flourishing_forces_is_a_contender_in_the_lifegain_pool():
    assert position("P3p1", "Flourishing Forces") <= 1


@pytest.mark.parametrize(
    "key, name",
    [
        ("P2p1", "Dina, Flesh Sower"),  # 17lands drafters' earliest Alchemy uncommon (ALSA 3.5)
        ("P2p2", "Enlightened Confidant"),
        ("P3p5", "Valgavoth, the Lifeline"),
    ],
)
def test_alchemy_lifegain_cards_lead_their_packs_for_the_lifegain_pool(key, name):
    # 2026-10-18: unrated Alchemy cards were capped at 0.25 of deck credit and read as
    # "assumed below average"; in a young format unrated says nothing about play.
    assert position(key, name) == 0


def test_life_gained_this_turn_or_game_is_a_lifegain_payoff():
    from arenamcp.limited_rules import rules_profile

    def profile(name):
        card = PRIMER.by_name()[name.lower()]
        return rules_profile({"type_line": card.types, "oracle_text": card.oracle})

    for name in (
        "Flourishing Forces",
        "Germinate Recruits",
        "Valgavoth, the Lifeline",
        "Enlightened Confidant",
    ):
        assert "lifegain" in profile(name)["payoffs"], name
    assert "lifegain" in profile("Dina, Flesh Sower")["enables"]


@pytest.mark.xfail(
    strict=True, reason="planeswalker-payoff credit outweighs the lifegain engine the pool is built on"
)
def test_the_lifegain_payoff_beats_a_planeswalker_payoff_for_this_pool():
    # The pro took Flourishing Forces over Way of the Healer (60.1% GIH, earned in Jace
    # decks). The pool does make Jace tokens (Way of the Wildspeaker, Academic Ascent,
    # Campus Crier), but its engine is lifegain: Dina, Confidant, Germinate Recruits.
    assert position("P3p1", "Flourishing Forces") < position("P3p1", "Way of the Healer")


@pytest.mark.xfail(
    strict=True, reason="Heartwood Crafter's mana casting Emrakul from exile is not a known link"
)
def test_heartwood_crafter_beats_tomik_for_the_ramp_pool():
    # Crafter's mana "can't be spent to cast spells from your hand", but Emrakul is cast
    # from exile through its own {3} ability; Tomik's credit is a planeswalker payoff.
    assert position("P3p7", "Heartwood Crafter") < position("P3p7", "Tomik, Orzhov Lawmage")
