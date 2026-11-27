"""Offline unit & integration tests for website/meta_sync.py and new desktop meta sources."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import httpx
from fastapi.testclient import TestClient
from tests.website_app import WEBSITE_DIR, load_website_app


def _make_site(tmp_path: Path, monkeypatch):
    (tmp_path / "templates").symlink_to(WEBSITE_DIR / "templates", target_is_directory=True)
    (tmp_path / "static").symlink_to(WEBSITE_DIR / "static", target_is_directory=True)
    app_mod, db, _patreon = load_website_app(tmp_path, monkeypatch)
    import meta_sync
    import profiles

    return app_mod, db, profiles, meta_sync


def test_mtgo_format_data_classifier_rules_variants_colors_and_untagged(tmp_path: Path, monkeypatch) -> None:
    _app_mod, db, profiles, meta_sync = _make_site(tmp_path, monkeypatch)

    # Badaro JSON files frequently include trailing commas
    raw_jsonc = """
    {
      "Name": "Prowess",
      "IncludeColorInName": true,
      "Conditions": [
        { "Type": "OneOrMoreInMainboard", "Cards": ["Monastery Swiftspear", "Slickshot Show-Off"] },
        { "Type": "DoesNotContain", "Cards": ["Omniscience"] },
      ],
      "Variants": [
        {
          "Name": "Jeskai Prowess",
          "IncludeColorInName": false,
          "Conditions": [
            { "Type": "TwoOrMoreInMainboard", "Cards": ["Lightning Helix", "SoulPartition", "Elspeth's Smite"] },
          ],
        },
      ],
    }
    """
    parsed = meta_sync._parse_jsonc(raw_jsonc)
    assert parsed["Name"] == "Prowess"

    clf = meta_sync.MTGOFormatDataClassifier(
        rules_by_format={"standard": [parsed]},
        card_colors={
            "monastery swiftspear": "R",
            "stormchaser's talent": "U",
            "lightning helix": "WR",
            "elspeth's smite": "W",
        },
    )

    arch1, col1 = clf.classify(
        "standard",
        [
            {"name": "Monastery Swiftspear", "count": 4},
            {"name": "Stormchaser's Talent", "count": 4},
        ],
    )
    assert col1 == "UR"
    assert arch1 == "Izzet Prowess"

    arch2, col2 = clf.classify(
        "standard",
        [
            {"name": "Monastery Swiftspear", "count": 4},
            {"name": "Lightning Helix", "count": 4},
            {"name": "Elspeth's Smite", "count": 2},
        ],
    )
    assert arch2 == "Jeskai Prowess"
    assert col2 == "WR"

    # Populate untagged player decks without overwriting already-tagged decks
    with db.get_db() as conn:
        profiles.ensure_profile(conn, "u1")
        profiles._upsert_deck_record(
            conn,
            "u1",
            name="Unlabeled Deck",
            fmt="standard",
            piles={
                "main": [
                    {"name": "Monastery Swiftspear", "count": 4},
                    {"name": "Stormchaser's Talent", "count": 4},
                    {"name": "Mountain", "count": 52},
                ],
                "sideboard": [],
                "commander": [],
                "companion": [],
            },
        )
        profiles._upsert_deck_record(
            conn,
            "u1",
            name="Manual Archetype",
            fmt="standard",
            archetype="Custom Brew",
            piles={
                "main": [{"name": "Monastery Swiftspear", "count": 4}, {"name": "Mountain", "count": 56}],
                "sideboard": [],
                "commander": [],
                "companion": [],
            },
        )

    updated = meta_sync.classify_untagged_player_decks(clf)
    assert updated == 1
    with db.get_db() as conn:
        rows = {
            r["name"]: r["archetype"]
            for r in conn.execute("SELECT name, archetype FROM synced_decks WHERE owner = 'u1'").fetchall()
        }
    assert rows["Unlabeled Deck"] == "Izzet Prowess"
    assert rows["Manual Archetype"] == "Custom Brew"


def test_parse_mtgazone_metaconfluence_archidekt_and_refresh_all(tmp_path: Path, monkeypatch) -> None:
    app_mod, _db, _profiles, meta_sync = _make_site(tmp_path, monkeypatch)

    mtgazone_html = """
    <div class="deck-block" id="uuid-tl1">
      <div class="name-container"><div class="name">Boros Energy Bo1 &amp; Knuckles</div></div>
      <div class="decklist short companion">
        <div class="card" data-name="Lurrus of the Dream-Den" data-quantity="1"></div>
      </div>
      <div class="decklist main">
        <div class="card" data-name="Guide of Souls" data-quantity="4"></div>
        <div class="card" data-name="Plains" data-quantity="56"></div>
      </div>
      <div class="decklist sideboard info"><h5 class="card__separator">1 Card</h5></div>
      <div class="decklist sideboard">
        <div class="card" data-name="Surgical Extraction" data-quantity="2"></div>
      </div>
    </div>
    """

    from datetime import datetime, timezone

    today = datetime.now(timezone.utc)
    ym = today.strftime("%Y/%m")
    day = today.strftime("%d")
    ymd = today.strftime("%Y-%m-%d")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/card_colors.json"):
            return httpx.Response(
                200, text='{"Lands": [], "NonLands": [{"Name": "Monastery Swiftspear", "Color": "R"},],}'
            )
        if url.endswith("/color_overrides.json"):
            return httpx.Response(404)
        if "/MTGOFormatData/contents/Formats/Standard/Archetypes" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "Prowess.json",
                        "download_url": "https://raw.githubusercontent.com/Badaro/MTGOFormatData/main/Formats/Standard/Archetypes/Prowess.json",
                    }
                ],
            )
        if "/MTGOFormatData/contents/Formats/Pioneer/Archetypes" in url:
            return httpx.Response(200, json=[])
        if url.endswith("/Prowess.json"):
            return httpx.Response(
                200,
                text='{"Name": "Mono-Red Prowess", "IncludeColorInName": false, "Conditions": [{"Type": "InMainboard", "Cards": ["Monastery Swiftspear"],}],}',
            )
        if f"/MTG_decklistcache/contents/Tournaments/MTGO/{ym}" in url and url.endswith(f"/{ym}"):
            return httpx.Response(200, json=[{"name": day, "type": "dir"}])
        if f"/MTG_decklistcache/contents/Tournaments/MTGO/{ym}/{day}" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "name": f"standard-league-{ymd}.json",
                        "download_url": f"https://raw.githubusercontent.com/fbettega/MTG_decklistcache/main/Tournaments/MTGO/{ym}/{day}/standard-league-{ymd}.json",
                    }
                ],
            )
        if "/MTG_decklistcache/contents/" in url:
            return httpx.Response(404)
        if url.endswith(f"standard-league-{ymd}.json"):
            return httpx.Response(
                200,
                json={
                    "Tournament": {"Name": "Standard League", "Date": ymd, "Formats": "Standard"},
                    "Decks": [
                        {
                            "Player": "TestPilot",
                            "Result": "5-0",
                            "AnchorUri": "https://www.mtgo.com/decklist/standard-league#TestPilot",
                            "Mainboard": [
                                {"Count": 4, "CardName": "Monastery Swiftspear"},
                                {"Count": 56, "CardName": "Mountain"},
                            ],
                            "Sideboard": [{"Count": 2, "CardName": "Abrade"}],
                        }
                    ],
                },
            )
        if url == "https://mtgazone.com/timeless-bo1-metagame-tier-list/":
            return httpx.Response(200, text=mtgazone_html)
        if url.startswith("https://mtgazone.com/"):
            return httpx.Response(404)
        if url == "https://metaconfluence.com/timeless-tier-list/":
            return httpx.Response(
                200,
                text='<a href="https://metaconfluence.com/timeless-deck-list/dimir-doomsday/">Doomsday</a>',
            )
        if url == "https://metaconfluence.com/competitive-brawl-tier-list/":
            return httpx.Response(200, text="<html></html>")
        if url == "https://metaconfluence.com/timeless-deck-list/dimir-doomsday/":
            return httpx.Response(200, text='<iframe src="https://moxfield.com/embed/doomsday123"></iframe>')
        if url == "https://api2.moxfield.com/v3/decks/all/doomsday123":
            return httpx.Response(
                200,
                json={
                    "name": "Dimir Doomsday",
                    "colorIdentity": ["U", "B"],
                    "boards": {
                        "mainboard": {
                            "cards": {
                                "a": {"quantity": 4, "card": {"name": "Dark Ritual", "rarity": "common"}},
                                "b": {"quantity": 56, "card": {"name": "Swamp", "rarity": "common"}},
                            }
                        },
                        "sideboard": {"cards": {}},
                        "commanders": {"cards": {}},
                        "companions": {"cards": {}},
                    },
                },
            )
        if "archidekt.com/api/decks/v3/?deckFormat=18" in url:
            return httpx.Response(200, json={"results": [{"id": 9001, "name": "Alchemy Heist", "size": 60}]})
        if "archidekt.com/api/decks/v3/" in url:
            return httpx.Response(429)  # verify 429 terminates cleanly without failure
        if url == "https://archidekt.com/api/decks/9001/":
            return httpx.Response(
                200,
                json={
                    "id": 9001,
                    "name": "Alchemy Heist",
                    "categories": [
                        {"name": "Creature", "includedInDeck": True},
                        {"name": "Maybeboard", "includedInDeck": False},
                    ],
                    "cards": [
                        {
                            "quantity": 60,
                            "categories": ["Creature"],
                            "card": {
                                "rarity": "uncommon",
                                "oracleCard": {"name": "Swamp", "colorIdentity": ["Black"]},
                            },
                        },
                        {
                            "quantity": 4,
                            "categories": ["Maybeboard"],
                            "card": {
                                "rarity": "rare",
                                "oracleCard": {"name": "Excluded Card", "colorIdentity": ["Blue"]},
                            },
                        },
                    ],
                },
            )
        return httpx.Response(404)

    async def _run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await meta_sync.refresh_all_meta_sources(force=True, client=client)

    summary = asyncio.run(_run())
    assert summary["ok"] is True
    assert summary["sources"]["mtgo_cache"] == 1
    assert summary["sources"]["mtgazone"] == 1
    assert summary["sources"]["metaconfluence"] == 1
    assert summary["sources"]["archidekt"] == 1
    assert summary["upsert"]["created"] == 4

    with TestClient(app_mod.app) as web_client:
        tl = web_client.get("/v1/decks/discover?format=timeless").json()["decks"]
        assert {d["name"] for d in tl} == {"Boros Energy Bo1 & Knuckles", "Dimir Doomsday"}

        alch = web_client.get("/v1/decks/discover?format=alchemy").json()["decks"]
        assert [d["name"] for d in alch] == ["Alchemy Heist"]
        assert all(c["name"] != "Excluded Card" for c in alch[0]["main"])

        # System sources never leak into player leaderboard
        lb = web_client.get("/v1/leaderboard?tab=rank&window=all").json()["entries"]
        assert lb == []


def test_desktop_archidekt_and_mtgazone_clients(monkeypatch, tmp_path: Path) -> None:
    from arenamcp.meta_sources import ArchidektClient, MTGAZoneClient

    arch = ArchidektClient(cache_dir=tmp_path / "arch")
    list_resp = MagicMock()
    list_resp.status_code = 200
    list_resp.raise_for_status.return_value = None
    list_resp.json.return_value = {"results": [{"id": 55, "name": "Alchemy Rabbits", "size": 60}]}
    detail_resp = MagicMock()
    detail_resp.raise_for_status.return_value = None
    detail_resp.json.return_value = {
        "categories": [{"name": "Deck", "includedInDeck": True}],
        "cards": [
            {"quantity": 60, "categories": ["Deck"], "card": {"oracleCard": {"name": "Plains"}}},
            {"quantity": 2, "categories": ["Maybeboard"], "card": {"oracleCard": {"name": "Ignored"}}},
        ],
    }
    monkeypatch.setattr(
        "arenamcp.meta_sources.requests.get",
        lambda url, **_kw: list_resp if "v3" in url else detail_resp,
    )
    meta = arch.get_metagame("alchemy")
    assert len(meta) == 1 and meta[0]["deck_id"] == "55"
    deck = arch.get_deck_list("55")
    assert deck["mainboard"] == ["60 Plains"]

    zone = MTGAZoneClient(cache_dir=tmp_path / "zone")
    z_resp = MagicMock()
    z_resp.raise_for_status.return_value = None
    z_resp.text = '<div class="deck-block" id="d1"><div class="name">Timeless Show</div><div class="decklist main"><div class="card" data-name="Swamp" data-quantity="60"></div></div></div>'
    monkeypatch.setattr("arenamcp.meta_sources.requests.get", lambda *_a, **_kw: z_resp)
    z_meta = zone.get_metagame("timeless")
    assert len(z_meta) == 1 and z_meta[0]["name"] == "Timeless Show"
    assert zone.get_deck_list(z_meta[0]["cache_key"])["mainboard"] == ["60 Swamp"]
