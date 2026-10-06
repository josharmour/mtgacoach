"""Deck-strength model: set-relative features, monotone predictions, percentiles, robustness."""

from __future__ import annotations

import json
import logging
from collections import Counter
from types import SimpleNamespace

import pytest

from arenamcp.deck_strength import (
    FEATURES,
    MODEL_PATH,
    SetContext,
    deck_features,
    evaluate_build,
    evaluate_deck,
    load_model,
    log_build_strength,
    percentile,
    predict_win_rate,
    set_context,
    set_context_from_primer,
)
from arenamcp.limited_deck import fallback_deck


def card(grp_id, name, cost, *, gih=0.56, type_line="Creature — Soldier", text="Vigilance", rarity="common"):
    return {
        "grp_id": grp_id,
        "name": name,
        "mana_cost": cost,
        "type_line": type_line,
        "oracle_text": text,
        "rarity": rarity,
        "gih_wr": gih,
    }


def basic(grp_id, color):
    land = {"W": "Plains", "U": "Island", "B": "Swamp", "R": "Mountain", "G": "Forest"}[color]
    return card(
        grp_id, land, "", gih=None, type_line=f"Basic Land — {land}", text=f"({{T}}: Add {{{color}}}.)"
    )


def two_color_deck(gih=0.56, removal_gih=0.58):
    """23 blue-red spells (15 creatures, 3 removal) and 17 basics."""
    deck = [card(i, f"Wisp {i}", "{1}{U}", gih=gih, text="Flying") for i in range(8)]
    deck += [card(10 + i, f"Pup {i}", "{2}{R}", gih=gih) for i in range(7)]
    deck += [
        card(
            20 + i,
            f"Bolt {i}",
            "{1}{R}",
            gih=removal_gih,
            type_line="Instant",
            text="Bolt deals 3 damage to target creature.",
        )
        for i in range(3)
    ]
    deck += [
        card(30 + i, f"Study {i}", "{3}{U}", gih=gih, type_line="Sorcery", text="Draw two cards.")
        for i in range(5)
    ]
    deck += [basic(100 + i, "U") for i in range(9)] + [basic(120 + i, "R") for i in range(8)]
    return deck


SET_CARDS = [
    card(500 + i, f"Set card {i}", "{2}", gih=0.53 + 0.003 * i, type_line="Creature — Ox") for i in range(21)
]
CTX = set_context(SET_CARDS, {"UR": 0.55, "WU": 0.56, "BR": 0.54, "WG": 0.55})


def test_features_count_the_deck():
    f = deck_features(two_color_deck(), CTX)
    assert set(f) == set(FEATURES)
    assert f["creatures"] == 15 and f["removal"] == 3
    assert f["lands"] == 0 and f["land_dev"] == 0 and f["extra_cards"] == 0
    assert f["splash_colors"] == 0 and f["splash_cards"] == 0 and f["mono"] == 0
    assert f["two_drops"] == 11 and f["top_end"] == 0
    assert f["pair_rate"] == pytest.approx(0.0)  # UR is exactly the set's pair average


def test_basic_lands_may_be_cards_or_counts():
    deck = two_color_deck()
    spells = [c for c in deck if "Land" not in c["type_line"]]
    as_cards = deck_features(deck, CTX)
    as_counts = deck_features(spells, CTX, {"U": 9, "R": 8})
    assert as_cards == as_counts


def test_splash_is_detected_with_its_sources():
    deck = two_color_deck()
    deck[0] = card(90, "Grave Tyrant", "{4}{B}", gih=0.65, rarity="mythic")
    f = deck_features(deck, CTX)
    assert f["splash_colors"] == 1 and f["splash_cards"] == 1
    assert f["splash_short"] == 3  # no black source at all
    deck[-1] = basic(140, "B")
    assert deck_features(deck, CTX)["splash_short"] == 2


def test_better_cards_predict_a_higher_win_rate():
    weak = evaluate_deck(two_color_deck(gih=0.54), CTX, "sealed")
    strong = evaluate_deck(two_color_deck(gih=0.58), CTX, "sealed")
    assert strong.win_rate > weak.win_rate
    assert strong.percentile >= weak.percentile
    assert 0.3 < weak.win_rate < strong.win_rate < 0.8


def test_an_extra_color_costs_consistency():
    plain = evaluate_deck(two_color_deck(), CTX, "sealed")
    deck = two_color_deck()
    deck[0] = card(90, "Off-color Wisp", "{1}{B}", gih=0.56, text="Flying")
    deck[-1] = basic(140, "B")
    assert evaluate_deck(deck, CTX, "sealed").win_rate < plain.win_rate


def test_both_formats_and_aliases_predict():
    deck = two_color_deck()
    sealed = evaluate_deck(deck, CTX, "Sealed")
    draft = evaluate_deck(deck, CTX, "PremierDraft")
    assert sealed.fmt == "sealed" and draft.fmt == "draft"
    assert "vs 17Lands sealed decks" in sealed.summary() and sealed.summary().endswith("percentile")


def test_percentile_lookup_interpolates_and_clamps():
    model = {
        "formats": {
            "draft": {
                "features": [],
                "mean": [],
                "scale": [],
                "coef": [],
                "intercept_play": 0.0,
                "intercept_draw": 0.0,
                "percentiles": [0.40 + 0.002 * i for i in range(101)],
            }
        }
    }
    assert percentile(0.30, "draft", model) == 0.0
    assert percentile(0.70, "draft", model) == 100.0
    assert percentile(0.50, "draft", model) == pytest.approx(50.0)
    assert percentile(0.501, "draft", model) == pytest.approx(50.5)
    assert predict_win_rate({}, "draft", model) == pytest.approx(0.5)


def test_model_file_is_consistent():
    model = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
    assert set(model["formats"]) == {"sealed", "draft"}
    for params in model["formats"].values():
        n = len(params["features"])
        assert n and len(params["coef"]) == len(params["mean"]) == len(params["scale"]) == n
        assert set(params["features"]) <= set(FEATURES)
        table = params["percentiles"]
        assert len(table) == 101 and table == sorted(table)
    assert load_model() is load_model()  # cached


def test_unknown_and_unrated_cards_do_not_break_scoring():
    deck = two_color_deck()
    deck[0] = {"grp_id": 1, "name": "Mystery"}  # no rules, no rating
    deck[1] = card(2, "Fresh Rare", "{2}{U}", gih=None, rarity="rare")
    deck[2] = card(3, "Odd Rating", "{1}{R}", gih="n/a")
    strength = evaluate_deck(deck, CTX, "draft")
    assert 0.0 < strength.win_rate < 1.0 and 0.0 <= strength.percentile <= 100.0
    assert evaluate_deck([], SetContext(), "sealed").win_rate > 0


def test_context_needs_enough_ratings_and_reads_primers():
    assert set_context(SET_CARDS[:3]).rated == 3
    assert set_context(SET_CARDS[:3]).gih_mean == SetContext().gih_mean  # too few: generic scale
    primer = SimpleNamespace(
        cards={
            c["grp_id"]: SimpleNamespace(
                gih_wr=c["gih_wr"],
                types="Creature — Ox",
                rarity="C",
                games=1000,
                name=c["name"],
                game_wr=None,
            )
            for c in SET_CARDS
        },
        pair_stats={"UR": {"win_rate": 0.55, "games": 100}},
    )
    ctx = set_context_from_primer(primer)
    assert ctx.rated == len(SET_CARDS) and ctx.gih_mean == pytest.approx(CTX.gih_mean)


def test_builder_output_scores_and_logs(caplog):
    pool = two_color_deck()
    pool = [c for c in pool if "Land" not in c["type_line"]]
    pool += [card(200 + i, f"Chaff {i}", "{2}{G}", gih=0.50) for i in range(10)]
    build = fallback_deck(pool, {"UR": 0.55}, fmt="sealed")
    strength = evaluate_build(build, pool, CTX, "sealed")
    assert strength.features["lands"] + 17 == build["land_count"]
    with caplog.at_level(logging.INFO, logger="arenamcp.deck_strength"):
        lines = log_build_strength(build, build["deck_options"], pool, "sealed", None)
    assert lines[0].startswith("Deck strength (sealed): predicted ") and "percentile" in lines[0]
    assert any("Deck strength (sealed)" in r.getMessage() for r in caplog.records)
    assert sum(line.startswith("Deck option") for line in lines) == len(build["deck_options"])


def test_logging_never_raises():
    lines = log_build_strength({"main_deck": [{"grp_id": 1, "count": "x"}]}, None, [{"grp_id": 1}], "sealed")
    assert lines and lines[0].startswith("Deck strength unavailable")


def _run_deck_step(caplog):
    """One deck-builder step of the event driver on a scripted bridge; returns bridge commands sent."""
    from arenamcp.draft_event import DraftEventDriver
    from arenamcp.event_course import CourseTracker

    pool = [c for c in two_color_deck() if "Land" not in c["type_line"]]
    pool += [card(200 + i, f"Chaff {i}", "{2}{G}", gih=0.50) for i in range(40)]
    by_id = {c["grp_id"]: c for c in pool}
    replies = {
        "get_screen": {"ok": True, "deck_builder": True},
        "get_limited_pool": {
            "ok": True,
            "main_deck": [],
            "sideboard": [{"grp_id": g, "count": n} for g, n in Counter(c["grp_id"] for c in pool).items()],
            "basics_in_pool": {"7001": 99, "7002": 99},
        },
        "set_limited_deck": {"ok": True},
        "submit_limited_deck": {"ok": True},
    }
    sent = []

    class Bridge:
        connected = True

        def draft_command(self, action, **fields):
            sent.append(action)
            return replies.get(action, {"ok": False, "error": "unscripted"})

    bridge = Bridge()
    d = DraftEventDriver(
        bridge_fn=lambda: bridge,
        tracker_fn=lambda: CourseTracker(),
        primer_service=SimpleNamespace(
            get=lambda code: None, ensure=lambda code: None, quick=lambda code: None
        ),
        card_db=SimpleNamespace(get_card=lambda g: SimpleNamespace(name=f"Card {g}", expansion_code="TST")),
        pool_cards_fn=lambda ids, code: [by_id[g] for g in ids],
    )
    d.set_enabled(True)
    d.run.event_name = "Sealed_TST_20261001"
    d._basics = {7001: "U", 7002: "R"}
    with caplog.at_level(logging.INFO):
        d._step()
    return sent


def test_draft_event_logs_strength_and_still_submits(caplog):
    sent = _run_deck_step(caplog)
    assert "submit_limited_deck" in sent
    assert any(r.getMessage().startswith("Deck strength (sealed): predicted") for r in caplog.records)


def test_draft_event_submits_even_if_scoring_explodes(caplog, monkeypatch):
    import arenamcp.deck_strength as deck_strength

    def boom(*args, **kwargs):
        raise RuntimeError("model missing")

    monkeypatch.setattr(deck_strength, "log_build_strength", boom)
    sent = _run_deck_step(caplog)
    assert "submit_limited_deck" in sent
    assert any("Deck strength unavailable" in r.getMessage() for r in caplog.records)


def test_pair_rates_ignore_splash_rows():
    """'Azorius (WU) + Splash' used to overwrite the plain Azorius rate."""
    from arenamcp.draftstats import DraftStatsCache

    rows = [
        {"is_summary": False, "color_name": "Azorius (WU)", "short_name": "WU", "wins": 580, "games": 1000},
        {
            "is_summary": False,
            "color_name": "Azorius (WU) + Splash",
            "short_name": "WU+",
            "wins": 50,
            "games": 100,
        },
        {"is_summary": False, "color_name": "Jeskai (WUR)", "short_name": "WUR", "wins": 52, "games": 100},
        {"is_summary": True, "color_name": "All Decks", "short_name": "All", "wins": 1, "games": 2},
    ]
    stats = DraftStatsCache.__new__(DraftStatsCache)._parse_color_data(rows)
    assert stats["WU"].win_rate == pytest.approx(0.58) and stats["WUR"].games == 100


def test_mtga_database_finds_cards_by_name(tmp_path):
    import sqlite3

    from arenamcp.mtgadb import MTGADatabase

    path = tmp_path / "Raw_CardDatabase_test.mtga"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE Cards (GrpId INTEGER, TitleId INTEGER, ExpansionCode TEXT, IsToken INTEGER)")
    con.execute("CREATE TABLE Localizations_enUS (LocId INTEGER, Loc TEXT, Formatted INTEGER)")
    con.executemany(
        "INSERT INTO Cards VALUES (?, ?, ?, ?)",
        [(100, 1, "M21", 0), (200, 1, "FDN", 0), (300, 2, "BLB", 0), (400, 1, "FDN", 1)],
    )
    con.executemany(
        "INSERT INTO Localizations_enUS VALUES (?, ?, 1)",
        [(1, "Llanowar Elves"), (2, "<nobr>Season-Bound</nobr>")],
    )
    con.commit()
    con.close()
    db = MTGADatabase(path)
    assert db.grp_ids_by_name("llanowar elves", "M21") == [100, 200]
    assert db.grp_ids_by_name("Llanowar Elves") == [200, 100]  # newest printing first; tokens excluded
    assert db.grp_ids_by_name("Season-Bound // Back Face") == [300]
    assert db.grp_ids_by_name("Nope") == []
