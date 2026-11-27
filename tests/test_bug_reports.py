"""Tests for Option A Discord #bug-reports submission, member check, dedupe & private storage."""

from __future__ import annotations

import base64
import gzip
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from tests.website_app import load_website_app


def _make_fake_discord(
    *,
    members: list[dict[str, Any]] | None = None,
    active_threads: list[dict[str, Any]] | None = None,
    thread_details: dict[str, dict[str, Any]] | None = None,
):
    posted_calls: list[dict[str, Any]] = []
    members_list = members if members is not None else []
    active_list = active_threads if active_threads is not None else []
    details_map = thread_details if thread_details is not None else {}

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/members/search"):
            q = (request.url.params.get("query") or "").strip().lower()
            matched = [
                m
                for m in members_list
                if q in str((m.get("user") or {}).get("username") or "").lower()
                or q in str((m.get("user") or {}).get("global_name") or "").lower()
                or q in str(m.get("nick") or "").lower()
            ]
            return httpx.Response(200, json=matched)

        if request.method == "GET" and path.endswith("/threads/active"):
            return httpx.Response(200, json={"threads": active_list})

        if request.method == "GET" and "/channels/" in path:
            tid = path.rsplit("/channels/", 1)[-1]
            if tid in details_map:
                return httpx.Response(200, json=details_map[tid])
            return httpx.Response(404, json={"message": "Unknown Channel"})

        if request.method == "POST" and path.endswith("/threads"):
            body_text = request.content.decode("utf-8", errors="replace")
            content_type = request.headers.get("content-type", "")
            posted_calls.append(
                {
                    "kind": "create_thread",
                    "path": path,
                    "content_type": content_type,
                    "body": body_text,
                    "raw": request.content,
                }
            )
            return httpx.Response(201, json={"id": "999888777666555444"})

        if request.method == "POST" and path.endswith("/messages"):
            body_text = request.content.decode("utf-8", errors="replace")
            content_type = request.headers.get("content-type", "")
            posted_calls.append(
                {
                    "kind": "attach_message",
                    "path": path,
                    "content_type": content_type,
                    "body": body_text,
                    "raw": request.content,
                }
            )
            return httpx.Response(200, json={"id": "888777666555444333"})

        return httpx.Response(404, json={"error": f"unmocked {request.method} {path}"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, posted_calls


def _seed_subscriber(db, key: str, email: str = "pilot@example.com") -> None:
    now = time.time()
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO subscribers (email, license_key, name, status, created_at)
            VALUES (?, ?, 'Pilot', 'active', ?)
            """,
            (email, key, now),
        )


def _setup_website(tmp_path: Path, monkeypatch, fake_client: httpx.AsyncClient):
    monkeypatch.setenv("BUG_REPORT_LLM_DEDUPE", "0")
    app_mod, db_mod, _patreon = load_website_app(tmp_path, monkeypatch)
    bug_reports = sys.modules["bug_reports"]
    state = sys.modules["state"]
    match_bundles = sys.modules["match_bundles"]

    monkeypatch.setattr(
        state,
        "config",
        {
            **state.config,
            "discord": {
                "invite_url": "https://discord.gg/mtgacoach-test",
                "bot_token": "bot-token-secret-123",
                "guild_id": "1575648896774832280",
                "bug_reports_channel_id": "1575721869062176818",
                "fixed_tag_id": "1575722345933447169",
                "bug_tags": {
                    "desktop": "1575722013165617302",
                    "autopilot": "1575722327260401735",
                    "coach": "1575722334894035005",
                    "website": "1575722318880309258",
                },
            },
        },
    )
    monkeypatch.setattr(state, "http_client", fake_client)
    monkeypatch.setattr(bug_reports, "_discord_client", fake_client)
    return app_mod, db_mod, state, bug_reports, match_bundles


def test_bug_report_auth_and_discord_member_gate(tmp_path, monkeypatch):
    fake_client, posted = _make_fake_discord(
        members=[
            {"user": {"id": "244670121795125250", "username": "__armour__", "bot": False}, "nick": "ARMOUR"}
        ]
    )
    app_mod, db_mod, _state, _bug_reports, _match_bundles = _setup_website(tmp_path, monkeypatch, fake_client)
    _seed_subscriber(db_mod, "sk-patron-alice")
    client = TestClient(app_mod.app)

    # 1. Requires license key (no anonymous submission)
    r_unauth = client.post(
        "/api/bug-reports",
        json={
            "title": "Crash on mulligan",
            "note": "Crashes every time I keep a 6 card hand.",
            "discord_handle": "__armour__",
        },
    )
    assert r_unauth.status_code == 401

    # 2. Requires Discord handle
    r_no_handle = client.post(
        "/api/bug-reports",
        headers={"Authorization": "Bearer sk-patron-alice"},
        json={"title": "Crash on mulligan", "note": "Crashes every time I keep a 6 card hand."},
    )
    assert r_no_handle.status_code == 422
    assert r_no_handle.json()["detail"]["error"] == "discord_handle_required"
    assert r_no_handle.json()["detail"]["invite_url"] == "https://discord.gg/mtgacoach-test"

    # 3. Rejects users NOT present in MTGA Coach Discord server
    r_outsider = client.post(
        "/api/bug-reports",
        headers={"Authorization": "Bearer sk-patron-alice"},
        json={
            "title": "Crash on mulligan",
            "note": "Crashes every time I keep a 6 card hand.",
            "discord_handle": "not_in_server_user",
        },
    )
    assert r_outsider.status_code == 422
    detail = r_outsider.json()["detail"]
    assert detail["error"] == "not_in_discord_server"
    assert detail["invite_url"] == "https://discord.gg/mtgacoach-test"
    assert posted == []


def test_bug_report_option_a_private_storage_and_dedupe_attach(tmp_path, monkeypatch):
    fake_client, posted = _make_fake_discord(
        members=[
            {"user": {"id": "313243866637664257", "username": "necroxis_", "bot": False}, "nick": None},
            {"user": {"id": "244670121795125250", "username": "__armour__", "bot": False}, "nick": "ARMOUR"},
        ],
        thread_details={
            "999888777666555444": {
                "id": "999888777666555444",
                "parent_id": "1575721869062176818",
                "applied_tags": ["1575722327260401735"],
                "thread_metadata": {"archived": False, "locked": False},
            }
        },
    )
    app_mod, db_mod, state, bug_reports, match_bundles = _setup_website(tmp_path, monkeypatch, fake_client)
    _seed_subscriber(db_mod, "sk-patron-necroxis", "nec@example.com")
    _seed_subscriber(db_mod, "sk-patron-armour", "arm@example.com")
    client = TestClient(app_mod.app)

    secret_sentinel = "INTERNAL_RAW_GRE_GAME_PAYLOAD_DO_NOT_LEAK"
    gz_b64 = base64.b64encode(gzip.compress(json.dumps({"raw_debug": secret_sentinel}).encode())).decode()
    png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32).decode()

    # 1. Create new Forum post for verified member
    r_create = client.post(
        "/api/bug-reports",
        headers={"Authorization": "Bearer sk-patron-necroxis"},
        json={
            "title": "Autopilot skipped attacking with flying creatures",
            "note": "Board had two flyers and opponent had no blockers on turn 4.",
            "category": "autopilot",
            "discord_handle": "@necroxis_",
            "app_version": "0.14.2",
            "platform": "Windows 11",
            "report_gz_b64": gz_b64,
            "screenshots": [{"name": "mtga.png", "png_b64": png_b64}],
        },
    )
    assert r_create.status_code == 201, r_create.text
    res1 = r_create.json()
    assert res1["status"] == "created"
    assert res1["discord_username"] == "necroxis_"
    assert res1["thread_id"] == "999888777666555444"
    assert res1["bug_id"].startswith("BUG-")

    # Raw diagnostics NEVER went to Discord; screenshot & <@user_id> mention DID
    assert len(posted) == 1
    assert posted[0]["kind"] == "create_thread"
    assert secret_sentinel not in posted[0]["body"]
    assert "<@313243866637664257>" in posted[0]["body"]
    assert "1575722327260401735" in posted[0]["body"]
    assert b"PNG" in posted[0]["raw"]

    # Verified handle persisted on player_profiles
    with db_mod.get_db() as conn:
        owner_nec = match_bundles.owner_of("sk-patron-necroxis")
        prof = conn.execute(
            "SELECT discord_handle FROM player_profiles WHERE owner = ?", (owner_nec,)
        ).fetchone()
        assert prof["discord_handle"] == "necroxis_"

    # Private diagnostics downloadable via admin endpoint (and blocked without admin auth)
    r_admin_unauth = client.get(f"/admin/api/bug-reports/{res1['bug_id']}/download")
    assert r_admin_unauth.status_code in (401, 403)
    r_admin = client.get(
        f"/admin/api/bug-reports/{res1['bug_id']}/download",
        headers={"X-Admin-Key": state.admin_password},
    )
    assert r_admin.status_code == 200
    assert json.loads(gzip.decompress(r_admin.content))["raw_debug"] == secret_sentinel

    # 2. Second user submits a similar bug -> backend surfaces possible duplicate instead of creating new thread
    r_dup = client.post(
        "/api/bug-reports",
        headers={"Authorization": "Bearer sk-patron-armour"},
        json={
            "title": "Autopilot skipped attacking with flying creatures",
            "note": "Happened on turn 5 too when opponent had zero flying blockers.",
            "category": "autopilot",
            "discord_handle": "ARMOUR",
        },
    )
    assert r_dup.status_code == 200, r_dup.text
    dup_body = r_dup.json()
    assert dup_body["status"] == "possible_duplicates"
    assert dup_body["matches"][0]["thread_id"] == "999888777666555444"
    assert len(posted) == 1  # No new Discord message yet

    # 3. Second user chooses to attach to existing thread
    r_attach = client.post(
        "/api/bug-reports",
        headers={"Authorization": "Bearer sk-patron-armour"},
        json={
            "title": "Autopilot skipped attacking with flying creatures",
            "note": "Happened on turn 5 too when opponent had zero flying blockers.",
            "category": "autopilot",
            "discord_handle": "__armour__",
            "existing_thread_id": "999888777666555444",
        },
    )
    assert r_attach.status_code == 201, r_attach.text
    res2 = r_attach.json()
    assert res2["status"] == "attached"
    assert res2["thread_id"] == "999888777666555444"
    assert len(posted) == 2
    assert posted[1]["kind"] == "attach_message"
    assert "<@244670121795125250>" in posted[1]["body"]

    # 4. Submitting immediately again triggers per-owner cooldown (429)
    r_rate = client.post(
        "/api/bug-reports",
        headers={"Authorization": "Bearer sk-patron-armour"},
        json={
            "title": "Another separate UI issue",
            "note": "Window flickered cleanly when toggling overlays.",
            "category": "desktop",
            "force_new": True,
        },
    )
    assert r_rate.status_code == 429
    assert "Retry-After" in r_rate.headers


def test_client_bug_report_upload_redacts_secrets_and_parses_outcomes(tmp_path):
    from arenamcp.bug_report_upload import submit_bug_report

    shot = tmp_path / "mtga.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"abcd" * 8)

    captured: list[dict[str, Any]] = []

    def fake_opener(url: str, method: str, data: bytes | None, headers: dict[str, str], timeout: float):
        captured.append({"url": url, "method": method, "headers": headers, "body": json.loads(data.decode())})
        return 201, json.dumps(
            {
                "status": "created",
                "bug_id": "BUG-1234ABCD",
                "discord_username": "__armour__",
                "thread_id": "1575999",
                "thread_url": "https://discord.com/channels/1/1575999",
            }
        )

    res = submit_bug_report(
        license_key="sk-secret-key-999",
        title="Voice audio stopped",
        note="Voice stopped after alt-tabbing back to Arena.",
        category="coach",
        discord_handle="__armour__",
        report_data={
            "settings": {"license_key": "sk-secret-key-999"},
            "log": "C:\\Users\\joshu\\secret\\Player.log player@example.com",
        },
        screenshot_paths=[("mtga", shot)],
        include_screenshots=True,
        base_url="https://mtgacoach.com",
        opener=fake_opener,
    )
    assert res["ok"] is True
    assert res["status"] == "created"
    assert res["bug_id"] == "BUG-1234ABCD"
    assert captured[0]["url"] == "https://mtgacoach.com/api/bug-reports"
    assert len(captured[0]["body"]["screenshots"]) == 1

    # Verify license key, email, and Windows user path were scrubbed before leaving the client
    decompressed = gzip.decompress(base64.b64decode(captured[0]["body"]["report_gz_b64"])).decode()
    assert "sk-secret-key-999" not in decompressed
    assert "player@example.com" not in decompressed
    assert "joshu" not in decompressed

    # Verify FastAPI HTTP 422 non-member payload parsing
    def non_member_opener(url: str, method: str, data: bytes | None, headers: dict[str, str], timeout: float):
        return 422, json.dumps(
            {
                "detail": {
                    "error": "not_in_discord_server",
                    "message": "Join Discord first.",
                    "invite_url": "https://discord.gg/mtgacoach",
                }
            }
        )

    err_res = submit_bug_report(
        license_key="sk-secret-key-999",
        title="Voice audio stopped",
        note="Voice stopped after alt-tabbing back to Arena.",
        discord_handle="outsider",
        base_url="https://mtgacoach.com",
        opener=non_member_opener,
    )
    assert err_res["ok"] is False
    assert err_res["error_code"] == "not_in_discord_server"
    assert err_res["invite_url"] == "https://discord.gg/mtgacoach"


def test_discord_bug_report_dialog_duplicate_and_non_member_flows(qapp, tmp_path, monkeypatch):
    from arenamcp.desktop.discord_bug_dialog import DiscordBugReportDialog
    from arenamcp.settings import Settings

    monkeypatch.setattr("arenamcp.settings.SETTINGS_DIR", tmp_path)
    monkeypatch.setattr("arenamcp.settings.SETTINGS_FILE", tmp_path / "settings.json")
    fake_settings = Settings()
    fake_settings.set("license_key", "sk-test-123")
    fake_settings.set("discord_handle", "__armour__")
    monkeypatch.setattr("arenamcp.desktop.discord_bug_dialog.get_settings", lambda: fake_settings)

    report_file = tmp_path / "bug_test.json"
    report_file.write_text(json.dumps({"reason": "Test"}), encoding="utf-8")

    submissions: list[dict[str, Any]] = []

    def fake_submit(**kwargs):
        submissions.append(kwargs)
        if not kwargs.get("existing_thread_id") and not kwargs.get("force_new"):
            return {
                "ok": True,
                "status": "possible_duplicates",
                "discord_username": "__armour__",
                "matches": [
                    {
                        "thread_id": "555111222333444555",
                        "title": "Stuck on sideboard timer",
                        "url": "https://discord.com/channels/1/555111222333444555",
                        "report_count": 2,
                        "reason": "Same sideboard issue",
                    }
                ],
            }
        return {
            "ok": True,
            "status": "attached",
            "bug_id": "BUG-AA11BB22",
            "discord_username": "__armour__",
            "thread_id": kwargs["existing_thread_id"],
            "thread_url": f"https://discord.com/channels/1/{kwargs['existing_thread_id']}",
        }

    dlg = DiscordBugReportDialog(
        report_path=report_file,
        submit_fn=fake_submit,
        prefill_fn=lambda **_kw: {"discord_handle": "__armour__", "invite_url": ""},
    )
    dlg.title_edit.setText("Sideboard timer freezes")
    dlg.note_edit.setPlainText("Game 2 sideboard screen freezes for 30 seconds.")

    # Bypass worker thread async wait cleanly by testing response handler + attach action
    dlg._on_submit_finished(
        fake_submit(
            license_key="sk-test-123",
            title="Sideboard timer freezes",
            note="Game 2 sideboard screen freezes for 30 seconds.",
            existing_thread_id="",
            force_new=False,
        )
    )
    assert not dlg.dup_frame.isHidden()
    assert len(dlg.dup_group.buttons()) == 1

    # Simulate attaching to selected duplicate
    dlg._on_submit_finished(
        fake_submit(
            license_key="sk-test-123",
            title="Sideboard timer freezes",
            note="Game 2 sideboard screen freezes for 30 seconds.",
            existing_thread_id="555111222333444555",
            force_new=False,
        )
    )
    assert dlg.result_thread_url == "https://discord.com/channels/1/555111222333444555"
    assert "BUG-AA11BB22" in dlg.status_label.text()

    # Non-member error surfaces Join Discord button
    dlg._on_submit_finished(
        {
            "ok": False,
            "error_code": "not_in_discord_server",
            "message": "Please join MTGA Coach Discord first.",
            "invite_url": "https://discord.gg/invite-test",
        }
    )
    assert not dlg.status_frame.isHidden()
    assert not dlg.invite_btn.isHidden()
    assert dlg._invite_url == "https://discord.gg/invite-test"
    dlg.close()

    # Closing dialog ignores any late background callback without mutating settings
    assert dlg._closed is True
    dlg._on_submit_finished(
        {
            "ok": True,
            "status": "created",
            "bug_id": "BUG-LATE9999",
            "discord_username": "late_overwrite_attempt",
            "thread_url": "https://discord.com/channels/1/999",
        }
    )
    assert fake_settings.get("discord_handle") == "__armour__"


def test_fixed_discord_threads_excluded_from_duplicates(tmp_path, monkeypatch):
    fake_client, posted = _make_fake_discord(
        members=[
            {"user": {"id": "244670121795125250", "username": "__armour__", "bot": False}, "nick": "ARMOUR"}
        ],
        active_threads=[
            {
                "id": "111222333444555666",
                "parent_id": "1575721869062176818",
                "name": "[BUG-OLD12345] Crash when opening deck",
                "applied_tags": ["1575722345933447169"],  # Fixed tag
                "thread_metadata": {"archived": False, "locked": False},
            }
        ],
    )
    app_mod, db_mod, _state, _bug_reports, _match_bundles = _setup_website(tmp_path, monkeypatch, fake_client)
    _seed_subscriber(db_mod, "sk-patron-armour", "arm@example.com")
    with db_mod.get_db() as conn:
        conn.execute(
            """
            INSERT INTO bug_reports (
                bug_id, owner, discord_user_id, discord_username, category, title, note,
                discord_thread_id, discord_thread_url, status, created_at
            ) VALUES ('BUG-OLD12345', 'other', '1', 'u', 'desktop', 'Crash when opening deck',
                      'Crash on click', '111222333444555666',
                      'https://discord.com/channels/1575648896774832280/111222333444555666',
                      'open', ?)
            """,
            (time.time() - 3600,),
        )

    client = TestClient(app_mod.app)
    r = client.post(
        "/api/bug-reports",
        headers={"Authorization": "Bearer sk-patron-armour"},
        json={
            "title": "Crash when opening deck",
            "note": "Still seeing a crash when opening my deck in the app.",
            "category": "desktop",
            "discord_handle": "__armour__",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "created"
    assert len(posted) == 1
    with db_mod.get_db() as conn:
        row = conn.execute("SELECT status FROM bug_reports WHERE bug_id = 'BUG-OLD12345'").fetchone()
        assert row["status"] == "closed"
