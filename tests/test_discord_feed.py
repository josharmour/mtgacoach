"""Tests for automatic Discord leaderboard & highest win-rate deck feed."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from tests.website_app import WEBSITE_DIR, load_website_app


def _seed_player(
    db,
    profiles_mod,
    owner: str,
    *,
    handle: str = "",
    opt_in: bool = True,
    rank_class: str = "Gold",
    rank_tier: int = 1,
    mythic_pct: float | None = None,
    wins: int = 0,
    losses: int = 0,
    deck_name: str = "Mono-Red Aggro",
    deck_public: bool = True,
    deck_wins: int = 0,
    deck_losses: int = 0,
) -> str:
    now = time.time()
    score = profiles_mod.compute_rank_score(rank_class, rank_tier, 0, mythic_pct, None)
    suffix = owner.rsplit("-", 1)[-1]
    deck_id = f"deck-{suffix}"
    slug = f"dslug-{suffix}"
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO player_profiles (
                owner, display_name, leaderboard_opt_in, is_patron,
                constructed_rank_class, constructed_rank_tier, constructed_rank_step,
                constructed_mythic_pct, constructed_rank_score, created_at, updated_at
            ) VALUES (?, ?, ?, 1, ?, ?, 0, ?, ?, ?, ?)
            """,
            (owner, handle, 1 if opt_in else 0, rank_class, rank_tier, mythic_pct, score, now, now),
        )
        conn.execute("DELETE FROM player_matches WHERE owner = ?", (owner,))
        for idx in range(wins + losses):
            res = "win" if idx < wins else "loss"
            conn.execute(
                """
                INSERT INTO player_matches (
                    owner, match_id, played_at, result, format_name, format_bucket,
                    deck_name, autopilot
                ) VALUES (?, ?, ?, ?, 'Standard', 'standard', ?, 0)
                """,
                (owner, f"m-{owner}-{idx}", now - idx * 60.0, res, deck_name),
            )
        conn.execute(
            """
            INSERT OR REPLACE INTO synced_decks (
                id, owner, slug, name, format, main_json, sideboard_json,
                commander_json, companion_json, is_public, wins, losses,
                autopilot_wins, autopilot_losses, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'standard', '[{"name":"Mountain","count":20}]', '[]', '[]', '[]', ?, ?, ?, 0, 0, ?, ?)
            """,
            (
                deck_id,
                owner,
                slug,
                deck_name,
                1 if deck_public else 0,
                deck_wins,
                deck_losses,
                now,
                now,
            ),
        )
    return deck_id


def _run(coro):
    return asyncio.run(coro)


def test_discord_feed_noop_without_webhook(tmp_path: Path, monkeypatch):
    load_website_app(tmp_path, monkeypatch, "app_discord_noop")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    discord_feed = sys.modules["discord_feed"]

    res = _run(discord_feed.check_and_post_updates())
    assert res["ok"] is True
    assert res["skipped"] == "no_webhook"


def test_discord_feed_seed_switches_controls_and_failures(tmp_path: Path, monkeypatch, caplog):
    _, db, _ = load_website_app(tmp_path, monkeypatch, "app_discord_feed")
    profiles = sys.modules["profiles"]
    discord_feed = sys.modules["discord_feed"]

    webhook_token = "super-secret-webhook-token-xyz"
    webhook_url = f"https://discord.com/api/webhooks/12345/{webhook_token}"
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", webhook_url)
    monkeypatch.setenv("DISCORD_FEED_COOLDOWN_SECONDS", "600")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://mtgacoach.com")

    # Seed 4 players across Rank, Win Rate (>=20 games), and Decks (>=10 games)
    _seed_player(
        db,
        profiles,
        "owner-secret-a",
        handle="Alice",
        rank_class="Mythic",
        mythic_pct=96.0,
        wins=18,
        losses=6,
        deck_name="Alpha Aggro",
        deck_wins=18,
        deck_losses=6,  # 75.0%
    )
    _seed_player(
        db,
        profiles,
        "owner-secret-b",
        handle="Bob",
        rank_class="Mythic",
        mythic_pct=92.0,
        wins=14,
        losses=8,
        deck_name="Beta Control",
        deck_wins=14,
        deck_losses=8,  # 63.6%
    )
    _seed_player(
        db,
        profiles,
        "owner-secret-c",
        handle="Carol",
        rank_class="Mythic",
        mythic_pct=88.0,
        wins=12,
        losses=10,
        deck_name="Gamma Midrange",
        deck_wins=12,
        deck_losses=10,  # 54.5%
    )
    _seed_player(
        db,
        profiles,
        "owner-secret-d",
        handle="Dave",
        rank_class="Diamond",
        rank_tier=1,
        wins=11,
        losses=11,
        deck_name="Delta Tempo",
        deck_wins=11,
        deck_losses=11,  # 50.0%
    )
    # Controls that must NEVER qualify or leak:
    _seed_player(
        db,
        profiles,
        "owner-secret-optout",
        handle="HiddenStar",
        opt_in=False,
        rank_class="Mythic",
        mythic_pct=99.9,
        wins=25,
        losses=0,
        deck_name="Hidden OP Deck",
        deck_wins=25,
        deck_losses=0,
    )
    _seed_player(
        db,
        profiles,
        "owner-secret-privdeck",
        handle="PrivateDeckUser",
        opt_in=True,
        rank_class="Bronze",
        rank_tier=4,
        wins=0,
        losses=0,
        deck_name="Secret Tech",
        deck_public=False,
        deck_wins=20,
        deck_losses=0,
    )
    _seed_player(
        db,
        profiles,
        "owner-secret-smalldeck",
        handle="SmallSample",
        opt_in=True,
        rank_class="Bronze",
        rank_tier=4,
        wins=1,
        losses=0,
        deck_name="One Game Wonder",
        deck_public=True,
        deck_wins=1,
        deck_losses=0,
    )

    posts: list[dict] = []
    fail_next = False

    def handler(request: httpx.Request) -> httpx.Response:
        if fail_next:
            return httpx.Response(500, json={"error": "upstream"})
        posts.append(json.loads(request.content.decode()))
        return httpx.Response(204)

    async def run_check(now: float, force: bool = False):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await discord_feed.check_and_post_updates(force_post=force, http_client=client, now=now)

    # 1. Cold start seeds silently without spamming Discord
    res1 = _run(run_check(now=1000.0))
    assert res1["results"] == {"rank_top3": "seeded", "winrate_top3": "seeded", "decks_top3": "seeded"}
    assert posts == []

    # 2. Same order + rank bump inside #1 (no Top 3 switch) + #4 handle rename -> no post!
    with db.get_db() as conn:
        conn.execute(
            "UPDATE player_profiles SET constructed_mythic_pct = 97.5, constructed_rank_score = ? WHERE owner = ?",
            (profiles.compute_rank_score("Mythic", 1, 0, 97.5, None), "owner-secret-a"),
        )
        conn.execute(
            "UPDATE player_profiles SET display_name = 'AliceRenamed' WHERE owner = 'owner-secret-a'"
        )
        conn.execute("UPDATE player_profiles SET display_name = 'DaveRenamed' WHERE owner = 'owner-secret-d'")
    res2 = _run(run_check(now=1100.0))
    assert res2["results"] == {
        "rank_top3": "unchanged",
        "winrate_top3": "unchanged",
        "decks_top3": "unchanged",
    }
    assert posts == []

    # 3. Bob overtakes Alice on Rank AND overtakes Alice on Win Rate + Beta Control climbs to #1 deck!
    with db.get_db() as conn:
        conn.execute(
            "UPDATE player_profiles SET constructed_mythic_pct = 98.5, constructed_rank_score = ? WHERE owner = 'owner-secret-b'",
            (profiles.compute_rank_score("Mythic", 1, 0, 98.5, None),),
        )
        # Boost Bob + Beta Control past Alice (need >75% on 30+ games)
        now = time.time()
        for i in range(50):
            conn.execute(
                """
                INSERT INTO player_matches (
                    owner, match_id, played_at, result, format_name, format_bucket, deck_name, autopilot
                ) VALUES ('owner-secret-b', ?, ?, 'win', 'Standard', 'standard', 'Beta Control', 0)
                """,
                (f"m-boost-{i}", now - i),
            )
        conn.execute("UPDATE synced_decks SET wins = 64 WHERE id = 'deck-b'")

    res3 = _run(run_check(now=1200.0))
    assert res3["results"] == {"rank_top3": "posted", "winrate_top3": "posted", "decks_top3": "posted"}
    assert len(posts) == 3

    combined_payload = json.dumps(posts)
    assert "Bob" in combined_payload
    assert "AliceRenamed" in combined_payload
    assert "Beta Control" in combined_payload
    assert "https://mtgacoach.com/players/Bob" in combined_payload
    assert "https://mtgacoach.com/decks/dslug-b" in combined_payload
    # Privacy controls must never appear anywhere in the webhook payload
    assert "owner-secret-" not in combined_payload
    assert "HiddenStar" not in combined_payload
    assert "Hidden OP Deck" not in combined_payload
    assert "Secret Tech" not in combined_payload
    assert "One Game Wonder" not in combined_payload

    # 4. Cooldown suppresses rapid flip; reverting before cooldown ends yields 0 posts
    with db.get_db() as conn:
        conn.execute(
            "UPDATE player_profiles SET constructed_mythic_pct = 99.0, constructed_rank_score = ? WHERE owner = 'owner-secret-c'",
            (profiles.compute_rank_score("Mythic", 1, 0, 99.0, None),),
        )
    res4 = _run(run_check(now=1500.0))
    assert res4["results"]["rank_top3"] == "cooldown"
    assert len(posts) == 3

    with db.get_db() as conn:
        conn.execute(
            "UPDATE player_profiles SET constructed_mythic_pct = 88.0, constructed_rank_score = ? WHERE owner = 'owner-secret-c'",
            (profiles.compute_rank_score("Mythic", 1, 0, 88.0, None),),
        )
    res4b = _run(run_check(now=1900.0))
    assert res4b["results"]["rank_top3"] == "unchanged"
    assert len(posts) == 3

    # 5. Sustained switch + HTTP 500 failure keeps last_posted_at/last_ids_json untouched so next tick retries cleanly
    with caplog.at_level(logging.INFO):
        with db.get_db() as conn:
            conn.execute(
                "UPDATE player_profiles SET constructed_mythic_pct = 99.0, constructed_rank_score = ? WHERE owner = 'owner-secret-c'",
                (profiles.compute_rank_score("Mythic", 1, 0, 99.0, None),),
            )
        fail_next = True
        res5 = _run(run_check(now=2000.0))
        assert res5["results"]["rank_top3"] == "error"
        assert len(posts) == 3

        fail_next = False
        res6 = _run(run_check(now=2060.0))
        assert res6["results"]["rank_top3"] == "posted"
        assert len(posts) == 4
        assert "Carol" in posts[-1]["embeds"][0]["description"]

    assert webhook_token not in caplog.text


def test_leaderboard_api_and_admin_feed_auth(tmp_path: Path, monkeypatch):
    app_mod, db, _ = load_website_app(tmp_path, monkeypatch, "app_discord_privacy")
    profiles = sys.modules["profiles"]
    discord_feed = sys.modules["discord_feed"]
    _seed_player(
        db,
        profiles,
        "secret-license-owner",
        handle="PlayerOne",
        rank_class="Mythic",
        mythic_pct=95.0,
        wins=22,
        losses=5,
        deck_wins=22,
        deck_losses=5,
    )

    client = TestClient(app_mod.app)
    for tab in ("rank", "winrate", "decks"):
        resp = client.get(f"/v1/leaderboard?tab={tab}")
        assert resp.status_code == 200
        text = resp.text
        assert "secret-license-owner" not in text
        for entry in resp.json()["entries"]:
            assert "owner" not in entry

    assert client.post("/admin/discord-feed/check").status_code == 403
    admin_resp = client.post("/admin/discord-feed/check", headers={"X-Admin-Key": "test-admin"})
    assert admin_resp.status_code == 200
    assert admin_resp.json()["skipped"] == "no_webhook"

    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/123/abc")
    monkeypatch.setenv("DISCORD_FEED_ENABLED", "0")
    discord_feed.notify_leaderboard_dirty()
    assert discord_feed._dirty_task is None


def test_player_profile_redirects_stale_anonymous_handle(tmp_path: Path, monkeypatch):
    (tmp_path / "templates").symlink_to(WEBSITE_DIR / "templates", target_is_directory=True)
    (tmp_path / "static").symlink_to(WEBSITE_DIR / "static", target_is_directory=True)
    app_mod, db, _ = load_website_app(tmp_path, monkeypatch, "app_player_redirect")
    profiles = sys.modules["profiles"]

    _seed_player(db, profiles, "df45999900001111", handle="", wins=5, losses=2)
    _seed_player(db, profiles, "optout9900001111", handle="HiddenUser", opt_in=False, wins=5, losses=2)

    client = TestClient(app_mod.app)
    anon_url = "/players/Player%20%23DF45"

    # Before setting a custom handle, anonymous URL renders directly
    r1 = client.get(anon_url, follow_redirects=False)
    assert r1.status_code == 200
    assert "Player #DF45" in r1.text

    # After setting custom handle, stale anonymous URL 302-redirects to canonical handle
    with db.get_db() as conn:
        conn.execute("UPDATE player_profiles SET display_name = 'Miles18' WHERE owner = 'df45999900001111'")
    r2 = client.get(anon_url, follow_redirects=False)
    assert r2.status_code == 302
    assert r2.headers["location"] == "/players/Miles18"

    r3 = client.get(anon_url, follow_redirects=True)
    assert r3.status_code == 200
    assert "Miles18" in r3.text

    # Opted-out control remains 404 under both handles
    assert client.get("/players/Player%20%23OPTO", follow_redirects=False).status_code == 404
    assert client.get("/players/HiddenUser", follow_redirects=False).status_code == 404
