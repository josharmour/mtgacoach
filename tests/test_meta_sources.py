"""Focused regression tests for multi-source metagame clients and DeckBuilder source filter."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

from arenamcp import deck_candidates
from arenamcp.meta_sources import (
    AetherHubClient,
    MoxfieldClient,
    MTGTop8Client,
    _parse_plain_deck_text,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_parse_plain_deck_text_brawl_and_standard():
    brawl_txt = "\n".join(
        [
            "4 Plains",
            "1 Command Tower",
            "",
            "1 Sample Commander",
        ]
    )
    parsed_brawl = _parse_plain_deck_text(brawl_txt, is_brawl=True)
    assert parsed_brawl["mainboard"] == ["4 Plains", "1 Command Tower"]
    assert parsed_brawl["commander"] == ["1 Sample Commander"]
    assert parsed_brawl["sideboard"] == []

    std_txt = "\n".join(
        [
            "20 Plains",
            "4 Sample Creature",
            "",
            "2 Sample Sideboard",
        ]
    )
    parsed_std = _parse_plain_deck_text(std_txt, is_brawl=False)
    assert parsed_std["mainboard"] == ["20 Plains", "4 Sample Creature"]
    assert parsed_std["sideboard"] == ["2 Sample Sideboard"]
    assert parsed_std["commander"] == []


def test_aetherhub_parse_metagame_html(tmp_path):
    html = """
    <table>
      <tr>
        <td></td>
        <td><a href="/Metagame/Standard-BO1/Deck/deck-one-1001">Deck One</a></td>
        <td>300 matches</td>
      </tr>
      <tr>
        <td></td>
        <td><a href="/Metagame/Standard-BO1/Deck/deck-two-1002">Deck Two</a></td>
        <td>100 matches</td>
      </tr>
      <tr>
        <td></td>
        <td><a href="/Metagame/Standard-BO1/Deck/deck-one-1001">Deck One</a></td>
        <td>300 matches</td>
      </tr>
    </table>
    """
    client = AetherHubClient(cache_dir=tmp_path)
    rows = client._parse_metagame_html(html)
    assert len(rows) == 2
    assert rows[0]["name"] == "Deck One"
    assert rows[0]["deck_id"] == "1001"
    assert rows[0]["meta_share"] == "75.0%"
    assert rows[1]["meta_share"] == "25.0%"


def test_mtgtop8_parse_metagame_html(tmp_path):
    html = """
    <div align="center" style="width:100%;">
      <div class="S14"><a href="archetype?a=346&amp;meta=50&amp;f=ST">Alpha Aggro</a></div>
      <div>
        <div class="S14" style="display:inline-block;width:48%;">11 %</div>
      </div>
    </div>
    """
    client = MTGTop8Client(cache_dir=tmp_path)
    rows = client._parse_metagame_html(html)
    assert len(rows) == 1
    assert rows[0]["name"] == "Alpha Aggro"
    assert rows[0]["meta_share"] == "11%"
    assert rows[0]["url"] == "https://www.mtgtop8.com/archetype?a=346&meta=50&f=ST"


def test_moxfield_search_and_deck_extraction(monkeypatch, tmp_path):
    client = MoxfieldClient(cache_dir=tmp_path)

    search_resp = MagicMock()
    search_resp.raise_for_status.return_value = None
    search_resp.json.return_value = {
        "data": [
            {"publicId": "incomplete1", "name": "Too Small", "mainboardCount": 25},
            {"publicId": "valid1", "name": "Valid Standard", "mainboardCount": 60},
        ]
    }
    deck_resp = MagicMock()
    deck_resp.raise_for_status.return_value = None
    deck_resp.json.return_value = {
        "boards": {
            "mainboard": {"cards": {"a": {"quantity": 60, "card": {"name": "Plains"}}}},
            "sideboard": {"cards": {"b": {"quantity": 2, "card": {"name": "Island"}}}},
            "commanders": {"cards": {}},
            "companions": {"cards": {}},
        }
    }

    def fake_get(url, **_kwargs):
        return search_resp if "/v2/decks/search" in url else deck_resp

    monkeypatch.setattr("arenamcp.meta_sources.requests.get", fake_get)
    found = client.get_metagame("standard", limit=5)
    assert len(found) == 1
    assert found[0]["public_id"] == "valid1"

    deck = client.get_deck_list("valid1")
    assert deck["mainboard"] == ["60 Plains"]
    assert deck["sideboard"] == ["2 Island"]


def test_load_candidates_multi_source_and_resilience(monkeypatch):
    class FakeDB:
        def get_card_by_arena_id(self, _aid):
            return None

        def get_card_by_name(self, _name):
            return None

    class FakeGoldfish:
        def get_metagame(self, fmt):
            if fmt == "standard":
                return [{"name": "GF Deck", "url": "https://gf/1", "meta_share": "10%"}]
            return []

        def get_deck_list(self, _url):
            return {"mainboard": ["60 Plains"], "sideboard": [], "commander": [], "companion": []}

    class FakeAetherHub:
        def get_metagame(self, fmt):
            if fmt == "standard":
                return [{"name": "AH Deck", "url": "https://ah/1", "meta_share": "9.3%"}]
            return []

        def get_deck_list(self, _url, is_brawl=False):
            return {"mainboard": ["60 Forest"], "sideboard": [], "commander": [], "companion": []}

    class FailingTop8:
        def get_metagame(self, _fmt):
            raise OSError("network down")

    class FakeMoxfield:
        def get_metagame(self, fmt):
            if fmt == "standard":
                return [{"name": "MX Deck", "public_id": "abc", "url": "https://mox/abc", "meta_share": ""}]
            return []

        def get_deck_list(self, _pid):
            return {"mainboard": ["60 Island"], "sideboard": [], "commander": [], "companion": []}

    class EmptyArchidekt:
        def get_metagame(self, _fmt):
            return []

    class EmptyMTGAZone:
        def get_metagame(self, _fmt):
            return []

    monkeypatch.setattr(deck_candidates, "pricing_database", lambda: FakeDB())
    monkeypatch.setattr("arenamcp.mtggoldfish.MTGGoldfishClient", FakeGoldfish)
    monkeypatch.setattr("arenamcp.meta_sources.AetherHubClient", FakeAetherHub)
    monkeypatch.setattr("arenamcp.meta_sources.MTGTop8Client", FailingTop8)
    monkeypatch.setattr("arenamcp.meta_sources.MoxfieldClient", FakeMoxfield)
    monkeypatch.setattr("arenamcp.meta_sources.ArchidektClient", EmptyArchidekt)
    monkeypatch.setattr("arenamcp.meta_sources.MTGAZoneClient", EmptyMTGAZone)

    rows = deck_candidates.load_candidates(
        {},
        {"common": 0, "uncommon": 0, "rare": 0, "mythic": 0},
        formats=("standard",),
        sources=deck_candidates.ALL_SOURCES,
    )
    sites = {r["source_site"] for r in rows}
    assert sites == {"MTGGoldfish", "AetherHub", "Moxfield"}


def test_deck_builder_source_filter_and_paste_reset():
    from PySide6.QtWidgets import QApplication

    from arenamcp.desktop.deck_builder import DeckBuilderDialog

    _app = QApplication.instance() or QApplication([])
    dlg = DeckBuilderDialog(session=None)
    dlg._collection = {}
    dlg._wildcards = {"common": 0, "uncommon": 0, "rare": 0, "mythic": 0}
    dlg._candidates = [
        {
            "name": "AH List",
            "format": "Standard",
            "source_site": "AetherHub",
            "source": "AetherHub Standard (Arena tracker)",
            "source_rank": 1,
            "export": "Deck\n60 Plains",
            "cost": {
                "missing": {},
                "spend": {},
                "shortfall_wildcards": {},
                "unknown": [],
                "owned": True,
                "affordable": True,
            },
        },
        {
            "name": "GF List",
            "format": "Standard",
            "source_site": "MTGGoldfish",
            "source": "MTGGoldfish Standard (paper metagame)",
            "source_rank": 2,
            "export": "Deck\n60 Forest",
            "cost": {
                "missing": {},
                "spend": {},
                "shortfall_wildcards": {},
                "unknown": [],
                "owned": True,
                "affordable": True,
            },
        },
        {
            "name": "AZ Timeless List",
            "format": "Timeless",
            "source_site": "MTG Arena Zone",
            "source": "MTG Arena Zone Timeless (tier list)",
            "source_rank": 1,
            "export": "Deck\n60 Swamp",
            "cost": {
                "missing": {},
                "spend": {},
                "shortfall_wildcards": {},
                "unknown": [],
                "owned": True,
                "affordable": True,
            },
        },
    ]
    dlg.mode.setCurrentIndex(2)
    dlg.source_filter.setCurrentIndex(1)  # AetherHub
    assert [d["name"] for d in dlg._visible] == ["AH List"]

    az_idx = dlg.source_filter.findData("mtgarenazone")
    assert az_idx > 0
    dlg.source_filter.setCurrentIndex(az_idx)
    assert [d["name"] for d in dlg._visible] == ["AZ Timeless List"]

    # Pasting a deck resets source_filter to All sources so it is selectable.
    dlg._checking_paste = True
    pasted = {
        "name": "Pasted List",
        "format": "Standard",
        "source": "Pasted",
        "source_rank": 1,
        "export": "Deck\n60 Island",
        "cost": {
            "missing": {},
            "spend": {},
            "shortfall_wildcards": {},
            "unknown": [],
            "owned": True,
            "affordable": True,
        },
    }
    dlg._loaded([pasted], "")
    assert dlg.source_filter.currentIndex() == 0
    assert any(d["name"] == "Pasted List" for d in dlg._visible)
    dlg.close()


def test_url_host_validation_and_no_empty_cache_poisoning(monkeypatch, tmp_path):
    import pytest

    ah = AetherHubClient(cache_dir=tmp_path / "ah")
    # External host link must be rejected by _parse_metagame_html and get_deck_list.
    bad_html = '<table><tr><td><a href="https://evil.example/Metagame/Standard-BO1/Deck/x-99">Bad</a></td></tr></table>'
    assert ah._parse_metagame_html(bad_html) == []
    with pytest.raises(ValueError):
        ah.get_deck_list("https://evil.example/Metagame/Standard-BO1/Deck/x-99")

    t8 = MTGTop8Client(cache_dir=tmp_path / "t8")
    assert t8._parse_metagame_html('<a href="https://evil.example/archetype?a=1">Bad</a>') == []
    with pytest.raises(ValueError):
        t8.get_deck_list("https://evil.example/archetype?a=1")

    mx = MoxfieldClient(cache_dir=tmp_path / "mx")
    with pytest.raises(ValueError):
        mx.get_deck_list("https://evil.example/decks/abc123")
    with pytest.raises(ValueError):
        mx.get_deck_list("../bad/id")

    # Empty response must NOT poison the cache.
    empty_resp = MagicMock()
    empty_resp.raise_for_status.return_value = None
    empty_resp.text = "<html><body>challenge</body></html>"
    empty_resp.json.return_value = "not-a-dict"
    monkeypatch.setattr("arenamcp.meta_sources.requests.get", lambda *_a, **_kw: empty_resp)
    assert ah.get_metagame("standard") == []
    assert ah._cache.read("metagame_standard") is None
    assert mx.get_metagame("standard") == []
    assert mx._cache.read("search_standard") is None
