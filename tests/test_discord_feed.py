"""Tests for automatic Discord leaderboard & highest win-rate deck feed."""

from __future__ import annotations

import asyncio
import json
import logging
import re
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
    is_patron: bool = True,
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
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                owner,
                handle,
                1 if opt_in else 0,
                1 if is_patron else 0,
                rank_class,
                rank_tier,
                mythic_pct,
                score,
                now,
                now,
            ),
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


def test_discord_feed_live_board_lifecycle_controls_and_self_heal(tmp_path: Path, monkeypatch, caplog):
    _, db, _ = load_website_app(tmp_path, monkeypatch, "app_discord_feed")
    profiles = sys.modules["profiles"]
    discord_feed = sys.modules["discord_feed"]

    # Verify legacy table without message_id migrates cleanly via init_db()
    with db.get_db() as conn:
        conn.execute("DROP TABLE IF EXISTS discord_feed_state")
        conn.executescript(
            """
            CREATE TABLE discord_feed_state (
                feed_key TEXT PRIMARY KEY,
                last_ids_json TEXT NOT NULL DEFAULT '[]',
                last_snapshot_json TEXT NOT NULL DEFAULT '[]',
                last_posted_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            );
            """
        )
    discord_feed.init_db()
    with db.get_db() as conn:
        cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(discord_feed_state)").fetchall()}
        assert "message_id" in cols

    webhook_token = "super-secret-webhook-token-xyz"
    webhook_url = f"https://discord.com/api/webhooks/12345/{webhook_token}?thread_id=777"
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", webhook_url)
    monkeypatch.setenv("DISCORD_FEED_COOLDOWN_SECONDS", "600")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://mtgacoach.com")

    # Seed players across Rank, Win Rate (>=20 games), and Decks (>=10 games)
    _seed_player(
        db,
        profiles,
        "owner-secret-a",
        handle="Alice",
        rank_class="Mythic",
        mythic_pct=96.0,
        wins=18,
        losses=6,
        deck_name="[S] Alpha Aggro",
        deck_wins=18,
        deck_losses=6,  # 75.0%
    )
    _seed_player(
        db,
        profiles,
        "owner-secret-b",
        handle="Bob",
        is_patron=False,
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
    _seed_player(
        db,
        profiles,
        "owner-secret-e",
        handle="Eve",
        rank_class="Platinum",
        rank_tier=1,
        wins=12,
        losses=10,
        deck_name="Epsilon Ramp",
        deck_wins=12,
        deck_losses=10,  # 54.5%
    )
    # Unranked high-winrate player: qualifies for Win Rate, NEVER Rank!
    _seed_player(
        db,
        profiles,
        "owner-secret-unranked",
        handle="CasualGrinder",
        rank_class="",
        rank_tier=0,
        wins=18,
        losses=4,
        deck_name="Grinder Deck",
        deck_wins=18,
        deck_losses=4,  # 81.8%
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

    requests_log: list[tuple[str, str, dict]] = []
    next_status: int | None = None
    post_counter = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal next_status, post_counter
        payload = json.loads(request.content.decode()) if request.content else {}
        if next_status is not None:
            status = next_status
            next_status = None
            return httpx.Response(status, json={"error": "simulated"})
        requests_log.append((request.method, str(request.url), payload))
        if request.method == "POST":
            post_counter += 1
            return httpx.Response(200, json={"id": f"msg-{1000 + post_counter}"})
        return httpx.Response(200, json={"id": "msg-1001"})

    async def run_check(now: float, force: bool = False):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await discord_feed.check_and_post_updates(force_post=force, http_client=client, now=now)

    # 1. Cold start creates 1 live board message via POST ?wait=true and persists message_id
    res1 = _run(run_check(now=1000.0))
    assert res1["results"] == {"live_board": "created"}
    assert len(requests_log) == 1
    method1, url1, payload1 = requests_log[0]
    assert method1 == "POST"
    assert "wait=true" in url1
    assert "thread_id=777" in url1
    assert payload1["username"] == "MTGA Coach Leaderboard"
    assert payload1["allowed_mentions"] == {"parse": []}
    assert "<t:1000:R>" in payload1["content"]
    assert "★ = Mythic Patron" in payload1["content"]
    assert [e["title"] for e in payload1["embeds"]] == [
        "Highest Rank",
        "Highest Win Rate (20+ games)",
        "Top Decks (10+ games)",
    ]

    with db.get_db() as conn:
        row = conn.execute(
            "SELECT message_id FROM discord_feed_state WHERE feed_key = 'live_board'"
        ).fetchone()
        assert row is not None and row["message_id"] == "msg-1001"

    # 2. Markdown escaping, Mythic Patron badge (★), & privacy controls
    rank_desc = payload1["embeds"][0]["description"]
    winrate_desc = payload1["embeds"][1]["description"]
    decks_desc = payload1["embeds"][2]["description"]
    assert r"\[S\] Alpha Aggro" in rank_desc
    assert r"\[S\] Alpha Aggro" in decks_desc
    assert "CasualGrinder" not in rank_desc
    assert "CasualGrinder" in winrate_desc
    assert "Unranked" not in winrate_desc
    assert "[Alice](https://mtgacoach.com/players/Alice) ★" in rank_desc
    assert "[Alice](https://mtgacoach.com/players/Alice) ★" in winrate_desc
    assert "by [Alice](https://mtgacoach.com/players/Alice) ★" in decks_desc
    assert "[Bob](https://mtgacoach.com/players/Bob) ★" not in rank_desc
    assert "[Bob](https://mtgacoach.com/players/Bob) ★" not in winrate_desc
    assert "[Bob](https://mtgacoach.com/players/Bob) ★" not in decks_desc

    combined_payload = json.dumps(payload1)
    assert "https://mtgacoach.com/players/Alice" in combined_payload
    assert "https://mtgacoach.com/decks/dslug-a" in combined_payload
    assert "owner-secret-" not in combined_payload
    assert "HiddenStar" not in combined_payload
    assert "Hidden OP Deck" not in combined_payload
    assert "Secret Tech" not in combined_payload
    assert "One Game Wonder" not in combined_payload

    # 3. Unchanged check sends 0 HTTP calls
    res2 = _run(run_check(now=1050.0))
    assert res2["results"] == {"live_board": "unchanged"}
    assert len(requests_log) == 1

    # 4. Change within cooldown (600s) is suppressed without HTTP calls
    with db.get_db() as conn:
        conn.execute(
            "UPDATE player_profiles SET constructed_mythic_pct = 98.5, constructed_rank_score = ? WHERE owner = 'owner-secret-b'",
            (profiles.compute_rank_score("Mythic", 1, 0, 98.5, None),),
        )
    res3 = _run(run_check(now=1200.0))
    assert res3["results"] == {"live_board": "cooldown"}
    assert len(requests_log) == 1

    # 5. After cooldown, silent PATCH edits existing msg-1001 in place with NO username field
    res4 = _run(run_check(now=1700.0))
    assert res4["results"] == {"live_board": "edited"}
    assert len(requests_log) == 2
    method2, url2, payload2 = requests_log[1]
    assert method2 == "PATCH"
    assert "/messages/msg-1001" in url2
    assert "thread_id=777" in url2
    assert "wait=true" not in url2
    assert "username" not in payload2
    assert "<t:1700:R>" in payload2["content"]
    assert payload2["embeds"][0]["description"].index("Bob") < payload2["embeds"][0]["description"].index(
        "Alice"
    )

    # 6. HTTP 500 on PATCH leaves DB state untouched so immediate retry succeeds without leaking token
    with caplog.at_level(logging.INFO):
        with db.get_db() as conn:
            conn.execute(
                "UPDATE player_profiles SET constructed_mythic_pct = 99.0, constructed_rank_score = ? WHERE owner = 'owner-secret-c'",
                (profiles.compute_rank_score("Mythic", 1, 0, 99.0, None),),
            )
        next_status = 500
        res5 = _run(run_check(now=2400.0))
        assert res5["results"] == {"live_board": "error"}
        assert len(requests_log) == 2

        res6 = _run(run_check(now=2460.0))
        assert res6["results"] == {"live_board": "edited"}
        assert len(requests_log) == 3
        assert "Carol" in requests_log[2][2]["embeds"][0]["description"]

    # 7. HTTP 404 on PATCH (deleted message) self-heals by POST ?wait=true and updates message_id
    with db.get_db() as conn:
        conn.execute(
            "UPDATE player_profiles SET display_name = 'CarolRenamed' WHERE owner = 'owner-secret-c'"
        )
    next_status = 404
    res7 = _run(run_check(now=3200.0))
    assert res7["results"] == {"live_board": "created"}
    assert len(requests_log) == 4
    method4, url4, payload4 = requests_log[3]
    assert method4 == "POST"
    assert "wait=true" in url4
    assert payload4["username"] == "MTGA Coach Leaderboard"

    with db.get_db() as conn:
        row = conn.execute(
            "SELECT message_id FROM discord_feed_state WHERE feed_key = 'live_board'"
        ).fetchone()
        assert row is not None and row["message_id"] == "msg-1002"

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


def test_deck_rename_api_match_propagation_and_permissions(tmp_path: Path, monkeypatch):
    (tmp_path / "templates").symlink_to(WEBSITE_DIR / "templates", target_is_directory=True)
    (tmp_path / "static").symlink_to(WEBSITE_DIR / "static", target_is_directory=True)
    app_mod, db, _ = load_website_app(tmp_path, monkeypatch, "app_deck_rename")
    profiles = sys.modules["profiles"]

    _seed_player(
        db,
        profiles,
        "owner-rename-1",
        handle="Miles18",
        wins=8,
        losses=4,
        deck_name="Standard Deck",
        deck_wins=8,
        deck_losses=4,
    )
    _seed_player(db, profiles, "other-user-2", handle="OtherUser", wins=2, losses=1, deck_name="Other Deck")
    with db.get_db() as conn:
        conn.execute("UPDATE synced_decks SET deck_identity = 'ident-rename-1' WHERE id = 'deck-1'")
        conn.execute(
            "UPDATE player_matches SET deck_identity = 'ident-rename-1' WHERE owner = 'owner-rename-1'"
        )

    tok_owner, _ = profiles.mint_login_token("owner-rename-1", "patreon")
    tok_other, _ = profiles.mint_login_token("other-user-2", "patreon")

    anon_client = TestClient(app_mod.app)
    owner_client = TestClient(app_mod.app)
    other_client = TestClient(app_mod.app)

    assert (
        owner_client.get(f"/profile/auth/exchange?token={tok_owner}", follow_redirects=False).status_code
        == 303
    )
    assert (
        other_client.get(f"/profile/auth/exchange?token={tok_other}", follow_redirects=False).status_code
        == 303
    )
    owner_csrf = re.search(r'data-csrf="([^"]+)"', owner_client.get("/profile").text).group(1)
    other_csrf = re.search(r'data-csrf="([^"]+)"', other_client.get("/profile").text).group(1)

    # Also verify player_profile shows Rename only for owner
    own_player_html = owner_client.get("/players/Miles18").text
    other_player_html = other_client.get("/players/Miles18").text
    assert 'class="mini-btn js-rename-deck"' in own_player_html
    assert 'id="rename-deck-dialog"' in own_player_html
    assert "js-rename-deck" not in other_player_html
    assert 'id="rename-deck-dialog"' not in other_player_html

    # Control: signed-out and non-owner views never render rename UI
    assert 'id="detail-rename-btn"' not in anon_client.get("/decks/dslug-1").text
    assert 'id="detail-rename-btn"' not in other_client.get("/decks/dslug-1").text
    assert 'id="rename-deck-dialog"' not in anon_client.get("/decks/dslug-1").text
    assert 'id="detail-rename-btn"' in owner_client.get("/decks/dslug-1").text

    # Control: non-owner cannot rename someone else's deck
    denied = other_client.patch(
        "/v1/decks/deck-1",
        headers={"X-CSRF-Token": other_csrf},
        json={"name": "Hijacked Name"},
    )
    assert denied.status_code == 404

    # Control: blank/whitespace deck name rejected with 422
    blank = owner_client.patch(
        "/v1/decks/deck-1",
        headers={"X-CSRF-Token": owner_csrf},
        json={"name": "   "},
    )
    assert blank.status_code == 422

    # Owner renames deck cleanly; deck + matching player_matches + win/loss stats stay intact
    renamed = owner_client.patch(
        "/v1/decks/deck-1",
        headers={"X-CSRF-Token": owner_csrf},
        json={"name": "  Boros   Burn  "},
    )
    assert renamed.status_code == 200
    assert renamed.json()["deck"]["name"] == "Boros Burn"
    assert renamed.json()["deck"]["wins"] == 8
    assert renamed.json()["deck"]["losses"] == 4

    with db.get_db() as conn:
        m_names = {
            r[0]
            for r in conn.execute(
                "SELECT deck_name FROM player_matches WHERE owner = 'owner-rename-1'"
            ).fetchall()
        }
        assert m_names == {"Boros Burn"}

    detail_html = owner_client.get("/decks/dslug-1").text
    assert "Boros Burn" in detail_html
    assert 'data-deck-name="Boros Burn"' in detail_html


def test_canonical_deck_name_nicknames_and_community_stats(tmp_path: Path, monkeypatch):
    (tmp_path / "templates").symlink_to(WEBSITE_DIR / "templates", target_is_directory=True)
    (tmp_path / "static").symlink_to(WEBSITE_DIR / "static", target_is_directory=True)
    app_mod, db, _ = load_website_app(tmp_path, monkeypatch, "app_canonical_decks")
    profiles = sys.modules["profiles"]

    alice = "alice-canon-1"
    bob = "bob-canon-2"
    carol = "carol-other-3"
    with db.get_db() as conn:
        profiles.ensure_profile(conn, alice)
        profiles.ensure_profile(conn, bob)
        profiles.ensure_profile(conn, carol)
        conn.execute("UPDATE player_profiles SET display_name = 'Alice' WHERE owner = ?", (alice,))
        conn.execute("UPDATE player_profiles SET display_name = 'Bob' WHERE owner = ?", (bob,))
        conn.execute("UPDATE player_profiles SET display_name = 'Carol' WHERE owner = ?", (carol,))

    tok_a, _ = profiles.mint_login_token(alice, "patreon")
    tok_b, _ = profiles.mint_login_token(bob, "patreon")
    client_a = TestClient(app_mod.app)
    client_b = TestClient(app_mod.app)
    assert client_a.get(f"/profile/auth/exchange?token={tok_a}", follow_redirects=False).status_code == 303
    assert client_b.get(f"/profile/auth/exchange?token={tok_b}", follow_redirects=False).status_code == 303
    csrf_a = re.search(r'data-csrf="([^"]+)"', client_a.get("/profile").text).group(1)
    csrf_b = re.search(r'data-csrf="([^"]+)"', client_b.get("/profile").text).group(1)

    shared_list = "4 Lightning Bolt\n20 Mountain\n"
    different_list = "4 Llanowar Elves\n20 Forest\n"

    # Alice uploads the deck first -> owns the canonical name
    c1 = client_a.post(
        "/v1/decks",
        headers={"X-CSRF-Token": csrf_a},
        json={"name": "Mono-Red Bolt", "format": "standard", "import_text": shared_list, "is_public": True},
    )
    assert c1.status_code == 201
    alice_deck = c1.json()["deck"]
    assert alice_deck["canonical_name"] == "Mono-Red Bolt"
    assert alice_deck["is_canonical_owner"] is True
    assert alice_deck["is_nicknamed"] is False

    # Bob clones Alice's deck and sets a personal nickname
    cl = client_b.post(
        f"/v1/decks/{alice_deck['id']}/clone",
        headers={"X-CSRF-Token": csrf_b},
        json={},
    )
    assert cl.status_code == 201
    bob_deck = cl.json()["deck"]
    assert bob_deck["list_hash"] == alice_deck["list_hash"]
    assert bob_deck["canonical_name"] == "Mono-Red Bolt"
    assert bob_deck["is_canonical_owner"] is False

    # Make Bob's copy public just to verify detail page, while keeping privacy guarantees
    rn_b = client_b.patch(
        f"/v1/decks/{bob_deck['id']}",
        headers={"X-CSRF-Token": csrf_b},
        json={"name": "Bob Speedy Bolt", "is_public": True},
    )
    assert rn_b.status_code == 200
    assert rn_b.json()["deck"]["name"] == "Bob Speedy Bolt"
    assert rn_b.json()["deck"]["canonical_name"] == "Mono-Red Bolt"
    assert rn_b.json()["deck"]["is_canonical_owner"] is False
    assert rn_b.json()["deck"]["is_nicknamed"] is True

    # Canonical owner Alice renames canonical name -> updates canonical_name without clobbering Bob's nickname
    rn_a = client_a.patch(
        f"/v1/decks/{alice_deck['id']}",
        headers={"X-CSRF-Token": csrf_a},
        json={"name": "Classic Red Burn"},
    )
    assert rn_a.status_code == 200
    assert rn_a.json()["deck"]["canonical_name"] == "Classic Red Burn"

    b_after = client_b.get("/v1/decks").json()["decks"][0]
    assert b_after["name"] == "Bob Speedy Bolt"
    assert b_after["canonical_name"] == "Classic Red Burn"

    # Both Alice & Bob play matches using that exact list; Carol plays a different list
    with db.get_db() as conn:
        now = time.time()
        for i in range(6):
            conn.execute(
                "INSERT INTO player_matches (owner, match_id, played_at, result, format_name, format_bucket, deck_identity, deck_name, autopilot) VALUES (?, ?, ?, ?, 'Standard', 'standard', ?, 'Classic Red Burn', 1)",
                (alice, f"ma-{i}", now - i, "win" if i < 4 else "loss", alice_deck["deck_identity"]),
            )
        for i in range(4):
            conn.execute(
                "INSERT INTO player_matches (owner, match_id, played_at, result, format_name, format_bucket, deck_identity, deck_name, autopilot) VALUES (?, ?, ?, ?, 'Standard', 'standard', ?, 'Bob Speedy Bolt', 0)",
                (bob, f"mb-{i}", now - i, "win" if i < 3 else "loss", bob_deck["deck_identity"]),
            )
        # Carol uploads a different list with 10 wins -> must NOT leak into Alice/Bob's community stats
        diff_id = profiles._upsert_deck_record(
            conn,
            carol,
            name="Green Elves",
            fmt="standard",
            piles=profiles.parse_arena_deck_text(different_list),
            is_public=True,
        )
        carol_ident = conn.execute(
            "SELECT deck_identity FROM synced_decks WHERE id = ?", (diff_id,)
        ).fetchone()[0]
        for i in range(10):
            conn.execute(
                "INSERT INTO player_matches (owner, match_id, played_at, result, format_name, format_bucket, deck_identity, deck_name, autopilot) VALUES (?, ?, ?, 'win', 'Standard', 'standard', ?, 'Green Elves', 0)",
                (carol, f"mc-{i}", now - i, carol_ident),
            )
        profiles.refresh_owner_deck_stats(conn, alice)
        profiles.refresh_owner_deck_stats(conn, bob)

    # Combined fleet-wide stats across Alice (4W-2L) + Bob (3W-1L) = 7W-3L (70.0%) across 2 pilots!
    alice_detail = client_a.get(f"/decks/{alice_deck['slug']}").text
    bob_detail = client_b.get(f"/decks/{bob_deck['slug']}").text
    assert "70.0% (7W-3L)" in alice_detail
    assert "2 pilots" in alice_detail
    assert "70.0% (7W-3L)" in bob_detail
    assert "Classic Red Burn" in bob_detail
    assert "Set Nickname" in bob_detail
    assert "Green Elves" not in alice_detail
    assert alice not in bob_detail
    assert bob not in alice_detail
