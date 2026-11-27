"""Tests for mtgacoach.com auth profiles, deck sync, wildcard builder, leaderboard, and Discord links."""

from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient
from tests.website_app import WEBSITE_DIR, load_website_app

PATRON_KEY = "sk-patron-profile-1"
PATRON_KEY_2 = "sk-patron-profile-2"
TRIAL_KEY = "sk-trial-profile-1"


@pytest.fixture()
def site(tmp_path: Path, monkeypatch):
    (tmp_path / "templates").symlink_to(WEBSITE_DIR / "templates", target_is_directory=True)
    (tmp_path / "static").symlink_to(WEBSITE_DIR / "static", target_is_directory=True)
    monkeypatch.setenv("DISCORD_INVITE_URL", "https://discord.gg/mtgacoach-test")
    app, db, patreon = load_website_app(tmp_path, monkeypatch)
    patreon._record_key("patron1@example.com", "Patron One", PATRON_KEY, "Patreon test 1")
    patreon._record_key("patron2@example.com", "Patron Two", PATRON_KEY_2, "Patreon test 2")
    db.create_trial(
        "a" * 64, TRIAL_KEY, "2026-11-25T00:00:00Z", "2099-01-01T00:00:00Z", email="t@example.com"
    )
    with TestClient(app.app) as client:
        yield client, db, app


def _auth_header(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _local_assets(html: str) -> set[str]:
    return set(re.findall(r'(?:src|href)="(/static/[^"?#]+)', html))


def test_desktop_token_login_exchange_single_use_and_csrf(site) -> None:
    client, _db, _app = site

    # Mint single-use token via Bearer key
    mint = client.post("/v1/profile/login-token", headers=_auth_header(PATRON_KEY))
    assert mint.status_code == 200
    login_url = mint.json()["login_url"]
    parsed = urlparse(login_url)
    token = parse_qs(parsed.query)["token"][0]

    # Exchange sets HttpOnly session cookie and redirects to /profile
    ex = client.get(f"/profile/auth/exchange?token={token}", follow_redirects=False)
    assert ex.status_code == 303
    assert ex.headers["location"] == "/profile"
    assert "mc-session" in ex.cookies

    # Replay of single-use token is rejected
    replay = client.get(f"/profile/auth/exchange?token={token}", follow_redirects=False)
    assert replay.status_code == 403

    # /profile renders 200 with security headers, no inline scripts, and Discord invite
    prof_page = client.get("/profile")
    assert prof_page.status_code == 200
    assert prof_page.headers["x-content-type-options"] == "nosniff"
    assert prof_page.headers["x-frame-options"] == "DENY"
    assert "script-src 'self'" in prof_page.headers["content-security-policy"]
    assert "https://discord.gg/mtgacoach-test" in prof_page.text
    assert "#patron-lounge" in prof_page.text
    for asset in _local_assets(prof_page.text):
        assert client.get(asset).status_code == 200

    # Extract CSRF token from #profile-app
    m = re.search(r'data-csrf="([^"]+)"', prof_page.text)
    assert m, "profile page must expose data-csrf"
    csrf = m.group(1)

    # Cookie mutation without CSRF fails with 403
    no_csrf = client.patch("/v1/profile/settings", json={"display_name": "Chandra"})
    assert no_csrf.status_code == 403

    # Invalid email-style handle is rejected while dotted handles (e.g. armour.tv) are accepted
    bad_handle = client.patch(
        "/v1/profile/settings",
        headers={"X-CSRF-Token": csrf},
        json={"display_name": "user@example.com"},
    )
    assert bad_handle.status_code == 422

    dotted = client.patch(
        "/v1/profile/settings",
        headers={"X-CSRF-Token": csrf},
        json={"display_name": "armour.tv"},
    )
    assert dotted.status_code == 200
    assert dotted.json()["profile"]["display_name"] == "armour.tv"

    # Cookie mutation with valid CSRF succeeds
    ok = client.patch(
        "/v1/profile/settings",
        headers={"X-CSRF-Token": csrf},
        json={"display_name": "Chandra", "leaderboard_opt_in": True},
    )
    assert ok.status_code == 200
    assert ok.json()["profile"]["display_name"] == "Chandra"
    assert ok.json()["profile"]["leaderboard_opt_in"] is True

    # Logout requires CSRF token (cross-site logout rejected)
    bad_out = client.post("/logout", follow_redirects=False)
    assert bad_out.status_code == 403

    # Logout with valid CSRF clears session & sends Clear-Site-Data
    out = client.post("/logout", data={"csrf_token": csrf}, follow_redirects=False)
    assert out.status_code == 303
    assert '"cookies"' in out.headers.get("clear-site-data", "")
    assert client.get("/profile", follow_redirects=False).status_code == 302


def test_key_login_rejects_invalid_and_accepts_trial(site) -> None:
    client, _db, _app = site
    bad = client.post("/login/key", data={"license_key": "sk-bogus", "next": "/profile"})
    assert bad.status_code == 401

    good = client.post(
        "/login/key",
        data={"license_key": TRIAL_KEY, "next": "/profile"},
        follow_redirects=False,
    )
    assert good.status_code == 303
    assert good.headers["location"] == "/profile"
    me = client.get("/v1/profile/me").json()
    assert me["profile"]["is_patron"] is False


def test_sync_push_stats_craftability_and_deck_queue(site) -> None:
    client, _db, _app = site
    now = time.time()

    # Push collection, wildcards, rank, 2 matches, and catalog rarities
    sync_resp = client.post(
        "/v1/sync/push",
        headers=_auth_header(PATRON_KEY),
        json={
            "ranks": {"constructedClass": "Diamond", "constructedLevel": 2, "constructedStep": 3},
            "collection": {
                "cards_by_name": {"Lightning Strike": 4, "Play with Fire": 4, "Mountain": 20},
                "wildcards": {"common": 10, "uncommon": 4, "rare": 2, "mythic": 1},
                "gold": 4200,
                "gems": 800,
            },
            "catalog_entries": [
                {"name": "Lightning Strike", "rarity": "common", "grp_id": 101},
                {"name": "Play with Fire", "rarity": "uncommon", "grp_id": 102},
                {"name": "Slickshot Show-Off", "rarity": "rare", "grp_id": 103},
                {"name": "Emberheart Challenger", "rarity": "rare", "grp_id": 104},
                {"name": "Fugitive Codebreaker", "rarity": "rare", "grp_id": 105},
            ],
            "matches": [
                {
                    "match_id": "m-1",
                    "played_at": now - 120,
                    "result": "win",
                    "format_name": "Standard Ranked",
                    "deck_name": "Mono Red",
                    "deck_identity": "id-red",
                    "turns": 6,
                    "autopilot": True,
                },
                {
                    "match_id": "m-2",
                    "played_at": now - 60,
                    "result": "loss",
                    "format_name": "Standard Ranked",
                    "deck_name": "Mono Red",
                    "deck_identity": "id-red",
                    "turns": 7,
                    "autopilot": False,
                },
            ],
        },
    )
    assert sync_resp.status_code == 200
    assert sync_resp.json()["synced"]["matches"] == 2

    # Idempotent match dedupe on repeat sync
    repeat = client.post(
        "/v1/sync/push",
        headers=_auth_header(PATRON_KEY),
        json={"matches": [{"match_id": "m-1", "result": "win", "format_name": "Standard Ranked"}]},
    )
    assert repeat.json()["synced"]["matches"] == 0

    me = client.get("/v1/profile/me", headers=_auth_header(PATRON_KEY)).json()
    assert me["profile"]["constructed_rank"] == "Diamond 2"
    assert me["stats"]["all_time"]["wins"] == 1
    assert me["stats"]["all_time"]["losses"] == 1
    assert me["stats"]["all_time"]["win_rate"] == 0.5

    # Build a deck via Arena text import: needs 2 rare Slickshot Show-Off -> affordable (have 2 rare WCs)
    created = client.post(
        "/v1/decks",
        headers=_auth_header(PATRON_KEY),
        json={
            "name": "Red Aggro Planned",
            "format": "standard",
            "is_public": True,
            "queue_for_arena": True,
            "import_text": "Deck\n4 Lightning Strike\n4 Play with Fire\n2 Slickshot Show-Off\n20 Mountain",
        },
    )
    assert created.status_code == 201
    deck = created.json()["deck"]
    assert deck["craftability"]["owned"] is False
    assert deck["craftability"]["affordable"] is True
    assert deck["craftability"]["spend"]["rare"] == 2
    assert deck["sync_to_arena_status"] == "pending"

    # Build an unaffordable deck (needs 6 rares total -> short 4 rares, still 'close' <= 4)
    close_deck = client.post(
        "/v1/decks",
        headers=_auth_header(PATRON_KEY),
        json={
            "name": "Prowess Close",
            "format": "standard",
            "is_public": False,
            "import_text": "Deck\n4 Lightning Strike\n2 Slickshot Show-Off\n4 Emberheart Challenger\n20 Mountain",
        },
    ).json()["deck"]
    assert close_deck["craftability"]["affordable"] is False
    assert close_deck["craftability"]["close"] is True
    assert close_deck["craftability"]["extra_wildcards_needed"] == 4

    # Unknown card rarity must never be marked affordable
    unknown_deck = client.post(
        "/v1/decks",
        headers=_auth_header(PATRON_KEY),
        json={
            "name": "Mystery List",
            "format": "standard",
            "import_text": "Deck\n4 Completely Unknown Mythic Card\n20 Mountain",
        },
    ).json()["deck"]
    assert unknown_deck["craftability"]["affordable"] is False
    assert "Completely Unknown Mythic Card" in unknown_deck["craftability"]["unknown"]

    # Pending queue -> ack
    pending = client.get("/v1/sync/pending-decks", headers=_auth_header(PATRON_KEY)).json()["decks"]
    assert [d["id"] for d in pending] == [deck["id"]]
    ack = client.post(
        f"/v1/sync/pending-decks/{deck['id']}/ack",
        headers=_auth_header(PATRON_KEY),
        json={"status": "synced", "arena_deck_id": "arena-guid-99"},
    )
    assert ack.status_code == 200
    assert client.get("/v1/sync/pending-decks", headers=_auth_header(PATRON_KEY)).json()["decks"] == []

    # Public discover hides private deck ('Prowess Close') and shows public deck
    disc = client.get("/v1/decks/discover?craftability=affordable", headers=_auth_header(PATRON_KEY)).json()
    names = [d["name"] for d in disc["decks"]]
    assert "Red Aggro Planned" in names
    assert "Prowess Close" not in names

    # Private deck detail returns 404 to another user
    other_view = client.get(f"/decks/{close_deck['slug']}")
    assert other_view.status_code == 404
    public_view = client.get(f"/decks/{deck['slug']}")
    assert public_view.status_code == 200
    assert "Red Aggro Planned" in public_view.text

    # Re-syncing an Arena deck without explicit is_public preserves existing public flag
    client.post(
        "/v1/sync/push",
        headers=_auth_header(PATRON_KEY),
        json={
            "ranks": {
                "constructedClass": "Diamond",
                "constructedLevel": 2,
                "limitedClass": "Platinum",
                "limitedLevel": 3,
            },
            "decks": [
                {
                    "arena_deck_id": "arena-pub-1",
                    "name": "Sync Keep Public",
                    "format": "standard",
                    "main": [{"name": "Mountain", "count": 20}],
                    "is_public": True,
                }
            ],
        },
    )
    client.post(
        "/v1/sync/push",
        headers=_auth_header(PATRON_KEY),
        json={
            "decks": [
                {
                    "arena_deck_id": "arena-pub-1",
                    "name": "Sync Keep Public",
                    "format": "standard",
                    "main": [{"name": "Mountain", "count": 20}],
                }
            ]
        },
    )
    me_after = client.get("/v1/profile/me", headers=_auth_header(PATRON_KEY)).json()
    assert me_after["profile"]["limited_rank"] == "Platinum 3"
    kept = [d for d in me_after["decks"] if d["arena_deck_id"] == "arena-pub-1"][0]
    assert kept["is_public"] is True

    # Behind TLS-terminating proxy, login-token upgrades scheme to https
    https_mint = client.post(
        "/v1/profile/login-token",
        headers={**_auth_header(PATRON_KEY), "X-Forwarded-Proto": "https", "Host": "mtgacoach.com"},
    )
    assert https_mint.status_code == 200
    assert https_mint.json()["login_url"].startswith("https://mtgacoach.com/profile/auth/exchange?")


def test_leaderboard_privacy_rank_ordering_and_20_game_floor(site) -> None:
    client, _db, _app = site
    now = time.time()

    # User 1 opts in as 'MythicHero' with 20 games (15W-5L = 75%)
    client.patch(
        "/v1/profile/settings",
        headers=_auth_header(PATRON_KEY),
        json={"display_name": "MythicHero", "leaderboard_opt_in": True},
    )
    matches_u1 = [
        {
            "match_id": f"u1-{i}",
            "played_at": now - i * 10,
            "result": "win" if i < 15 else "loss",
            "format_name": "Standard Ranked",
            "deck_name": "Boros",
            "autopilot": True,
        }
        for i in range(20)
    ]
    client.post(
        "/v1/sync/push",
        headers=_auth_header(PATRON_KEY),
        json={
            "ranks": {"constructedClass": "Mythic", "constructedLeaderboardPlace": 12},
            "matches": matches_u1,
        },
    )

    # User 2 opts in as 'AlmostQualified' with 19 games (18W-1L) and Diamond 1
    client.patch(
        "/v1/profile/settings",
        headers=_auth_header(PATRON_KEY_2),
        json={"display_name": "AlmostQualified", "leaderboard_opt_in": True},
    )
    matches_u2 = [
        {
            "match_id": f"u2-{i}",
            "played_at": now - i * 10,
            "result": "win" if i < 18 else "loss",
            "format_name": "Standard Ranked",
            "deck_name": "Azorius",
        }
        for i in range(19)
    ]
    client.post(
        "/v1/sync/push",
        headers=_auth_header(PATRON_KEY_2),
        json={
            "ranks": {"constructedClass": "Diamond", "constructedLevel": 1, "constructedStep": 4},
            "matches": matches_u2,
        },
    )

    # Trial user remains unnamed: appears by default under an anonymous handle, then can opt out
    client.post(
        "/v1/sync/push",
        headers=_auth_header(TRIAL_KEY),
        json={
            "ranks": {"constructedClass": "Mythic", "constructedLeaderboardPlace": 1},
            "matches": [
                {"match_id": f"t-{i}", "result": "win", "format_name": "Standard Ranked"} for i in range(25)
            ],
        },
    )

    anon_lb = client.get("/v1/leaderboard?tab=rank").json()
    anon_handles = [e["handle"] for e in anon_lb["entries"]]
    assert any(h.startswith("Player #") for h in anon_handles)
    assert "MythicHero" in anon_handles
    assert "AlmostQualified" in anon_handles
    assert "sk-" not in str(anon_lb)
    assert "@example.com" not in str(anon_lb)

    # Trial user opts out -> excluded from both rank and winrate leaderboards
    client.patch(
        "/v1/profile/settings",
        headers=_auth_header(TRIAL_KEY),
        json={"leaderboard_opt_in": False},
    )

    # Rank leaderboard: MythicHero (#12) > AlmostQualified (Diamond 1); opted-out Trial user excluded
    rank_lb = client.get("/v1/leaderboard?tab=rank").json()
    handles = [e["handle"] for e in rank_lb["entries"]]
    assert handles == ["MythicHero", "AlmostQualified"]

    # Winrate leaderboard: 20-game minimum admits MythicHero and excludes AlmostQualified (19 games)
    wr_lb = client.get("/v1/leaderboard?tab=winrate").json()
    assert [e["handle"] for e in wr_lb["entries"]] == ["MythicHero"]

    # Public /leaderboard page renders 200 and serves assets
    lb_html = client.get("/leaderboard")
    assert lb_html.status_code == 200
    for asset in _local_assets(lb_html.text):
        assert client.get(asset).status_code == 200


def test_backfill_from_match_bundles_populates_matches_and_private_decks(site) -> None:
    import gzip
    import json
    import sys

    client, db, _app = site
    profiles = sys.modules["profiles"]
    match_bundles = sys.modules["match_bundles"]

    owner = match_bundles.owner_of(PATRON_KEY)
    rel = "2026-11-26/" + owner + "/inst-1/bundle-match-1.json.gz"
    target = match_bundles.bundle_root() / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    bundle_payload = {
        "final_state": {
            "format_name": "Ladder",
            "deck_cards": [101] * 20 + [301] * 40,
            "commander_grp_ids": [],
        }
    }
    with gzip.open(target, "wt", encoding="utf-8") as fh:
        json.dump(bundle_payload, fh)

    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO card_prints_cache (grp_id, name, rarity) VALUES (?, ?, ?), (?, ?, ?)",
            (101, "Lightning Strike", "common", 301, "Mountain", "common"),
        )
    match_bundles.insert_bundle(
        owner=owner,
        install_id="inst-1",
        match_id="bundle-match-1",
        license_key=PATRON_KEY,
        auth_source="subscriber",
        size=123,
        path=rel,
        app_version="4.0.0",
        platform="darwin",
        result="win",
        schema_version=1,
        autopilot_enabled=True,
        format_name="Ladder",
        deck_name="DECK PLAYBOOK v2: Burn Aggro",
    )

    first = profiles.backfill_from_match_bundles(extract_decks=True)
    assert first["matches_inserted"] == 1
    assert first["decks_created"] == 1

    # Re-running is idempotent
    second = profiles.backfill_from_match_bundles(extract_decks=True)
    assert second["matches_inserted"] == 0
    assert second["decks_created"] == 0

    me = client.get("/v1/profile/me", headers=_auth_header(PATRON_KEY)).json()
    assert me["profile"]["leaderboard_opt_in"] is True
    assert me["profile"]["effective_handle"].startswith("Player #")
    assert me["stats"]["all_time"]["wins"] == 1
    assert len(me["decks"]) == 1
    assert me["decks"][0]["name"] == "Burn Aggro"
    assert me["decks"][0]["is_public"] is True
    assert me["decks"][0]["wins"] == 1

    # Client sync enriches the existing bundle match in-place without duplicating
    client.post(
        "/v1/sync/push",
        headers=_auth_header(PATRON_KEY),
        json={
            "matches": [
                {
                    "match_id": "bundle-match-1",
                    "result": "win",
                    "format_name": "Standard Ranked",
                    "turns": 5,
                    "deck_colors": "R",
                }
            ]
        },
    )
    me_after = client.get("/v1/profile/me", headers=_auth_header(PATRON_KEY)).json()
    assert me_after["stats"]["all_time"]["wins"] == 1
    assert me_after["recent_matches"][0]["turns"] == 5
    assert me_after["recent_matches"][0]["deck_slug"] == me["decks"][0]["slug"]

    # Opting out hides even default-public synced decks from public /decks/{slug}
    client.patch(
        "/v1/profile/settings",
        headers=_auth_header(PATRON_KEY),
        json={"leaderboard_opt_in": False},
    )
    assert client.get(f"/decks/{me['decks'][0]['slug']}").status_code == 404


def test_upsert_sourced_decks_idempotent_discoverable_and_excluded_from_player_leaderboard(site) -> None:
    import sys

    client, _db, _app = site
    profiles = sys.modules["profiles"]

    sample_decks = [
        {
            "name": "Mono Red Prowess",
            "format": "standard",
            "source_site": "AetherHub",
            "source": "AetherHub Standard (Arena tracker)",
            "url": "https://aetherhub.com/Deck/mono-red-prowess",
            "colors": "R",
            "export": "Deck\n4 Monastery Swiftspear\n20 Mountain\n\nSideboard\n2 Abrade",
        },
        {
            "name": "Azorius Control",
            "format": "historic",
            "source_site": "MTGGoldfish",
            "source": "MTGGoldfish Historic (Arena metagame)",
            "url": "https://www.mtggoldfish.com/archetype/historic-azorius",
            "colors": "WU",
            "main": [{"name": "Counterspell", "count": 4}, {"name": "Island", "count": 24}],
        },
    ]

    first = profiles.upsert_sourced_decks(sample_decks)
    assert first["created"] == 2
    assert first["updated"] == 0
    assert first["total_public_sourced"] == 2

    second = profiles.upsert_sourced_decks(sample_decks)
    assert second["created"] == 0
    assert second["updated"] == 2
    assert second["total_public_sourced"] == 2

    discover = client.get("/v1/decks/discover", headers=_auth_header(PATRON_KEY)).json()
    names = {d["name"]: d for d in discover["decks"]}
    assert "Mono Red Prowess" in names
    assert "Azorius Control" in names
    assert names["Mono Red Prowess"]["author_name"] == "AetherHub"
    assert names["Azorius Control"]["author_name"] == "MTGGoldfish"
    assert "craftability" in names["Mono Red Prowess"]

    # System source profiles never appear in player rank/winrate leaderboards
    rank_handles = {e["handle"] for e in client.get("/v1/leaderboard?tab=rank&window=all").json()["entries"]}
    wr_handles = {e["handle"] for e in client.get("/v1/leaderboard?tab=winrate&window=all").json()["entries"]}
    assert "AetherHub" not in rank_handles
    assert "MTGGoldfish" not in rank_handles
    assert "AetherHub" not in wr_handles
    assert "MTGGoldfish" not in wr_handles


def test_clickable_top_decks_player_profiles_and_two_clone_actions(site) -> None:
    client, _db, _app = site
    now = time.time()

    client.patch(
        "/v1/profile/settings",
        headers=_auth_header(PATRON_KEY),
        json={"display_name": "ArmourTest", "leaderboard_opt_in": True},
    )
    client.post(
        "/v1/sync/push",
        headers=_auth_header(PATRON_KEY),
        json={
            "ranks": {"constructedClass": "Mythic", "constructedPercentile": 91.5},
            "decks": [
                {
                    "arena_deck_id": "auras-1",
                    "name": "Mono-White Auras",
                    "format": "standard",
                    "main": [{"name": "Plains", "count": 20}, {"name": "Sheltered by Ghosts", "count": 4}],
                },
                {
                    "arena_deck_id": "secret-1",
                    "name": "Secret Brew",
                    "format": "standard",
                    "is_public": False,
                    "main": [{"name": "Island", "count": 60}],
                },
            ],
            "matches": [
                {
                    "match_id": f"m-{i}",
                    "played_at": now - i * 30,
                    "result": "win" if i < 5 else "loss",
                    "format_name": "Standard Ranked",
                    "deck_name": "Mono-White Auras",
                    "opponent_colors": "BR",
                    "turns": 6,
                    "autopilot": True,
                }
                for i in range(7)
            ],
        },
    )

    me_p1 = client.get("/v1/profile/me", headers=_auth_header(PATRON_KEY)).json()
    auras = [d for d in me_p1["decks"] if d["name"] == "Mono-White Auras"][0]
    assert auras["is_public"] is True
    assert me_p1["recent_matches"][0]["deck_slug"] == auras["slug"]

    # Leaderboard exposes profile_url + top_deck_slug
    lb = client.get("/v1/leaderboard?tab=rank&window=30d").json()
    entry = [e for e in lb["entries"] if e["handle"] == "ArmourTest"][0]
    assert entry["profile_url"] == "/players/ArmourTest"
    assert entry["top_deck"] == "Mono-White Auras"
    assert entry["top_deck_slug"] == auras["slug"]
    assert entry["top_deck_games"] == 7

    # Public player page shows Mono-White Auras, hides Secret Brew & emails
    pp_html = client.get("/players/ArmourTest")
    assert pp_html.status_code == 200
    assert f"/decks/{auras['slug']}" in pp_html.text
    assert "Secret Brew" not in pp_html.text
    assert "@example.com" not in pp_html.text

    # Sign in as second subscriber to verify deck detail stats + both add buttons
    mint2 = client.post("/v1/profile/login-token", headers=_auth_header(PATRON_KEY_2)).json()
    tok2 = parse_qs(urlparse(mint2["login_url"]).query)["token"][0]
    client.get(f"/profile/auth/exchange?token={tok2}", follow_redirects=False)

    detail_html = client.get(f"/decks/{auras['slug']}")
    assert detail_html.status_code == 200
    assert "detail-save-profile-btn" in detail_html.text
    assert "detail-clone-btn" in detail_html.text
    assert "5W-2L" in detail_html.text
    assert "BR" in detail_html.text
    assert "AP " in detail_html.text

    m_csrf = re.search(r'data-csrf="([^"]+)"', detail_html.text)
    assert m_csrf
    csrf2 = m_csrf.group(1)

    # 1. Add to profile without queueing for Arena
    save_res = client.post(
        f"/v1/decks/{auras['id']}/clone",
        headers={"X-CSRF-Token": csrf2},
        json={"queue_for_arena": False},
    )
    assert save_res.status_code == 201
    assert save_res.json()["deck"]["sync_to_arena_status"] == ""
    assert client.get("/v1/sync/pending-decks", headers=_auth_header(PATRON_KEY_2)).json()["decks"] == []

    # 2. Add & send to Coach / Arena queues for pickup
    queue_res = client.post(
        f"/v1/decks/{auras['id']}/clone",
        headers={"X-CSRF-Token": csrf2},
        json={"queue_for_arena": True},
    )
    assert queue_res.status_code == 201
    assert queue_res.json()["deck"]["sync_to_arena_status"] == "pending"
    pending2 = client.get("/v1/sync/pending-decks", headers=_auth_header(PATRON_KEY_2)).json()["decks"]
    assert len(pending2) == 1

    # Opted-out player's public profile & decks return 404 to others
    client.patch(
        "/v1/profile/settings",
        headers=_auth_header(PATRON_KEY),
        json={"leaderboard_opt_in": False},
    )
    assert client.get("/players/ArmourTest").status_code == 404
    assert client.get(f"/decks/{auras['slug']}").status_code == 404
