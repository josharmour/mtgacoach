"""D4 deck-suggestion tests, built from real captured cases.

Plan section 2 requires every behaviour change to be tested against a real
logged case, named in the test:

- STANDARD_METAGAME_HTML — live MTGGoldfish standard metagame page captured
  2026-11-12: two paper-metagame archetype tiles (Mono-Green Landfall,
  Dimir Midrange) plus the page's budget-decks-container tile (Puppet
  Crafting Cookies). Verifies that every listed archetype and its budget
  lists are offered as candidates.
- BRAWL_DECK_PAGE_HTML — live MTGGoldfish brawl archetype page for
  "Esika, God of the Tree" captured 2026-11-12; its initializeDeckComponents
  payload carries the full 100-line list. Verifies brawl deck lists parse.
- The empty-filter fallback reproduces the 2026-11-10 customer report where
  every suggested deck was 56-68 copies short and the builder showed an
  empty table.
"""

import json
import pathlib

import pytest

from arenamcp import mtggoldfish as mtggoldfish_module
from arenamcp.mtggoldfish import MTGGoldfishClient

STANDARD_METAGAME_HTML = """<html><head></head><body>
<h1>Standard Metagame</h1>
<div class='archetype-tile' id='28136'>
<div class='archetype-tile-image'>
<div aria-label='Image of Icetill Explorer' class='card-tile' role='img'>
<div class='card-image-tile' style='background-image: url(&#39;https://cards.mtggoldfish.com/images/019ce2f0-256d-7b09-b5a1-cb81f2347603/variants/672/938/card_image.webp&#39;);'></div>
<a class='card-image-tile-link-overlay' href='/archetype/standard-mono-green-landfall-woe'>
<span class='sr-only'>Icetill Explorer</span>
</a>
</div>

</div>
<div class='archetype-tile-description-wrapper'>
<div class='archetype-tile-description'>
<div class='archetype-tile-title'>
<span class='deck-price-online'>
<a href="/archetype/standard-mono-green-landfall-woe#online">Mono-Green Landfall</a>
</span>
<span class='deck-price-paper'>
<a href="/archetype/standard-mono-green-landfall-woe#paper">Mono-Green Landfall</a>
</span>
</div>
<div class='manacost-container'>
<span class='manacost' aria-label='colors: green'><i class='ms ms-g ms-cost ms-shadow'></i></span>
</div>
<ul>
<li>Icetill Explorer</li>
<li>Mossborn Hydra</li>
<li>Earthbender Ascension</li>
</ul>
</div>
<div class='archetype-tile-statistics'>
<div class='archetype-tile-statistics-left'>
<div class='archetype-tile-statistic metagame-percentage'>
<div class='archetype-tile-statistic-name'>
META%
</div>
<div class='archetype-tile-statistic-value'>
12.0%
<span class='archetype-tile-statistic-value-extra-data'>
(441)
</span>
</div>
</div>
</div>
<div class='archetype-tile-statistics-right'>
<div class='archetype-tile-statistic deck-price-paper'>
<div class='archetype-tile-statistic-name'>
Tabletop
</div>
<div class='archetype-tile-statistic-value'>
$ 247
</div>
</div>
<div class='archetype-tile-statistic deck-price-online'>
<div class='archetype-tile-statistic-name'>
MTGO
</div>
<div class='archetype-tile-statistic-value'>
31 tix
</div>
</div>
</div>
</div>
</div>
</div>
<div class='archetype-tile' id='28086'>
<div class='archetype-tile-image'>
<div aria-label='Image of Kaito, Bane of Nightmares' class='card-tile' role='img'>
<div class='card-image-tile' style='background-image: url(&#39;https://cards.mtggoldfish.com/images/019ce2f0-1085-7a67-8bb6-acec97b3caa1/variants/672/938/card_image.webp&#39;);'></div>
<a class='card-image-tile-link-overlay' href='/archetype/standard-dimir-midrange-woe'>
<span class='sr-only'>Kaito, Bane of Nightmares</span>
</a>
</div>

</div>
<div class='archetype-tile-description-wrapper'>
<div class='archetype-tile-description'>
<div class='archetype-tile-title'>
<span class='deck-price-online'>
<a href="/archetype/standard-dimir-midrange-woe#online">Dimir Midrange</a>
</span>
<span class='deck-price-paper'>
<a href="/archetype/standard-dimir-midrange-woe#paper">Dimir Midrange</a>
</span>
</div>
<div class='manacost-container'>
<span class='manacost' aria-label='colors: blue black'><i class='ms ms-u ms-cost ms-shadow'></i><i class='ms ms-b ms-cost ms-shadow'></i></span>
</div>
<ul>
<li>Kaito, Bane of Nightmares</li>
<li>Enduring Curiosity</li>
<li>Floodpits Drowner</li>
</ul>
</div>
<div class='archetype-tile-statistics'>
<div class='archetype-tile-statistics-left'>
<div class='archetype-tile-statistic metagame-percentage'>
<div class='archetype-tile-statistic-name'>
META%
</div>
<div class='archetype-tile-statistic-value'>
10.2%
<span class='archetype-tile-statistic-value-extra-data'>
(375)
</span>
</div>
</div>
</div>
<div class='archetype-tile-statistics-right'>
<div class='archetype-tile-statistic deck-price-paper'>
<div class='archetype-tile-statistic-name'>
Tabletop
</div>
<div class='archetype-tile-statistic-value'>
$ 313
</div>
</div>
<div class='archetype-tile-statistic deck-price-online'>
<div class='archetype-tile-statistic-name'>
MTGO
</div>
<div class='archetype-tile-statistic-value'>
103 tix
</div>
</div>
</div>
</div>
</div>
</div>
<h1>Budget Decks</h1>
<div class='archetype-tile-container budget-decks-container'>
<div class='archetype-tile' id=''>
<div class='archetype-tile-image'>
<div aria-label='Image of Into the Flood Maw' class='card-tile' role='img'>
<div class='card-image-tile' style='background-image: url(&#39;https://cards.mtggoldfish.com/images/019ce2f0-0c40-7ed9-afdc-e69ecc12f14d/variants/672/938/card_image.webp&#39;);'></div>
<a class='card-image-tile-link-overlay' href='/deck/7967844'>
<span class='sr-only'>Into the Flood Maw</span>
</a>
</div>

</div>
<div class='archetype-tile-description-wrapper'>
<div class='archetype-tile-description'>
<div class='archetype-tile-title'>
<span class='deck-price-online'>
<a href="/deck/7967844#online">Puppet Crafting Cookies</a>
</span>
<span class='deck-price-paper'>
<a href="/deck/7967844#paper">Puppet Crafting Cookies</a>
</span>
</div>
<div class='deck-tile-author'>
by SaffronOlive
</div>
<div class='manacost-container'>
<span class='manacost' aria-label='colors: blue green'><i class='ms ms-u ms-cost ms-shadow'></i><i class='ms ms-g ms-cost ms-shadow'></i></span>
</div>
<ul>
<li>Into the Flood Maw</li>
<li>Puppet Crafting</li>
<li>Gingerbrute</li>
</ul>
</div>
<div class='archetype-tile-statistics'>
<div class='archetype-tile-statistics-left'>
</div>
<div class='archetype-tile-statistics-right'>
<div class='archetype-tile-statistic deck-price-paper'>
<div class='archetype-tile-statistic-name'>
Tabletop
</div>
<div class='archetype-tile-statistic-value'>
$ 49
</div>
</div>
<div class='archetype-tile-statistic deck-price-online'>
<div class='archetype-tile-statistic-name'>
MTGO
</div>
<div class='archetype-tile-statistic-value'>
22 tix
</div>
</div>
</div>
</div>
</body></html>"""

BRAWL_DECK_PAGE_HTML = """<html><head></head><body>
<script>
initializeDeckComponents('0ef5b3e9-b940-4278-a97a-db6acf3cd38f', '8020076', "1%20Wrath%20of%20God%0A1%20Plains%0A1%20Counterspell%0A1%20Lightning%20Bolt%0A1%20Rampant%20Growth%0A1%20Dragonskull%20Summit%0A1%20Damnation%0A1%20Wooded%20Foothills%0A1%20Cultivate%0A1%20Emrakul%2C%20the%20Aeons%20Torn%0A1%20Reflecting%20Pool%0A1%20Cruel%20Ultimatum%0A1%20Command%20Tower%0A1%20Snow-Covered%20Island%0A1%20Snow-Covered%20Forest%0A1%20Snow-Covered%20Mountain%0A1%20Snow-Covered%20Swamp%0A1%20Snow-Covered%20Plains%0A1%20Terrain%20Generator%0A1%20Breeding%20Pool%0A1%20Stomping%20Ground%0A1%20Godless%20Shrine%0A1%20Rootbound%20Crag%0A1%20Opt%0A1%20Mirari%27s%20Wake%0A1%20Dust%20Bowl%0A1%20Flooded%20Strand%0A1%20Polluted%20Delta%0A1%20Sheoldred%2C%20Whispering%20One%0A1%20Overgrown%20Tomb%0A1%20Temple%20Garden%0A1%20Exploration%0A1%20Misty%20Rainforest%0A1%20Marsh%20Flats%0A1%20Woodland%20Cemetery%0A1%20Hinterland%20Harbor%0A1%20Supreme%20Verdict%0A1%20Chromatic%20Lantern%0A1%20Urban%20Evolution%0A1%20Bring%20to%20Light%0A1%20Hour%20of%20Devastation%0A1%20Zacama%2C%20Primal%20Calamity%0A1%20Cleansing%20Nova%0A1%20Growth%20Spiral%0A1%20The%20Elderspell%0A1%20Golos%2C%20Tireless%20Pilgrim%0A1%20Arcane%20Signet%0A1%20Kroxa%2C%20Titan%20of%20Death%27s%20Hunger%0A1%20Uro%2C%20Titan%20of%20Nature%27s%20Wrath%0A1%20Migration%20Path%0A1%20Emergent%20Ultimatum%0A1%20Ruinous%20Ultimatum%0A1%20Indatha%20Triome%0A1%20Ketria%20Triome%0A1%20Raugrin%20Triome%0A1%20Savai%20Triome%0A1%20Zagoth%20Triome%0A1%20Chromatic%20Orrery%0A1%20Thriving%20Grove%0A1%20Hagra%20Mauling%0A1%20Binding%20the%20Old%20Gods%0A1%20Blightstep%20Pathway%0A1%20The%20World%20Tree%0A1%20Fracture%0A1%20Lier%2C%20Disciple%20of%20the%20Drowned%0A1%20Deserted%20Beach%0A1%20Overgrown%20Farmland%0A1%20Shipwreck%20Marsh%0A1%20Stormcarved%20Coast%0A1%20Farewell%0A1%20Jetmir%27s%20Garden%0A1%20Spara%27s%20Headquarters%0A1%20Xander%27s%20Lounge%0A1%20Kami%20of%20Bamboo%20Groves%0A1%20Choice%20of%20Fortunes%0A1%20Leyline%20Binding%0A1%20Drag%20to%20the%20Bottom%0A1%20One%20with%20the%20Multiverse%0A1%20Brotherhood%27s%20End%0A1%20Atraxa%2C%20Grand%20Unifier%0A1%20Sunfall%0A1%20Etali%2C%20Primal%20Conqueror%0A1%20Invasion%20of%20Zendikar%0A1%20Spelunking%0A1%20Make%20Your%20Own%20Luck%0A1%20Ancient%20Cornucopia%0A1%20Omenpath%20Journey%0A1%20Planar%20Genesis%0A1%20Roaring%20Furnace%20%2F%2F%20Steaming%20Sauna%0A1%20Loot%2C%20Exuberant%20Explorer%0A1%20Blitzball%0A1%20Hexing%20Squelcher%0A1%20Final%20Act%0A1%20Preponderant%20Pearl%0A1%20Through%20the%20Forest%20Gate%0A1%20Jennifer%20Walters%0A1%20Simulacrum%20Shaper%0A1%20Aerid%20Konstrari%0A1%20Tenured%20Tethermage%0A1%20Esika%2C%20God%20of%20the%20Tree%0A");
</script>
</body></html>"""


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        return None


@pytest.fixture
def goldfish_client(monkeypatch, tmp_path):
    """Hermetic client: cache redirected to tmp, network replaced per test."""
    monkeypatch.setattr(mtggoldfish_module, "CACHE_DIR", tmp_path / "cache")
    return MTGGoldfishClient()


def test_standard_metagame_lists_every_archetype_and_budget_lists(goldfish_client, monkeypatch):
    """From the live standard metagame page captured 2026-11-12: the builder
    must offer every archetype the page lists — including the budget deck
    section — not only the first twelve paper-metagame tiles."""
    monkeypatch.setattr(
        mtggoldfish_module.requests,
        "get",
        lambda *a, **k: _FakeResponse(STANDARD_METAGAME_HTML.encode("utf-8")),
    )
    decks = goldfish_client.get_metagame("standard", force_refresh=True)

    by_name = {d["name"]: d for d in decks}
    assert set(by_name) == {"Mono-Green Landfall", "Dimir Midrange", "Puppet Crafting Cookies"}

    landfall = by_name["Mono-Green Landfall"]
    assert landfall["meta_share"] == "12.0%"
    assert landfall["url"].endswith("/archetype/standard-mono-green-landfall-woe#paper")
    assert landfall["budget"] is False

    dimir = by_name["Dimir Midrange"]
    assert dimir["meta_share"] == "10.2%"
    assert dimir["budget"] is False

    # Budget tiles carry a tabletop price where metagame tiles carry META%;
    # the share must stay empty rather than mislabel a price as a share.
    cookies = by_name["Puppet Crafting Cookies"]
    assert cookies["budget"] is True
    assert cookies["meta_share"] == ""
    assert cookies["url"].endswith("/deck/7967844#paper")


def test_brawl_archetype_page_yields_full_deck_list(goldfish_client, monkeypatch, tmp_path):
    """From the live brawl archetype page for Esika, God of the Tree captured
    2026-11-12 (tests/fixtures/mtggoldfish/archetype-brawl-esika-god-of-the-tree.html):
    the full 100-card list must parse so brawl candidates reach the suggestion
    table, with the commander designated — not buried as the hundredth deck
    card (review defect 3, logged 2026-11-12)."""
    page = (
        pathlib.Path(__file__).parent
        / "fixtures"
        / "mtggoldfish"
        / "archetype-brawl-esika-god-of-the-tree.html"
    ).read_bytes()
    monkeypatch.setattr(mtggoldfish_module.requests, "get", lambda *a, **k: _FakeResponse(page))
    deck = goldfish_client.get_deck_list("https://www.mtggoldfish.com/deck/8020076#paper", force_refresh=True)

    assert sum(int(line.split()[0]) for line in deck["mainboard"]) == 99
    assert deck["mainboard"][0] == "1 Wrath of God"
    assert deck["commander"] == ["1 Esika, God of the Tree"]
    assert deck["sideboard"] == []

    # The Arena export carries the commander in its own section, so the
    # imported brawl deck keeps its commander instead of dropping it.
    from arenamcp.deck_candidates import arena_export

    exported = arena_export(deck)
    assert "Commander\n1 Esika, God of the Tree" in exported.split("\n\n")
    assert "Deck\n1 Wrath of God" in exported


def test_load_candidates_spans_standard_and_brawl_cheapest_first(monkeypatch):
    """The suggestion pool must span both ranked formats (Standard and Brawl)
    with every archetype and budget list, ordered cheapest to craft first —
    from the same captured pages as the parser tests above."""
    import arenamcp.deck_candidates as dc
    import arenamcp.deck_candidates as pricing_module

    metagame = {
        "standard": [
            {
                "name": "Dimir Midrange",
                "meta_share": "10.2%",
                "url": "u/std-dimir",
                "colors": "UB",
                "budget": False,
            },
            {
                "name": "Puppet Crafting Cookies",
                "meta_share": "",
                "url": "u/std-cookies",
                "colors": "UG",
                "budget": True,
            },
        ],
        "brawl": [
            {
                "name": "Esika, God of the Tree",
                "meta_share": "",
                "url": "u/br-esika",
                "colors": "G",
                "budget": False,
            },
        ],
    }
    deck_lists = {
        # Real-shaped lists: arena_export refuses decks under sixty main-deck
        # cards, so fixtures use complete constructed lists.
        "u/std-dimir": {
            "mainboard": ["4 Fatal Push", "4 Drown in the Loch", "52 Island"],
            "sideboard": [],
        },
        "u/std-cookies": {
            "mainboard": ["3 Gingerbrute", "57 Forest"],
            "sideboard": [],
        },
        "u/br-esika": {
            "commander": ["1 Esika, God of the Tree"],
            "mainboard": ["1 Wrath of God", "98 Plains"],
            "sideboard": [],
        },
    }

    class FakeClient:
        def get_metagame(self, format_name, force_refresh=False):
            return metagame[format_name]

        def get_deck_list(self, url, force_refresh=False):
            return dict(deck_lists[url])

    # Real-world rarities so the cheapest-first order reflects craft cost.
    RARITY_BY_NAME = {
        "Fatal Push": "uncommon",
        "Drown in the Loch": "rare",
        "Gingerbrute": "common",
        "Wrath of God": "rare",
        "Esika, God of the Tree": "mythic",
    }

    class FakeCard:
        def __init__(self, name):
            self.name = name
            self.rarity = RARITY_BY_NAME.get(name, "rare")
            self.arena_id = 70000

    class FakeDatabase:
        def get_card_by_arena_id(self, arena_id):
            return None

        def get_card_by_name(self, name):
            return FakeCard(name)

    monkeypatch.setattr(mtggoldfish_module.MTGGoldfishClient, "get_metagame", FakeClient.get_metagame)
    monkeypatch.setattr(mtggoldfish_module.MTGGoldfishClient, "get_deck_list", FakeClient.get_deck_list)
    monkeypatch.setattr(pricing_module, "pricing_database", lambda: FakeDatabase())

    collection = {70001: 0}
    wildcards = {"common": 0, "uncommon": 0, "rare": 0, "mythic": 0}
    candidates = dc.load_candidates(collection, wildcards)

    # Cheapest first across both formats; unresolvable cards sort last.
    # Spends: Esika = 1 rare + 1 mythic, Cookies = 3 commons, Dimir =
    # 4 uncommons + 4 rares.
    order = [(c["name"], sum(c["cost"]["spend"].values())) for c in candidates]
    assert order == [
        ("Esika, God of the Tree", 2),
        ("Puppet Crafting Cookies", 3),
        ("Dimir Midrange", 8),
    ]
    formats = {c["name"]: c["format"] for c in candidates}
    assert formats["Esika, God of the Tree"] == "Brawl"
    assert formats["Dimir Midrange"] == "Standard"
    sources = {c["name"]: c["source"] for c in candidates}
    assert sources["Puppet Crafting Cookies"] == "MTGGoldfish Standard (budget list)"
    assert sources["Dimir Midrange"] == "MTGGoldfish Standard (paper metagame)"


def test_rank_candidates_shows_only_measured_arena_rates():
    """Per-deck results follow P1's recording semantics: a win rate is only
    published once a deck has at least twenty decided games. A candidate may
    only carry a rate the fleet actually measured — never an invented one —
    and measured decks rank ahead of unmeasured ones."""
    from arenamcp.deck_candidates import rank_candidates

    candidates = [
        {
            "name": "Azorius Control",
            "format": "Standard",
            "cost": {"spend": {"rare": 5}, "unknown": []},
            "source_rank": 1,
        },
        {
            "name": "Mono-Blue Flash",
            "format": "Standard",
            "cost": {"spend": {"rare": 2}, "unknown": []},
            "source_rank": 2,
        },
        {
            "name": "Rakdos Fling",
            "format": "Standard",
            "cost": {"spend": {"rare": 1}, "unknown": ["X"]},
            "source_rank": 3,
        },
    ]
    results = [
        # Recorded under its Arena name; folds to match the hyphenated source name.
        {
            "name": "Mono Blue Flash",
            "games": 34,
            "wins": 21,
            "losses": 13,
            "win_rate": 0.6176,
            "formats": ["Standard Ranked"],
        },
        # Below the twenty-game floor: no rate may be shown for it.
        {
            "name": "Rakdos Fling",
            "games": 5,
            "wins": 5,
            "losses": 0,
            "win_rate": 1.0,
            "formats": ["Standard Ranked"],
        },
        # No win_rate at all (history without decided games).
        {
            "name": "Azorius Control",
            "games": 40,
            "wins": None,
            "losses": None,
            "win_rate": None,
            "formats": ["Standard Ranked"],
        },
    ]

    ranked = rank_candidates(candidates, results)
    assert [c["name"] for c in ranked] == ["Mono-Blue Flash", "Azorius Control", "Rakdos Fling"]

    flash = ranked[0]
    assert flash["arena"] == {"games": 34, "win_rate": 0.6176}
    # Sub-floor and rate-less decks stay in cheapest-first order with no arena data.
    assert all("arena" not in c for c in ranked[1:])
    assert ranked[2]["cost"]["spend"] == {"rare": 1}

    # Without any recorded results the cheapest-first order is untouched.
    plain = rank_candidates(candidates, None)
    assert [c["name"] for c in plain] == ["Azorius Control", "Mono-Blue Flash", "Rakdos Fling"]
    assert all("arena" not in c for c in plain)


def test_empty_filter_falls_back_to_nearest_first(qapp):
    """2026-11-10 customer report: with roughly forty wildcards every
    suggested deck was 56-68 copies short, so both filters came back empty
    and the builder showed an empty table. The builder must instead show all
    candidates nearest first (fewest wildcards to craft) and say so."""
    pytest.importorskip("PySide6")
    from arenamcp.desktop.deck_builder import DeckBuilderDialog

    def deck(name, spend_rare, unknown=()):
        return {
            "name": name,
            "source_rank": 1,
            "meta_share": "",
            "budget": True,
            "format": "Standard",
            "source": f"MTGGoldfish Standard ({'budget list' if not unknown else 'paper metagame'})",
            "checked_at": "2026-11-12T00:00:00+00:00",
            "url": f"https://www.mtggoldfish.com/deck/{name.replace(' ', '-')}",
            "export": f"Deck\n60 Cards\n{name}",
            "cost": {
                "owned": False,
                "affordable": False,
                "missing": {name: spend_rare},
                "spend": {} if unknown else {"rare": spend_rare},
                "unknown": list(unknown),
            },
        }

    dialog = DeckBuilderDialog(None)
    try:
        dialog._candidates = [
            deck("Expensive Wall", 5),
            deck("Cheap Starter", 2),
            deck("Odd Rarity", 1, unknown=("Mystery Card",)),
            {
                **deck("Proven Pilot", 3),
                # Measured by this fleet's autopilot over thirty-four games.
                **{"arena": {"games": 34, "win_rate": 0.6176}, "arena_sample": "fleet"},
                **{"meta_share": ""},
            },
        ]
        dialog.mode.setCurrentIndex(0)  # Owns every card: none qualify
        dialog._render()

        assert dialog.table.columnCount() == 6
        assert dialog.table.horizontalHeaderItem(4).text() == "Arena autopilot"
        assert "2026-11-12" in dialog.table.item(0, 5).text()
        assert "MTGGoldfish Standard" in dialog.table.item(0, 5).text()

        # Nothing owns every card, so all four candidates show nearest first.
        names = [dialog.table.item(r, 0).text() for r in range(dialog.table.rowCount())]
        assert names == [
            "1. Cheap Starter",
            "1. Proven Pilot",
            "1. Expensive Wall",
            # Unresolvable cards sort last even when cheaper on paper.
            "1. Odd Rarity",
        ]
        assert dialog.table.rowCount() == len(dialog._candidates)
        assert (
            f"showing all {len(dialog._candidates)} candidates" in dialog.status.text()
            and "nearest first" in dialog.status.text()
        )

        # Measured rates render exactly as measured; unmeasured decks show no rate.
        arena_cells = {
            dialog.table.item(r, 0).text(): dialog.table.item(r, 4).text()
            for r in range(dialog.table.rowCount())
        }
        assert arena_cells["1. Proven Pilot"] == "34 matches · 62% win rate"
        assert arena_cells["1. Cheap Starter"] == "\u2014"

        dialog.table.selectRow(names.index("1. Proven Pilot"))
        details = dialog.details.text()
        assert "[Standard]" in details
        assert "Shared fleet: 34 autopilot matches with this archetype, 62% win rate." in details
        dialog.table.selectRow(names.index("1. Cheap Starter"))
        assert autopilot_absent(dialog.details.text())
    finally:
        dialog.close()


def autopilot_absent(details: str) -> bool:
    return "[Standard]" in details and "with this archetype" not in details


# ---------------------------------------------------------------------------
# Review-defect regressions (Dotty, 2026-11-12, on c97ba1c)
# ---------------------------------------------------------------------------


def test_standard_rate_never_attaches_to_brawl_archetype_of_same_name():
    """Review defect 1 (logged 2026-11-12): the old substring matcher attached
    the Standard bucket "Mono Red Aggro, 50 games, 80%" to the Brawl "Mono Red"
    archetype. A rate attaches only when the recorded queue names identify
    exactly the candidate's format and exactly one measured bucket relates by
    name; anything ambiguous gets no rate."""
    from arenamcp.deck_candidates import rank_candidates

    standard = {
        "name": "Mono-Red Aggro",
        "format": "Standard",
        "cost": {"spend": {"rare": 3}, "unknown": []},
        "source_rank": 1,
    }
    brawl = {
        "name": "Mono Red Aggro",
        "format": "Brawl",
        "cost": {"spend": {"mythic": 1}, "unknown": []},
        "source_rank": 2,
    }

    # The logged case: a Standard-only bucket must dress the Standard
    # archetype and never the Brawl one.
    standard_bucket = {
        "name": "Mono Red Aggro",
        "games": 50,
        "wins": 40,
        "losses": 10,
        "win_rate": 0.8,
        "formats": ["Standard Ranked"],
    }
    ranked = rank_candidates([dict(standard), dict(brawl)], [standard_bucket])
    by_name = {c["name"]: c for c in ranked}
    assert by_name["Mono-Red Aggro"]["arena"] == {"games": 50, "win_rate": 0.8}
    assert "arena" not in by_name["Mono Red Aggro"]

    # Mirror image: a Brawl-ranked bucket belongs to the Brawl archetype only.
    brawl_bucket = dict(standard_bucket, formats=["Brawl Ranked"])
    ranked = rank_candidates([dict(standard), dict(brawl)], [brawl_bucket])
    by_name = {c["name"]: c for c in ranked}
    assert by_name["Mono Red Aggro"]["arena"] == {"games": 50, "win_rate": 0.8}
    assert "arena" not in by_name["Mono-Red Aggro"]

    # Ambiguity: two same-format buckets both relating by name (hyphen
    # variants of the same archetype) — no rate rather than a guessed one.
    twin_buckets = [
        dict(standard_bucket),
        dict(standard_bucket, name="Mono-Red Aggro", games=22, win_rate=0.45),
    ]
    ranked = rank_candidates([dict(standard)], twin_buckets)
    assert "arena" not in ranked[0]

    # A bucket whose queue names span more than one format is ambiguous too.
    mixed_queue = dict(standard_bucket, formats=["Standard Ranked", "Brawl Ranked"])
    ranked = rank_candidates([dict(standard)], [mixed_queue])
    assert "arena" not in ranked[0]


def test_local_results_integrate_with_the_per_deck_aggregator(tmp_path, monkeypatch):
    """Review defect 2 (logged 2026-11-12): deck suggestions must consume the
    per-deck results work (P1) through the real aggregator over really stored
    history — not scrape a local MatchHistory with a silent empty fallback."""
    import arenamcp.match_history as mh_module
    from arenamcp.deck_candidates import local_autopilot_results, rank_candidates
    from arenamcp.desktop.performance_tab import autopilot_deck_results
    from arenamcp.match_history import MatchHistory, MatchRecord

    records = [
        MatchRecord(
            match_id=f"m{i}",
            result="win" if i % 3 else "loss",
            deck_identity="id-dimir",
            deck_name="Dimir Midrange",
            format_name="Standard Ranked",
            autopilot=True,
        )
        for i in range(30)
    ]
    history = MatchHistory(tmp_path / "history.json")
    for rec in records:
        history.add_record(rec)

    # The suggestions pipeline reads the same stored history the Match
    # History tab reads and aggregates it with P1's own aggregator.
    monkeypatch.setattr(mh_module, "MatchHistory", lambda: MatchHistory(tmp_path / "history.json"))
    local = local_autopilot_results()
    assert local == autopilot_deck_results(list(reversed(records)))
    entry = next(e for e in local if e["name"] == "Dimir Midrange")
    assert entry["games"] == 30
    assert entry["win_rate"] is not None
    assert entry["formats"] == ["Standard Ranked"]

    # The aggregated rate flows onto the matching Standard candidate — and
    # onto nothing else.
    candidates = [
        {
            "name": "Dimir Midrange",
            "format": "Standard",
            "cost": {"spend": {"rare": 4}, "unknown": []},
            "source_rank": 1,
        },
        {
            "name": "Dimir Midrange",
            "format": "Brawl",
            "cost": {"spend": {"rare": 4}, "unknown": []},
            "source_rank": 2,
        },
    ]
    ranked = rank_candidates(candidates, local)
    assert ranked[0]["arena"]["games"] == 30
    assert "arena" not in ranked[1]

    # Failures are loud: a broken history store raises instead of silently
    # suggesting that the fleet has no data.
    def broken_history():
        raise RuntimeError("history store unreadable")

    monkeypatch.setattr(mh_module, "MatchHistory", broken_history)
    try:
        local_autopilot_results()
    except RuntimeError:
        pass
    else:
        raise AssertionError("broken history must raise, not return no data")


def _fleet_bundle(match_id, *, format_name, archetype_line, result, autopilot=True):
    """Minimal shared-corpus bundle shaped like the real ones (see
    tests/test_fleet_review.py, whose fixtures mirror bundle 00651f77)."""
    return {
        "match_id": match_id,
        "created_utc": "2026-11-10T00:00:00Z",
        "settings_subset": {"autopilot_enabled": autopilot},
        "result": result,
        "final_state": {
            "format_name": format_name,
            "opponent_seat_id": 1,
            "local_seat_id": 0,
            "turn_info": {"turn_number": 8},
            "players": [
                {"seat_id": 0, "life_total": 25},
                {"seat_id": 1, "life_total": 3},
            ],
            "zones": {"command": []},
        },
        "coach_log": {"lines": []},
        "packet": {"deck_strategy": archetype_line, "decisions": []},
    }


def test_fleet_results_come_from_the_shared_match_bundle_corpus(tmp_path):
    """Review defect 3 (logged 2026-11-12): the fleet path must mine the ACTUAL
    fleet source — W1's tools/fleet_review.py over the shared match-bundle
    corpus — not this machine's local MatchHistory. Rates stay per format and
    per archetype, folded per match exactly as W1 folds them, and only reach a
    percentage past the twenty-game floor (real corpus case 2026-11-12: the
    Brawl Laelia deck and the Standard prowess-burn deck are separate rows)."""
    import gzip

    from arenamcp.deck_candidates import fleet_autopilot_results

    bundles = tmp_path / "match_bundles"
    bundles.mkdir()

    def write(match_id, **kwargs):
        payload = gzip.compress(json.dumps(_fleet_bundle(match_id, **kwargs)).encode())
        (bundles / f"{match_id}.json.gz").write_bytes(payload)

    laelia = (
        "DECK PLAYBOOK v2: Mono-red Laelia cascade/discover aggro-tempo: "
        "cast Laelia early, cascade into burn."
    )
    prowess = (
        "DECK PLAYBOOK v2: Mono-red prowess burn: stack spells on the prowess creature and finish with burn."
    )
    # Same archetype line in two formats -> two separate rows (never pooled).
    for i in range(23):  # Brawl Laelia: past the floor
        write(
            f"brawl-laelia-{i}",
            format_name="Brawl Ladder",
            archetype_line=laelia,
            result="win" if i % 3 else "loss",
        )
    for i in range(19):  # Standard Laelia: below the floor -> no percentage
        write(
            f"std-laelia-{i}", format_name="Ladder", archetype_line=laelia, result="win" if i % 2 else "loss"
        )
    for i in range(36):  # Standard prowess-burn: past the floor
        write(
            f"std-prowess-{i}",
            format_name="Ladder",
            archetype_line=prowess,
            result="win" if i % 3 else "loss",
        )
    # Advisor-mode games never count toward the fleet sample.
    write("advisor-brawl", format_name="Brawl Ladder", archetype_line=laelia, result="win", autopilot=False)

    fleet = fleet_autopilot_results(bundles_dir=str(bundles))
    by_key = {(e["formats"][0] if e["formats"] else "", e["name"]): e for e in fleet}

    brawl_laelia = by_key[("Brawl Ladder", "Mono-red Laelia cascade/discover aggro-tempo")]
    assert brawl_laelia["games"] == 23
    assert brawl_laelia["wins"] == sum(1 for i in range(23) if i % 3)
    assert brawl_laelia["win_rate"] == pytest.approx(brawl_laelia["wins"] / 23)

    std_laelia = by_key[("Ladder", "Mono-red Laelia cascade/discover aggro-tempo")]
    assert std_laelia["games"] == 19
    assert std_laelia["win_rate"] is None

    std_prowess = by_key[("Ladder", "Mono-red prowess burn")]
    assert std_prowess["games"] == 36
    assert std_prowess["win_rate"] == pytest.approx(std_prowess["wins"] / 36)

    # Failures are loud: a missing corpus directory raises instead of
    # silently suggesting that the fleet has no data.
    try:
        fleet_autopilot_results(bundles_dir=str(tmp_path / "missing"))
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("missing fleet corpus must raise, not return no data")


def test_unreachable_source_pages_are_skipped_with_a_logged_warning(goldfish_client, monkeypatch, caplog):
    """One blocked source page (the /deck/ budget pages answer HTTP 403 to
    this host, logged 2026-11-12) must skip that source with a warning — not
    abort the whole suggestion table."""
    import logging

    import arenamcp.deck_candidates as dc

    metagame = [
        {
            "name": "Dimir Midrange",
            "meta_share": "",
            "url": "https://x/archetype/standard-dimir-midrange-woe#paper",
            "colors": "UB",
            "budget": False,
        },
        {
            "name": "Budget Cookies",
            "meta_share": "",
            "url": "https://x/deck/7967844#paper",
            "colors": "UG",
            "budget": True,
        },
    ]

    class FakeClient:
        def get_metagame(self, format_name, force_refresh=False):
            return metagame

        def get_deck_list(self, url, force_refresh=False):
            if "/deck/" in url:
                raise OSError("HTTP 403 for budget deck page")
            return {
                "mainboard": ["4 Fatal Push", "4 Drown in the Loch", "52 Island"],
                "sideboard": [],
            }

    class FakeCard:
        def __init__(self, name):
            self.name = name
            self.rarity = {"Fatal Push": "uncommon", "Drown in the Loch": "rare"}.get(name, "basic")
            self.arena_id = 70000

    class FakeDatabase:
        def get_card_by_arena_id(self, arena_id):
            return None

        def get_card_by_name(self, name):
            return FakeCard(name)

    import arenamcp.deck_candidates as pricing_module

    monkeypatch.setattr(mtggoldfish_module.MTGGoldfishClient, "__new__", lambda cls: FakeClient())
    monkeypatch.setattr(pricing_module, "pricing_database", lambda: FakeDatabase())

    with caplog.at_level(logging.WARNING, logger="arenamcp.deck_candidates"):
        candidates = dc.load_candidates({70001: 0}, {"common": 0, "uncommon": 0, "rare": 0, "mythic": 0})

    # Only the Standard source has an admissible list for its format.
    assert [(c["format"], c["name"]) for c in candidates] == [
        ("Standard", "Dimir Midrange"),
    ]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Budget Cookies" in r.getMessage() for r in warnings)


def test_captured_source_pages_and_synthetic_inventory_produce_ten_priced_candidates(
    goldfish_client, monkeypatch
):
    """Captured November 12 source pages, replayed against a synthetic collection."""
    import json
    from datetime import datetime

    import arenamcp.deck_candidates as dc
    import arenamcp.deck_candidates as pricing_module

    class CaptureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 12, tzinfo=tz)

    monkeypatch.setattr(mtggoldfish_module, "datetime", CaptureClock)
    fixtures = pathlib.Path(__file__).parent / "fixtures" / "mtggoldfish"
    pages = {p.stem: p.read_bytes() for p in fixtures.glob("*.html")}
    rarities = json.loads((fixtures / "rarities.json").read_text())

    def fake_get(url, *args, **kwargs):
        if url.endswith("/metagame/standard#paper"):
            return _FakeResponse(pages["meta_standard"])
        if url.endswith("/metagame/brawl#paper"):
            return _FakeResponse(pages["meta_brawl"])
        slug = url.split("/archetype/", 1)[1].split("#", 1)[0].rstrip("/")
        if slug.endswith("-woe"):
            slug = slug[:-4]
        return _FakeResponse(pages[f"archetype-{slug}"])

    monkeypatch.setattr(mtggoldfish_module.requests, "get", fake_get)

    next_arena_id = 70001

    class FixtureCard:
        def __init__(self, name):
            nonlocal next_arena_id
            self.name = name
            self.rarity = rarities[name]
            # Unique sequential ids: a real database never hands two cards
            # the same Arena id (a hash-derived id collided across cards and
            # silently zeroed part of the collection).
            self.arena_id = next_arena_id
            next_arena_id += 1

    cards = {}

    def card_for(name):
        if name not in cards:
            cards[name] = FixtureCard(name)
        return cards[name]

    class FixtureDatabase:
        """Faithful stand-in for the unified card database: every Arena id in
        the collection resolves back to its card, exactly as the real
        database does for a live account (review defect 4, logged 2026-11-12:
        a resolver returning None silently discarded the whole collection and
        made every candidate unaffordable)."""

        def get_card_by_arena_id(self, arena_id):
            for card in cards.values():
                if card.arena_id == int(arena_id):
                    return card
            return None

        def get_card_by_name(self, name):
            return card_for(name)

    monkeypatch.setattr(pricing_module, "pricing_database", lambda: FixtureDatabase())

    # Reconstruction of the reported account: a grinding Standard/Brawl
    # account owns every common and uncommon it plays (four copies), plus
    # half of the rares and mythics it has opened; the wildcard balance is
    # the reported ~40 (30 rare, 10 mythic). The exact inventory is not in
    # the evidence pack, so the reconstruction is deliberately conservative
    # and fully documented here.
    collection = {}
    seen = set()

    def collect_owned():
        # Walk every metagame entry — archetype lists AND budget lists —
        # because load_candidates prices budget decks too, and a card that
        # only appears in a budget list must still count as owned.
        client = mtggoldfish_module.MTGGoldfishClient()
        for fmt in ("standard", "brawl"):
            for entry in client.get_metagame(fmt, force_refresh=True):
                try:
                    deck = client.get_deck_list(entry["url"], force_refresh=True)
                except (ValueError, OSError, KeyError):
                    continue
                for line in deck["mainboard"] + deck.get("commander", []) + deck["sideboard"]:
                    qty, name = line.split(" ", 1)
                    if name in seen:
                        continue
                    seen.add(name)
                    rarity = rarities[name]
                    owned = 4 if rarity in ("common", "uncommon") else (4 if len(seen) % 2 == 0 else 0)
                    collection[card_for(name).arena_id] = owned

    collect_owned()
    wildcards = {"common": 0, "uncommon": 0, "rare": 30, "mythic": 10}

    candidates = dc.load_candidates(collection, wildcards)

    # At least ten parsed, priced candidates across both formats.
    assert len(candidates) >= 10
    formats_seen = {c["format"] for c in candidates}
    assert formats_seen == {"Standard", "Brawl"}

    # Every candidate is priced: a spend breakdown and its source/date ride
    # along, and the export round-trips through the Arena importer.
    from arenamcp.deck_candidates import parse_deck

    for cand in candidates:
        assert isinstance(cand["cost"]["spend"], dict)
        assert cand["source"].startswith("MTGGoldfish")
        assert cand["checked_at"].startswith("2026-11-12")
        rebuilt = parse_deck(cand["export"].splitlines())
        assert sum(rebuilt.values()) >= 60

    # Cheapest first: no candidate costs more than the one before it.
    def total_spend(cost):
        return sum(cost["spend"].values())

    spends = [total_spend(c["cost"]) for c in candidates]
    assert spends == sorted(spends)

    # The reported bug — an empty table under 'Within my wildcard balance' —
    # is gone: several candidates are craftable under the ~40-wildcard
    # balance by compare_collection's own all-rarity budget check (the old
    # test re-implemented a weaker rare/mythic-only filter instead of asking
    # the production code).
    affordable = [c for c in candidates if c["cost"]["affordable"]]
    assert len(affordable) >= 3
    for cand in affordable:
        for rarity in ("common", "uncommon", "rare", "mythic"):
            assert cand["cost"]["spend"].get(rarity, 0) <= wildcards[rarity]
        assert not cand["cost"]["unknown"]


def test_fleet_rates_count_only_decided_matches_toward_the_twenty_game_floor(tmp_path):
    """Review defect logged 2026-11-12 against 096b665: a fleet bucket with
    1 decided win and 19 matches whose outcome was never recorded published
    games=20 / losses=19 / win_rate=5% — crossing the twenty-game floor on
    undecided games alone. Matches the local aggregator's decided-games rule
    (performance_tab.autopilot_deck_results skips any record whose result is
    not win/loss): undecided matches are excluded from the denominator AND
    from losses, and only decided compatible-format games count."""
    import gzip

    from arenamcp.deck_candidates import fleet_autopilot_results

    bundles = tmp_path / "match_bundles"
    bundles.mkdir()

    def write(match_id, **kwargs):
        payload = gzip.compress(json.dumps(_fleet_bundle(match_id, **kwargs)).encode())
        (bundles / f"{match_id}.json.gz").write_bytes(payload)

    prowess = (
        "DECK PLAYBOOK v2: Mono-red prowess burn: stack spells on the prowess creature and finish with burn."
    )
    tempo = "DECK PLAYBOOK v2: Mono-blue tempo: counter the early threats and fly over the top."

    # The logged defect case, reproduced: 1 decided win plus 19 matches whose
    # outcome was never recorded (bundle result absent, no last_game_result).
    write("prowess-decided-win", format_name="Ladder", archetype_line=prowess, result="win")
    for i in range(19):
        write(f"prowess-undecided-{i}", format_name="Ladder", archetype_line=prowess, result=None)

    # Control bucket: twenty DECIDED games do cross the floor.
    control_wins = 0
    for i in range(20):
        win = i % 5 != 0  # 16 wins, 4 losses
        control_wins += win
        write(
            f"tempo-decided-{i}", format_name="Ladder", archetype_line=tempo, result="win" if win else "loss"
        )

    fleet = fleet_autopilot_results(bundles_dir=str(bundles))
    by_key = {(e["formats"][0] if e["formats"] else "", e["name"]): e for e in fleet}

    undecided_bucket = by_key[("Ladder", "Mono-red prowess burn")]
    # Undecided matches are invisible in the sample: 1 decided game, not 20.
    assert undecided_bucket["games"] == 1
    assert undecided_bucket["wins"] == 1
    # An unrecorded outcome is not a loss.
    assert undecided_bucket["losses"] == 0
    # One decided game is far below the floor: no percentage is published.
    assert undecided_bucket["win_rate"] is None

    control_bucket = by_key[("Ladder", "Mono-blue tempo")]
    assert control_bucket["games"] == 20
    assert control_bucket["wins"] == control_wins
    assert control_bucket["losses"] == 20 - control_wins
    assert control_bucket["win_rate"] == pytest.approx(control_wins / 20)


def test_missing_fleet_corpus_keeps_the_suggestion_table_populated(qapp, monkeypatch, caplog):
    """Review defect logged 2026-11-12 against 096b665: with the fleet corpus
    missing, _LoadDecks raised before load_candidates ran and emitted EMPTY
    decks even though viable candidates existed — violating the never-empty
    requirement. End-to-end regression through the worker: the fleet failure
    degrades to this machine's measured rates only, the warning is surfaced
    explicitly in the status message (and logged), and no fleet rate is
    invented for any candidate."""
    import logging

    import arenamcp.desktop.deck_builder as deck_builder_module

    candidates = [
        {
            "name": "Mono-Blue Flash",
            "format": "Standard",
            "cost": {"spend": {"rare": 2}, "unknown": []},
            "source_rank": 1,
        },
        {
            "name": "Rakdos Fling",
            "format": "Standard",
            "cost": {"spend": {"rare": 1}, "unknown": ["X"]},
            "source_rank": 2,
        },
    ]
    local_results = [
        {
            "name": "Mono Blue Flash",
            "games": 34,
            "wins": 21,
            "losses": 13,
            "win_rate": 0.6176,
            "formats": ["Standard Ranked"],
        }
    ]

    def fake_local():
        return [dict(entry) for entry in local_results]

    def missing_fleet(bundles_dir=None):
        raise FileNotFoundError("fleet match-bundle corpus not found: /home/x/.arenamcp/match_bundles")

    monkeypatch.setattr(deck_builder_module, "local_autopilot_results", fake_local)
    monkeypatch.setattr(deck_builder_module, "fleet_autopilot_results", missing_fleet)
    monkeypatch.setattr(deck_builder_module, "load_candidates", lambda collection, wildcards: candidates)

    captured = []
    worker = deck_builder_module._LoadDecks({}, {})
    worker.result.ready.connect(lambda decks, message: captured.append((decks, message)))
    with caplog.at_level(logging.WARNING, logger="arenamcp.desktop.deck_builder"):
        worker.run()

    assert len(captured) == 1
    decks, message = captured[0]
    # Never empty: both viable candidates are still offered.
    assert [c["name"] for c in decks] == ["Mono-Blue Flash", "Rakdos Fling"]
    # The degraded source is surfaced explicitly, naming what is unavailable.
    assert "Fleet results unavailable" in message
    assert "fleet match-bundle corpus not found" in message
    assert "showing this machine's rates only" in message
    assert any("fleet autopilot results unavailable" in record.message for record in caplog.records)
    # The local rate attaches; no fleet rate is invented for anything else.
    flash = next(c for c in decks if c["name"] == "Mono-Blue Flash")
    assert flash["arena"] == {"games": 34, "win_rate": 0.6176}
    fling = next(c for c in decks if c["name"] == "Rakdos Fling")
    assert "arena" not in fling


def test_load_decks_combines_local_and_fleet_rates_end_to_end(qapp, monkeypatch):
    """End-to-end wiring regression (review defect follow-up logged
    2026-11-12): when both sources are healthy, _LoadDecks ranks on the union
    of this machine's rates and the fleet's rates — measured decks lead,
    ordered by win rate then cheapest-first."""
    import arenamcp.desktop.deck_builder as deck_builder_module

    candidates = [
        {
            "name": "Mono-Blue Flash",
            "format": "Standard",
            "cost": {"spend": {"rare": 2}, "unknown": []},
            "source_rank": 1,
        },
        {
            "name": "Rakdos Fling",
            "format": "Standard",
            "cost": {"spend": {"rare": 1}, "unknown": ["X"]},
            "source_rank": 2,
        },
        {
            "name": "Azorius Control",
            "format": "Standard",
            "cost": {"spend": {"rare": 5}, "unknown": []},
            "source_rank": 3,
        },
    ]

    def fake_local():
        return [
            {
                "name": "Mono Blue Flash",
                "games": 34,
                "wins": 21,
                "losses": 13,
                "win_rate": 0.6176,
                "formats": ["Standard Ranked"],
            }
        ]

    def fake_fleet(bundles_dir=None):
        return [
            {
                "name": "Rakdos Fling",
                "games": 25,
                "wins": 20,
                "losses": 5,
                "win_rate": 0.8,
                "formats": ["Standard Ranked"],
            }
        ]

    monkeypatch.setattr(deck_builder_module, "local_autopilot_results", fake_local)
    monkeypatch.setattr(deck_builder_module, "fleet_autopilot_results", fake_fleet)
    monkeypatch.setattr(deck_builder_module, "load_candidates", lambda collection, wildcards: candidates)

    captured = []
    worker = deck_builder_module._LoadDecks({}, {})
    worker.result.ready.connect(lambda decks, message: captured.append((decks, message)))
    worker.run()

    decks, message = captured[0]
    assert message == ""
    assert [c["name"] for c in decks] == ["Rakdos Fling", "Mono-Blue Flash", "Azorius Control"]
    assert decks[0]["arena"] == {"games": 25, "win_rate": 0.8}
    assert decks[1]["arena"] == {"games": 34, "win_rate": 0.6176}
    assert "arena" not in decks[2]
