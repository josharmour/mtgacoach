"""Regression tests for Performance Dashboard filters/matchups and Deck Builder upgrades."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from arenamcp.desktop.performance_tab import compute_performance_summary, filter_records
from arenamcp.match_history import MatchRecord


def _rec(
    idx: int,
    result: str,
    *,
    fmt: str = "Ladder",
    days_ago: float = 0.2,
    turns: int = 6,
    opp_colors: list[str] | None = None,
    now: datetime,
) -> MatchRecord:
    return MatchRecord(
        match_id=f"m-{idx}",
        timestamp=(now - timedelta(days=days_ago)).isoformat(),
        result=result,
        format_name=fmt,
        turns=turns,
        opponent_colors_seen=list(opp_colors or []),
        autopilot=True,
    )


def test_performance_filter_records_by_window_and_format():
    now = datetime(2026, 11, 21, 22, 0, tzinfo=timezone.utc)
    recs = [
        _rec(1, "win", fmt="Ladder", days_ago=0.2, now=now),
        _rec(2, "loss", fmt="Brawl_Ladder", days_ago=2.0, now=now),
        _rec(3, "win", fmt="Ladder", days_ago=10.0, now=now),
        _rec(4, "loss", fmt="Historic_Play", days_ago=40.0, now=now),
    ]
    assert len(filter_records(recs, window_days=1, format_filter="all", now=now)) == 1
    assert len(filter_records(recs, window_days=7, format_filter="all", now=now)) == 2
    assert len(filter_records(recs, window_days=None, format_filter="standard", now=now)) == 2
    assert len(filter_records(recs, window_days=None, format_filter="brawl", now=now)) == 1
    assert len(filter_records(recs, window_days=None, format_filter="other", now=now)) == 1


def test_compute_performance_summary_streak_avg_turns_and_color_matchups():
    now = datetime(2026, 11, 21, 22, 0, tzinfo=timezone.utc)
    # Newest-first order: W, W, L, W -> current streak W2
    recs = [
        _rec(1, "win", turns=5, opp_colors=["W"], now=now),
        _rec(2, "win", turns=7, opp_colors=["W"], now=now),
        _rec(3, "loss", turns=8, opp_colors=["U", "B"], now=now),
        _rec(4, "win", turns=4, opp_colors=["R"], now=now),
    ]
    stats = compute_performance_summary(recs)
    assert stats["wins"] == 3
    assert stats["losses"] == 1
    assert stats["streak"] == "W2"
    assert stats["avg_turns"] == 6.0
    assert stats["matchups"][0]["colors"] == "W"
    assert stats["matchups"][0]["wins"] == 2
    assert stats["matchups"][0]["losses"] == 0


def test_deck_builder_format_filter_close_mode_and_loaded_count():
    from PySide6.QtWidgets import QApplication

    from arenamcp.desktop.deck_builder import DeckBuilderDialog

    app = QApplication.instance() or QApplication([])
    assert app is not None

    dlg = DeckBuilderDialog(session=None)
    dlg._collection = {}
    dlg._wildcards = {"common": 100, "uncommon": 100, "rare": 4, "mythic": 6}
    dlg._candidates = [
        {
            "name": "Owned Brawl",
            "format": "Brawl",
            "source_rank": 1,
            "source": "MTGGoldfish Brawl (Arena metagame)",
            "checked_at": "2026-11-21",
            "export": "",
            "cost": {
                "missing": {"CardA": 2},
                "spend": {"rare": 2},
                "shortfall_wildcards": {},
                "unknown": [],
                "owned": False,
                "affordable": True,
            },
        },
        {
            "name": "Close Standard",
            "format": "Standard",
            "source_rank": 2,
            "source": "MTGGoldfish Standard (paper metagame)",
            "checked_at": "2026-11-21",
            "export": "",
            "cost": {
                "missing": {"CardB": 7},
                "spend": {"rare": 7},
                "shortfall_wildcards": {"rare": 3},
                "unknown": [],
                "owned": False,
                "affordable": False,
            },
        },
        {
            "name": "Expensive Standard",
            "format": "Standard",
            "source_rank": 3,
            "source": "MTGGoldfish Standard (paper metagame)",
            "checked_at": "2026-11-21",
            "export": "",
            "cost": {
                "missing": {"CardC": 20},
                "spend": {"rare": 20},
                "shortfall_wildcards": {"rare": 16},
                "unknown": [],
                "owned": False,
                "affordable": False,
            },
        },
    ]

    # Mode 1 ("Within my wildcard balance"): 1 of 3 matches
    dlg.mode.setCurrentIndex(1)
    assert [d["name"] for d in dlg._visible] == ["Owned Brawl"]
    assert "1 of 3" in dlg.status.text()

    # Mode 3 ("Close (<= 4 extra wildcards)"): includes Owned Brawl + Close Standard (short 3 rare)
    dlg.mode.setCurrentIndex(3)
    assert {d["name"] for d in dlg._visible} == {"Owned Brawl", "Close Standard"}

    # Format filter "Standard" + Mode 2 ("All candidates")
    dlg.mode.setCurrentIndex(2)
    dlg.format_filter.setCurrentIndex(1)  # Standard
    assert [d["name"] for d in dlg._visible] == ["Close Standard", "Expensive Standard"]

    # Checking a pasted deck while format_filter is "Standard" resets format_filter and does not crash
    pasted = {
        "name": "Pasted Deck",
        "source": "Pasted",
        "checked_at": "2026-11-21",
        "export": "",
        "cost": {
            "missing": {},
            "spend": {},
            "shortfall_wildcards": {},
            "unknown": [],
            "owned": True,
            "affordable": True,
        },
    }
    dlg._checking_paste = True
    dlg._loaded([pasted], "")
    assert dlg._checking_paste is False
    assert dlg.format_filter.currentIndex() == 0
    assert any(d["name"] == "Pasted Deck" for d in dlg._visible)
    dlg.close()


def test_load_candidates_merges_full_metagame_dedups_and_labels_arena_formats(monkeypatch):
    import arenamcp.deck_candidates as dc
    import arenamcp.mtggoldfish as mg

    pages = {
        "standard": [
            {"name": "Std Main", "meta_share": "10%", "url": "u/std1", "colors": "W", "budget": False},
            {"name": "Std Budget", "meta_share": "", "url": "u/std2", "colors": "R", "budget": True},
        ],
        "standard/full": [
            {"name": "Std Main", "meta_share": "10%", "url": "u/std1", "colors": "W", "budget": False},
            {"name": "Std Extra", "meta_share": "2%", "url": "u/std3", "colors": "G", "budget": False},
        ],
        "brawl": [
            {"name": "Brawl Main", "meta_share": "6%", "url": "u/br1", "colors": "G", "budget": False},
        ],
        "brawl/full": RuntimeError("temporary /full outage"),
        "historic": [
            {"name": "Hist Burn", "meta_share": "", "url": "u/h1", "colors": "R", "budget": False},
        ],
    }

    class FakeClient:
        def get_metagame(self, fmt, force_refresh=False):
            val = pages[fmt]
            if isinstance(val, Exception):
                raise val
            return val

        def get_deck_list(self, url, force_refresh=False):
            if url == "u/br1":
                return {"commander": ["1 Cmdr"], "mainboard": ["99 Forest"], "sideboard": []}
            return {"mainboard": ["60 Plains"], "sideboard": []}

    class FakeCard:
        def __init__(self, name):
            self.name = name
            self.rarity = "common"
            self.arena_id = 70000

    class FakeDB:
        def get_card_by_arena_id(self, _aid):
            return None

        def get_card_by_name(self, name):
            return FakeCard(name)

    monkeypatch.setattr(mg.MTGGoldfishClient, "get_metagame", FakeClient.get_metagame)
    monkeypatch.setattr(mg.MTGGoldfishClient, "get_deck_list", FakeClient.get_deck_list)
    monkeypatch.setattr(dc, "pricing_database", lambda: FakeDB())

    out = dc.load_candidates(
        {70001: 0},
        {"common": 10, "uncommon": 0, "rare": 0, "mythic": 0},
        formats=("standard", "brawl", "historic"),
    )
    by_name = {c["name"]: c for c in out}
    # Deduplicated std1 + kept budget std2 + merged std3 + survived brawl/full error + included historic
    assert set(by_name) == {"Std Main", "Std Budget", "Std Extra", "Brawl Main", "Hist Burn"}
    assert by_name["Std Main"]["source"] == "MTGGoldfish Standard (paper metagame)"
    assert by_name["Brawl Main"]["source"] == "MTGGoldfish Brawl (Arena metagame)"
    assert by_name["Hist Burn"]["source"] == "MTGGoldfish Historic (Arena metagame)"
