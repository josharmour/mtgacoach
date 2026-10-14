"""Set research (2026-10-13): engines, rarity-checked lists, low-sample bombs, online research.

The online comparison of the FRA primer found key_commons holding uncommons
and rares, bombs missing because 17lands withholds their win rate, pair win
rates without splash decks, and archetype engines nobody named. Online
research is opt-in (setting draft_research_online) and never required.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from arenamcp import set_research
from arenamcp.set_primer import (
    RESEARCH_HEADER,
    SetCard,
    SetPrimer,
    _card_rows,
    _clean_archetype,
    _name_index,
    archetype_prompt,
    format_prompt,
)


def tiny_primer() -> SetPrimer:
    cards = {
        1: SetCard(1, "Common Spell", "U", "C", "Instant", cmc=1, gih_wr=0.57, baseline=0.5, games=5000),
        2: SetCard(2, "Signpost", "UR", "U", "Creature", cmc=4, gih_wr=0.58, baseline=0.8, games=3000),
        3: SetCard(3, "Rare Bomb", "R", "M", "Creature", cmc=5, games=140, game_wr=0.61, alsa=1.6),
        4: SetCard(4, "Chaff", "R", "C", "Creature", cmc=3, gih_wr=0.50, baseline=-1.2, games=900),
    }
    return SetPrimer(
        set_code="TST",
        cards=cards,
        pair_stats={"UR": {"win_rate": 0.56, "games": 9000}},
        archetypes=[{"colors": "UR", "name": "Izzet", "tier": 2}],
    )


def test_key_lists_keep_only_their_rarity_and_avoid_drops_recommended_cards():
    primer = tiny_primer()
    entry = _clean_archetype(
        {
            "name": "Izzet Spells",
            "plan": "Spells.",
            "tier": 1,
            "engine": ["noncreature_spell", "made_up", "jace"],
            "key_commons": ["Common Spell", "Signpost", "Rare Bomb"],
            "key_uncommons": ["Signpost", "Rare Bomb"],
            "payoffs": ["Rare Bomb"],
            "avoid": ["Chaff", "Signpost"],
        },
        "UR",
        _name_index(primer),
        primer,
    )
    assert entry["key_commons"] == ["Common Spell"]
    assert entry["key_uncommons"] == ["Signpost"]
    assert entry["payoffs"] == ["Rare Bomb"]
    assert entry["avoid"] == ["Chaff"]  # Signpost is recommended, so never avoided
    assert entry["engine"] == ["noncreature_spell", "jace"]


def test_low_sample_cards_are_shown_as_such_not_as_missing():
    rows = _card_rows(list(tiny_primer().cards.values()))
    bomb = next(row for row in rows if row.startswith("Rare Bomb"))
    assert "low sample (140 games, game WR 61.0)" in bomb


def test_research_digest_is_appended_as_opinion():
    primer = tiny_primer()
    assert RESEARCH_HEADER not in archetype_prompt(primer, "UR")
    prompt = archetype_prompt(primer, "UR", "Izzet is the best deck; take Signpost early.")
    assert RESEARCH_HEADER in prompt and prompt.rstrip().endswith("take Signpost early.")
    assert RESEARCH_HEADER in format_prompt(primer, "Slow format.")


class FakeSession:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.urls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        for prefix, text in self.pages.items():
            if url.startswith(prefix):
                return SimpleNamespace(text=text, raise_for_status=lambda: None)
        raise OSError(f"no page for {url}")


def search_page(*urls: str) -> str:
    return "".join(
        f'<a class="result__a" href="//duckduckgo.com/l/?uddg={quote(url, safe="")}&rut=x">t</a>'
        for url in urls
    )


GUIDE = (
    "<html><head><title>Reality Fracture Draft Guide</title><script>evil()</script></head><body>"
    "<nav>menu menu menu menu menu menu menu menu</nav>"
    "<p>Reality Fracture is a slow format; the Izzet archetype wants cheap removal and spells.</p>"
    "<p>Signpost uncommons: Saheeli, Jewel of Avishkar is the Izzet payoff you should take early in draft.</p>"
    "<p>Short.</p></body></html>"
)


def test_online_research_reads_trusted_guides_and_caches_them(tmp_path):
    session = FakeSession(
        {
            "https://api.scryfall.com/sets/fra": '{"name": "Reality Fracture"}',
            "https://html.duckduckgo.com/": search_page(
                "https://draftsim.com/fra-guide/", "https://spam.example.com/fra", "https://mtgazone.com/fra/"
            ),
            "https://draftsim.com/fra-guide/": GUIDE + "<p>" + "Izzet archetype tempo spells. " * 10 + "</p>",
            "https://mtgazone.com/fra/": "<title>Some Other Set Review</title><p>"
            + "An unrelated archetype common pick. " * 20
            + "</p>",
        }
    )
    result = set_research.fetch_set_research("FRA", session=session, cache_dir=tmp_path)
    assert result["set_name"] == "Reality Fracture"
    assert [source["url"] for source in result["sources"]] == ["https://draftsim.com/fra-guide/"]
    assert "slow format" in result["digest"] and "evil()" not in result["digest"]
    assert not any("spam.example.com" in url for url in session.urls)
    # Cached: a second call makes no requests.
    session.urls.clear()
    assert set_research.fetch_set_research("FRA", session=session, cache_dir=tmp_path)["digest"]
    assert session.urls == []


def test_online_research_failure_is_an_empty_digest(tmp_path):
    result = set_research.fetch_set_research("FRA", session=FakeSession({}), cache_dir=tmp_path)
    assert result["digest"] == "" and result["sources"] == []


def test_primer_service_passes_research_to_the_synthesis(monkeypatch, tmp_path):
    from arenamcp import set_primer

    seen = {}

    def fake_synthesize(primer, backend, report=None, research=""):
        seen["research"] = research
        return primer

    monkeypatch.setattr(set_primer, "synthesize", fake_synthesize)
    monkeypatch.setattr(set_primer, "data_primer", lambda *args: rated_primer())
    service = set_primer.SetPrimerService(
        backend_fn=lambda: object(),
        ratings_fn=lambda key: [],
        color_stats_fn=lambda key: {},
        cache_dir=tmp_path,
        research_fn=lambda key: {"digest": "Izzet is good.", "sources": [{"title": "Guide", "url": "u"}]},
    )
    primer = service.build_now("TST")
    assert seen["research"] == "Izzet is good."
    assert primer.research == [{"title": "Guide", "url": "u"}]


def rated_primer() -> SetPrimer:
    cards = {
        i: SetCard(i, f"Card {i}", "U", "C", "Creature", cmc=2, gih_wr=0.55, baseline=0.0, games=1000)
        for i in range(1, 30)
    }
    return SetPrimer(set_code="TST", cards=cards)


@pytest.mark.parametrize("combine", [True, False])
def test_color_ratings_keep_combined_and_split_rows_apart(monkeypatch, tmp_path, combine):
    from arenamcp import draftstats

    urls = []

    def fake_get(url, timeout=None):
        urls.append(url)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: [{"color_name": "Izzet (UR)"}])

    monkeypatch.setattr(draftstats.requests, "get", fake_get)
    cache = draftstats.DraftStatsCache(cache_dir=tmp_path)
    rows = cache.get_color_rows("FRA", combine_splash=combine)
    assert rows == [{"color_name": "Izzet (UR)"}]
    assert ("combine_splash=true" in urls[0]) is combine
    assert cache._get_color_cache_path("FRA", combine).exists()


# ---------------------------------------------------------------------------
# Research before the draft (2026-10-13: it started at P1p1 and took 82 s, so
# P1p1-p3 were picked on 17lands data alone; joining had shown the set 2.5
# minutes earlier).
# ---------------------------------------------------------------------------


def test_an_old_research_file_is_served_while_it_rebuilds(monkeypatch, tmp_path):
    from arenamcp import set_primer

    old = rated_primer()
    old.version = set_primer.PRIMER_VERSION - 1
    old.built_at = 1.0
    (tmp_path / "TST.json").write_text(old.to_json(), encoding="utf-8")
    service = set_primer.SetPrimerService(
        backend_fn=lambda: None, ratings_fn=lambda key: [], color_stats_fn=lambda key: {}, cache_dir=tmp_path
    )
    started = []
    monkeypatch.setattr(service, "_build", lambda key: started.append(key))
    assert service.get("TST") is not None  # served, not dropped
    assert service.status("TST") == "stale"
    service.ensure("TST")
    for _ in range(100):
        if started:
            break
        time.sleep(0.01)
    assert started == ["TST"]


def test_research_starts_when_the_event_is_joined_and_is_announced_once():
    from arenamcp.standalone_draft_event import _DraftEventMixin

    class FakeService:
        def __init__(self):
            self.state = "missing"
            self.ensured = []

        def status(self, code):
            return self.state

        def ensure(self, code):
            self.ensured.append(code)
            self.state = "building"

        def get(self, code):
            return rated_primer() if self.state == "ready" else None

    logged = []
    coach = _DraftEventMixin()
    service = FakeService()
    coach._set_primer_service = lambda: service
    coach.ui = SimpleNamespace(log=logged.append)
    joined = {"is_active": True, "set_code": "tst", "cards": []}  # no pack yet
    coach._prefetch_draft_research(joined)
    coach._prefetch_draft_research(joined)
    assert service.ensured == ["TST"]
    assert len(logged) == 1 and "researching the set before the first pick" in logged[0]
    service.state = "ready"
    coach._prefetch_draft_research(joined)
    assert len(logged) == 2 and logged[1].startswith("[DRAFT RESEARCH] TST ready. Target decks:")


def test_research_summary_names_the_target_decks_and_their_engines():
    from tests import fra_contender_draft_2026_10_13 as fx

    from arenamcp.set_primer import research_summary

    line = research_summary(fx.primer())
    assert line.startswith("Target decks: ")
    assert "UR Izzet Spells & Jace Empower (tier 1" in line and "noncreature spells" in line
