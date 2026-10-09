"""Match bundles: build, redact, cap, save, upload — and the setting that gates them."""

from __future__ import annotations

import gzip
import json
import logging
import os
import time
from pathlib import Path

import pytest

from arenamcp import match_bundle as mb
from arenamcp.pipe_adapter import PipeAdapter

ACCOUNT = "T3MARZNUZRD4RDKHEA6GNFXM3M"
OPP_ACCOUNT = "Q7XKPLMNBVCXZASDFGHJKLQWER"
LOCAL_NAME = "JoshArmour#12345"
OPP_NAME = "SneakyOpponent#67890"
MATCH_ID = "c0c20b9e-76e6-429b-8a01-8d0951b2bba0"
OLD_MATCH_ID = "11111111-2222-3333-4444-555555555555"
LICENSE = "sk-abcdefghijklmnop1234567890"
EMAIL = "someone@example.com"


def _room_event(match_id: str, state: str) -> str:
    return json.dumps(
        {
            "transactionId": "17ea0f47",
            "matchGameRoomStateChangedEvent": {
                "gameRoomInfo": {
                    "gameRoomConfig": {
                        "reservedPlayers": [
                            {
                                "userId": ACCOUNT,
                                "playerName": LOCAL_NAME,
                                "systemSeatId": 1,
                                "sessionId": "sess-aaaa-bbbb-cccc",
                                "platformId": "SteamMac",
                            },
                            {
                                "userId": OPP_ACCOUNT,
                                "playerName": OPP_NAME,
                                "systemSeatId": 2,
                                "sessionId": "sess-dddd-eeee-ffff",
                                "platformId": "SteamWindows",
                            },
                        ],
                        "matchId": match_id,
                    },
                    "stateType": state,
                    "players": [
                        {"userId": ACCOUNT, "playerName": LOCAL_NAME, "systemSeatId": 1},
                        {"userId": OPP_ACCOUNT, "playerName": OPP_NAME, "systemSeatId": 2},
                    ],
                }
            },
        }
    )


def _gre(n: int, extra: str = "") -> str:
    return json.dumps(
        {
            "transactionId": f"tx-{n}",
            "greToClientEvent": {
                "greToClientMessages": [
                    {
                        "type": "GREMessageType_GameStateMessage",
                        "gameStateId": n,
                        "text": "cost {T}: add " + extra,
                    }
                ]
            },
        }
    )


def _player_log(tmp_path: Path) -> tuple[Path, int]:
    """A Player.log with an earlier match, noise, and the match under test. Returns (path, watcher offset)."""
    hdr = "[UnityCrossThreadLogger]10/7/2026 6:02:20 PM: "
    old = [
        f"{hdr}Match to {ACCOUNT}: MatchGameRoomStateChangedEvent",
        _room_event(OLD_MATCH_ID, "MatchGameRoomStateType_Playing"),
        f"{hdr}Match to {ACCOUNT}: GreToClientEvent",
        _gre(1, "old-match"),
        f"{hdr}Match to {ACCOUNT}: MatchGameRoomStateChangedEvent",
        _room_event(OLD_MATCH_ID, "MatchGameRoomStateType_MatchCompleted"),
        f"[Accounts - Login] Logged in successfully. Display Name: {LOCAL_NAME}",
        f"[UnityCrossThreadLogger]Loading from file: /Users/joshu/Library/Application Support/x {EMAIL}",
        "<== EventGetCoursesV2(123)",
        json.dumps({"Courses": [{"CourseId": "secret", "token": "ghp_" + "a" * 30}]}),
    ]
    start_room = [
        f"{hdr}Match to {ACCOUNT}: MatchGameRoomStateChangedEvent",
        _room_event(MATCH_ID, "MatchGameRoomStateType_Playing"),
    ]
    rest = [
        f"{hdr}Match to {ACCOUNT}: GreToClientEvent",
        _gre(2, "second"),
        f"{hdr}{ACCOUNT} to Match: ClientToGremessage",
        "{",
        '  "requestId": 3,',
        '  "clientToMatchServiceMessageType": "ClientToMatchServiceMessageType_ClientToGREMessage",',
        '  "payload": {',
        '    "type": "ClientMessageType_PerformActionResp",',
        '    "performActionResp": {"actions": [{"actionType": "ActionType_Play", "instanceId": 42}]}',
        "  }",
        "}",
        f"{hdr}Match to {ACCOUNT}: GreToClientEvent",
        _gre(3, "third"),
        f"{hdr}Match to {ACCOUNT}: MatchGameRoomStateChangedEvent",
        _room_event(MATCH_ID, "MatchGameRoomStateType_MatchCompleted"),
        f"{hdr}Match to {ACCOUNT}: GreToClientEvent",
        _gre(4, "after-the-match"),
    ]
    path = tmp_path / "Player.log"
    text_before = "\n".join(old + start_room) + "\n"
    path.write_text(text_before + "\n".join(rest) + "\n", encoding="utf-8")
    # The watcher has consumed the room event by the time the snapshot shows the match id.
    return path, len(text_before.encode("utf-8"))


def _coach_log_before(tmp_path: Path) -> Path:
    path = tmp_path / "standalone.log"
    path.write_text(
        "2026-10-07 18:00:00 | INFO     | arenamcp.standalone | previous match line\n", encoding="utf-8"
    )
    return path


def _coach_log_during(path: Path) -> None:
    during = (
        "2026-10-07 18:02:20 | INFO     | arenamcp.standalone | Started match packet recording\n"
        "2026-10-07 18:02:21 | DEBUG    | arenamcp.gamestate | noisy debug\n"
        "   debug continuation line\n"
        f"2026-10-07 18:02:22 | INFO     | arenamcp.match_history | Recorded match: win vs {OPP_NAME}\n"
        f"2026-10-07 18:02:23 | ERROR    | arenamcp.backends.proxy | 401 key={LICENSE} from /Users/joshu/x\n"
        "Traceback (most recent call last):\n"
        '  File "/Users/joshu/repos/mtgacoach/src/arenamcp/coach.py", line 1, in x\n'
        "RuntimeError: boom\n"
        "2026-10-07 18:02:24 | INFO     | arenamcp.autopilot | [AUTOPILOT] MANUAL REQUIRED: Cast X\n"
    )
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(during)


def _packet(tmp_path: Path) -> Path:
    data = {
        "match_id": MATCH_ID,
        "start_time": "2026-10-07T18:02:20",
        "end_time": "2026-10-07T18:07:31",
        "result": "loss",
        "deck_strategy": "Aggro: curve out and attack.",
        "opponent_name": OPP_NAME,
        "replay_path": "/Users/joshu/Library/Application Support/MTGA/replays/match.rply",
        "decisions": [
            {
                "pending_decision": {
                    "request_id": [2, 9],
                    "request_type": "Mulligan",
                    "options": [
                        {"option_id": "mull:keep", "label": "Keep this hand", "payable": None, "meta": {}}
                    ],
                    "min_select": 1,
                    "max_select": 1,
                    "can_pass": False,
                    "can_cancel": False,
                    "source_label": "",
                    "min_weight": None,
                    "max_weight": None,
                    "slots": [],
                },
                "chosen_options": ["mull:keep"],
                "outcome": "executed",
                "timestamp": 1791092497.3,
            },
            {
                "executed_action": {
                    "action_type": "play_land",
                    "card_name": "Island",
                    "detail": "",
                    "turn": 1,
                },
                "outcome": "executed",
                "timestamp": 1791092500.0,
            },
        ],
    }
    path = tmp_path / f"packet_20261007_180731_{MATCH_ID}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _bug_report(tmp_path: Path, name: str = "bug_20261007_180300.json") -> Path:
    bug_dir = tmp_path / "bug_reports"
    bug_dir.mkdir(exist_ok=True)
    report = {
        "reason": "User Request",
        "reporter": {"install_id": "inst_" + "a" * 32},
        "settings": {
            "license_key": LICENSE,
            "local_api_key": "vllm",
            "autopilot_vision_api_key": "sk-visionkey12345678",
            "tts_server_url": "http://10.0.0.5:8880",
            "mtga_install_dir": "C:\\Users\\joshu\\MTGA",
            "autopilot_enabled": True,
            "install_id": "inst_" + "a" * 32,
            "model": "deepseek-v4-flash",
        },
        "system": {"platform": "macOS-15", "python_version": "3.11.9", "machine": "arm64"},
        "mtga_log": {"path": "/Users/joshu/Library/Logs/Wizards Of The Coast/MTGA/Player.log"},
        "screenshots": {"coach": str(bug_dir / "bug_coach.png"), "mtga": str(bug_dir / "bug_mtga.png")},
        "game_state": {"opponent_name": OPP_NAME, "match_id": MATCH_ID, "players": [{"seat_id": 1}]},
        "recent_logs": [f"2026-10-07 18:02:22 | INFO | x | Recorded match: win vs {OPP_NAME}"],
        "llm_context": {"system_prompt": "You are a coach.", "formatted_game_state": "Board: Island"},
        "autopilot": {"engine": "native", "enabled": True},
    }
    path = bug_dir / name
    path.write_text(json.dumps(report), encoding="utf-8")
    (bug_dir / "bug_coach.png").write_bytes(b"\x89PNG")
    return path


def _final_state() -> dict:
    return {
        "match_id": MATCH_ID,
        "opponent_name": OPP_NAME,
        "local_seat_id": 1,
        "players": [{"seat_id": 1, "life_total": 3, "is_local": True}, {"seat_id": 2, "life_total": 20}],
        "turn": {"turn_number": 9},
        "raw_gre_events": [{"big": "x" * 10}],
    }


class _Settings(dict):
    def get(self, key, default=None):  # mirror arenamcp.settings.Settings.get
        return super().get(key, default)

    def set(self, key, value, save=True):
        self[key] = value


def _settings(**overrides) -> _Settings:
    base = {
        "share_match_logs": True,
        "license_key": LICENSE,
        "install_id": "inst_" + "a" * 32,
        "autopilot_enabled": True,
        "model": None,
        "game_device": "desktop",
    }
    base.update(overrides)
    return _Settings(base)


@pytest.fixture
def world(tmp_path, monkeypatch):
    player_log, player_offset = _player_log(tmp_path)
    coach_log = _coach_log_before(tmp_path)
    packet_path = _packet(tmp_path)
    bug_path = _bug_report(tmp_path)
    ctx = mb.begin_match(
        MATCH_ID, coach_log_path=coach_log, player_log_path=player_log, player_log_offset=player_offset
    )
    assert ctx.coach_log_start == coach_log.stat().st_size
    _coach_log_during(coach_log)
    ctx.started_at = time.time() - 60
    monkeypatch.setattr(mb, "_bundled_match_ids", set())
    return {
        "ctx": ctx,
        "packet_path": packet_path,
        "bug_dir": tmp_path / "bug_reports",
        "bug_path": bug_path,
        "bundle_dir": tmp_path / "match_bundles",
        "player_log": player_log,
    }


def _build(world, **kw) -> dict:
    args = dict(
        install_id="inst_" + "a" * 32,
        result="loss",
        final_state=_final_state(),
        packet_path=world["packet_path"],
        advice_history=[{"turn": 1, "advice": f"Attack {OPP_NAME}'s face; key {LICENSE}"}],
        settings=_settings(),
        config={"served_model": "deepseek-v4-flash", "draft_mode": False},
        bug_dir=world["bug_dir"],
    )
    args.update(kw)
    return mb.build_bundle(world["ctx"], **args)


# --- slicing -------------------------------------------------------------------


def test_player_log_slice_keeps_only_this_matchs_traffic(world):
    ctx = world["ctx"]
    events, info = mb.slice_player_log(world["player_log"], ctx.player_log_start, None, MATCH_ID)
    kinds = [(e["direction"], e["kind"]) for e in events]
    assert kinds == [
        ("in", "MatchGameRoomStateChangedEvent"),
        ("in", "GreToClientEvent"),
        ("out", "ClientToGremessage"),
        ("in", "GreToClientEvent"),
        ("in", "MatchGameRoomStateChangedEvent"),
    ]
    assert info == {"start_marker_found": True, "end_marker_found": True}
    assert MATCH_ID in events[0]["json"]
    assert "old-match" not in json.dumps(events) and "after-the-match" not in json.dumps(events)
    # multi-line outgoing block reassembled into valid JSON
    out = json.loads(events[2]["json"])
    assert out["payload"]["performActionResp"]["actions"][0]["instanceId"] == 42
    assert events[1]["header_ts"] == "10/7/2026 6:02:20 PM"


def test_player_log_slice_without_a_start_marker_keeps_the_window(tmp_path):
    path = tmp_path / "Player.log"
    path.write_text(
        "[UnityCrossThreadLogger]10/7/2026 6:02:20 PM: Match to ABC: GreToClientEvent\n" + _gre(9) + "\n"
    )
    events, info = mb.slice_player_log(path, 0, None, "no-such-match")
    assert len(events) == 1 and info["start_marker_found"] is False and info["end_marker_found"] is False
    assert mb.slice_player_log(tmp_path / "missing.log", 0, None, MATCH_ID) == (
        [],
        {
            "start_marker_found": False,
            "end_marker_found": False,
        },
    )


def test_coach_log_slice_drops_debug_and_keeps_tracebacks(world):
    ctx = world["ctx"]
    lines = mb.slice_coach_log(ctx.coach_log_path, ctx.coach_log_start)
    assert lines[0].endswith("Started match packet recording")
    assert not any("DEBUG" in ln or "debug continuation" in ln for ln in lines)
    assert "Traceback (most recent call last):" in lines and "RuntimeError: boom" in lines
    assert "previous match line" not in "\n".join(lines)
    assert mb.slice_coach_log(None, 0) == []


# --- redaction -----------------------------------------------------------------


def _assert_clean(text: str) -> None:
    for secret in (
        LICENSE,
        "sk-visionkey",
        "ghp_",
        EMAIL,
        LOCAL_NAME,
        OPP_NAME,
        ACCOUNT,
        OPP_ACCOUNT,
        "sess-aaaa",
    ):
        assert secret not in text, secret
    assert "/Users/joshu" not in text and "Users\\\\joshu" not in text and "joshu" not in text


def test_bundle_is_fully_redacted(world):
    bundle = _build(world)
    text = json.dumps(bundle)
    _assert_clean(text)
    assert bundle["install_id"] == "inst_" + "a" * 32  # the one identifier that stays
    assert bundle["schema"] == mb.SCHEMA_VERSION and bundle["match_id"] == MATCH_ID
    # names -> seat labels, account ids -> [account], recorded-match line -> opponent
    room = json.loads(bundle["player_log"]["events"][0]["json"])
    players = room["matchGameRoomStateChangedEvent"]["gameRoomInfo"]["gameRoomConfig"]["reservedPlayers"]
    assert [p["playerName"] for p in players] == ["[playerName]", "[playerName]"]
    assert [p["userId"] for p in players] == ["[userId]", "[userId]"]
    assert bundle["final_state"]["opponent_name"] == "opponent"
    assert bundle["packet"]["opponent_name"] == "opponent"
    assert bundle["packet"]["replay_path"] == "match.rply"
    assert any("Recorded match: win vs opponent" in ln for ln in bundle["coach_log"]["lines"])
    assert any("[redacted]" in ln and "~/x" in ln for ln in bundle["coach_log"]["lines"])
    assert "opponent's face" in bundle["advice_history"][0]["advice"]
    # settings: whitelisted subset only, nowhere the raw dict
    assert bundle["settings_subset"]["autopilot_enabled"] is True
    assert "license_key" not in bundle["settings_subset"] and "install_id" not in bundle["settings_subset"]
    report = bundle["bug_reports"][0]["report"]
    assert "settings" not in report and "screenshots" not in report and "mtga_log" not in report
    assert report["settings_subset"] == {
        k: v for k, v in report["settings_subset"].items() if k in mb.SETTINGS_WHITELIST
    }
    assert report["settings_subset"]["model"] == "deepseek-v4-flash"
    assert report["reporter"]["install_id"] == "inst_" + "a" * 32
    assert bundle["bug_reports"][0]["file"] == world["bug_path"].name
    # the shipped match_review detectors are the post-match path's job (and
    # the offline reviewer's); running them here double-logged calibration
    assert "match_review_findings" not in bundle
    assert bundle["arena_match_id"] == MATCH_ID and bundle["game_number"] == 1
    assert bundle["config"]["served_model"] == "deepseek-v4-flash"


def test_bundle_never_touches_the_calibration_log(world, monkeypatch, tmp_path):
    from arenamcp import match_review

    calibration = tmp_path / "calib" / "win_prob_calibration.jsonl"
    monkeypatch.setattr(match_review, "CALIBRATION_LOG", calibration)
    with open(world["ctx"].coach_log_path, "a", encoding="utf-8") as fh:
        fh.write("2026-10-07 18:02:25 | INFO     | arenamcp.coach_analysis | [WIN-PROB] WIN: 85%\n")
    bundle = _build(world)
    assert any("[WIN-PROB] WIN: 85%" in ln for ln in bundle["coach_log"]["lines"])
    assert not calibration.exists()


@pytest.mark.parametrize("name", ["hand", "null", "2024", "true", "turn"])
def test_redact_never_rewrites_structure_for_names_that_look_like_json(name):
    doc = {
        "final_state": {"hand": [1], "turn": {"turn_number": 2024}, "flag": True, "none": None},
        "advice": f"{name} attacked; 2024 damage to null and true",
        "ev": '{"playerName":"' + name + '","turn":2024,"hand":[null,true],"opp":"' + name + '"}',
        "quoted": f'opponent "{name}" conceded',
    }
    out = mb.redact(doc, {name: "opponent"})
    # Never the document's own keys / non-strings...
    assert out["final_state"] == {"hand": [1], "turn": {"turn_number": 2024}, "flag": True, "none": None}
    # ...nor the keys or literals of the raw Player.log JSON kept inside strings.
    ev = json.loads(out["ev"])
    assert ev == {"playerName": "[playerName]", "turn": 2024, "hand": [None, True], "opp": "opponent"}
    assert out["quoted"] == 'opponent "opponent" conceded'
    if name in ("hand", "turn"):  # ordinary words are replaced in free text
        assert out["advice"].startswith("opponent attacked")
    else:  # a number or JSON literal is not identifying as a bare word
        assert out["advice"] == doc["advice"]


def test_redact_keeps_token_counts_but_not_token_strings():
    out = mb.redact(
        {
            "usage": {"prompt_tokens": 6100, "completion_tokens": 240, "total_tokens": 6340},
            "access_token": "ghp_" + "b" * 30,
            "id_token": "abc.def",
            "tokens": "raw-credential",
            "max_tokens": 512,
        }
    )
    assert out["usage"] == {"prompt_tokens": 6100, "completion_tokens": 240, "total_tokens": 6340}
    assert out["max_tokens"] == 512
    assert out["access_token"] == out["id_token"] == out["tokens"] == "[redacted]"


def test_redact_scrubs_keys_tokens_emails_and_paths():
    names = {"Evil Opponent#999": "opponent", "Jo": "you"}
    out = mb.redact(
        {
            "license_key": LICENSE,
            "autopilot_vision_api_key": "sk-visionkey12345678",
            "tts_server_url": "http://x",
            "access_token": "ghp_" + "b" * 30,
            "token_count": 12,
            "install_id": "inst_keep",
            "note": f"mail {EMAIL} path C:\\Users\\joshu\\x and /home/joshu/y Bearer mc_abcdefgh12 by Evil Opponent#999",
            "header": f"Match to {ACCOUNT}: GreToClientEvent / {ACCOUNT} to Match: ClientToGremessage",
            "short": "Jo played Island",
            "blob": "x" * (mb._BLOB_LIMIT + 1),
            "empty_key": "",
        },
        names,
    )
    assert out["license_key"] == "[redacted]" and out["autopilot_vision_api_key"] == "[redacted]"
    assert out["tts_server_url"] == "[redacted]" and out["access_token"] == "[redacted]"
    assert out["token_count"] == 12 and out["install_id"] == "inst_keep" and out["empty_key"] == ""
    assert out["note"] == "mail [redacted] path ~\\x and ~/y Bearer [redacted] by opponent"
    assert out["header"] == "Match to [account]: GreToClientEvent / [account] to Match: ClientToGremessage"
    assert out["short"] == "Jo played Island"  # names under 4 chars are never literal-replaced
    assert out["blob"].startswith("[dropped ")


def test_settings_subset_accepts_settings_objects_and_dicts():
    assert mb.settings_subset(
        {"license_key": "x", "autopilot_enabled": False, "model": None, "voice_mode": "ptt"}
    ) == {
        "autopilot_enabled": False,
        "model": None,
        "voice_mode": "ptt",
    }

    class S:
        def get(self, key, default=None):
            return {"language": "en", "license_key": "sk-1234567890ab"}.get(key)

    assert mb.settings_subset(S()) == {k: ("en" if k == "language" else None) for k in mb.SETTINGS_WHITELIST}


# --- caps ----------------------------------------------------------------------


def test_log_sections_truncate_oldest_first_with_a_marker(world, monkeypatch):
    monkeypatch.setattr(mb, "COACH_LOG_CAP", 260)
    monkeypatch.setattr(mb, "PLAYER_LOG_CAP", 1500)
    bundle = _build(world)
    coach = bundle["coach_log"]
    assert (
        coach["truncated"] > 0
        and coach["lines"][0] == f"[BUNDLE TRUNCATED: dropped {coach['truncated']} older lines]"
    )
    assert coach["lines"][-1].endswith("MANUAL REQUIRED: Cast X")  # newest kept
    plog = bundle["player_log"]
    assert plog["truncated"] > 0 and plog["start_marker_found"] is False and plog["end_marker_found"] is True
    assert "after-the-match" not in json.dumps(plog["events"])


def test_final_state_and_bug_reports_are_capped(world, monkeypatch):
    monkeypatch.setattr(mb, "FINAL_STATE_CAP", 150)
    monkeypatch.setattr(mb, "BUG_REPORT_CAP", 400)
    bundle = _build(world, final_state={**_final_state(), "raw_gre_events": [{"big": "y" * 400}]})
    assert bundle["final_state"]["truncated"] is True and "raw_gre_events" not in bundle["final_state"]
    report = bundle["bug_reports"][0]["report"]
    assert report["dropped_fields"] and "reason" in report


def test_encode_shrinks_until_under_the_gzip_cap():
    bundle = {
        "coach_log": {"lines": [f"line {i} {os.urandom(40).hex()}" for i in range(400)], "truncated": 0},
        "player_log": {
            "events": [{"json": os.urandom(60).hex()} for _ in range(400)],
            "truncated": 0,
            "start_marker_found": True,
        },
    }
    data = mb.encode_bundle(bundle, gzip_cap=12_000)
    assert len(data) <= 12_000
    out = json.loads(gzip.decompress(data))
    assert out["coach_log"]["truncated"] > 0 and out["coach_log"]["lines"][0].startswith("[BUNDLE TRUNCATED")
    assert out["player_log"]["truncated"] > 0 and out["player_log"]["start_marker_found"] is False
    # an unshrinkable bundle still encodes (returns the best it can)
    assert mb.encode_bundle(
        {"coach_log": {"lines": []}, "player_log": {"events": []}, "x": "y" * 5000}, gzip_cap=10
    )


def test_bug_reports_only_from_this_match_and_at_most_five(tmp_path):
    bug_dir = tmp_path / "bug_reports"
    for i in range(8):
        _bug_report(tmp_path, f"bug_2026100718030{i}.json")
    old = _bug_report(tmp_path, "bug_old.json")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    found = mb.collect_bug_reports(bug_dir, time.time() - 60)
    assert len(found) == mb.MAX_BUG_REPORTS and old not in found
    assert mb.collect_bug_reports(tmp_path / "nope", 0) == []


# --- save / rotate -------------------------------------------------------------


def test_save_bundle_rotates_oldest(tmp_path):
    bundle_dir = tmp_path / "bundles"
    paths = []
    for i in range(4):
        p = mb.save_bundle(b"data", f"m-{i}", bundle_dir, max_bundles=3)
        os.utime(p, (time.time() - 100 + i, time.time() - 100 + i))
        paths.append(p)
    assert not paths[0].exists() and all(p.exists() for p in paths[1:])
    assert mb.save_bundle(b"x", "../../evil id", bundle_dir, max_bundles=3).name == "______evil_id.json.gz"


# --- upload --------------------------------------------------------------------


class _Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, bytes, dict]] = []

    def __call__(self, url, data, headers, timeout):
        self.calls.append((url, data, dict(headers)))
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def test_upload_retries_then_succeeds(caplog):
    opener = _Opener(OSError("conn refused"), (503, "busy"), (200, "ok"))
    delays: list[float] = []
    with caplog.at_level(logging.INFO, logger="arenamcp.match_bundle"):
        ok = mb.upload_bundle(
            b"gz",
            match_id=MATCH_ID,
            license_key=LICENSE,
            install_id="inst_x",
            base_url="https://example.test/",
            opener=opener,
            sleep=delays.append,
        )
    assert ok and delays == [5.0, 30.0]
    url, data, headers = opener.calls[-1]
    assert url == "https://example.test/api/match-bundle" and data == b"gz"
    assert headers["Authorization"] == f"Bearer {LICENSE}" and headers["Content-Type"] == "application/gzip"
    assert headers["X-MTGACoach-Install-ID"] == "inst_x" and headers["X-MTGACoach-Match-ID"] == MATCH_ID
    assert headers["User-Agent"].startswith("mtgacoach/")
    assert f"[BUNDLE] uploaded {MATCH_ID} (HTTP 200" in caplog.text
    assert LICENSE not in caplog.text


def test_upload_gives_up_on_client_errors_and_after_retries(caplog):
    delays: list[float] = []
    with caplog.at_level(logging.INFO, logger="arenamcp.match_bundle"):
        assert not mb.upload_bundle(
            b"gz",
            match_id="m",
            license_key="k",
            install_id="i",
            base_url="https://e.test",
            opener=_Opener((401, "no")),
            sleep=delays.append,
        )
        assert delays == []
        assert mb.upload_bundle(
            b"gz",
            match_id="m",
            license_key="k",
            install_id="i",
            base_url="https://e.test",
            opener=_Opener((409, "duplicate")),
            sleep=delays.append,
        )
        opener = _Opener(*(OSError("down"),) * 4)
        assert not mb.upload_bundle(
            b"gz",
            match_id="m",
            license_key="k",
            install_id="i",
            base_url="https://e.test",
            opener=opener,
            sleep=delays.append,
        )
    assert delays == [5.0, 30.0, 120.0] and len(opener.calls) == 4
    assert (
        "[BUNDLE] upload failed (HTTP 401)" in caplog.text
        and "[BUNDLE] upload failed (OSError" in caplog.text
    )


def test_upload_stops_when_sharing_is_turned_off_between_attempts(caplog):
    sharing = {"on": True}
    calls: list[int] = []

    def opener(url, data, headers, timeout):
        calls.append(1)
        sharing["on"] = False  # the user unticks the box during the back-off
        return 503, "busy"

    delays: list[float] = []
    with caplog.at_level(logging.INFO, logger="arenamcp.match_bundle"):
        ok = mb.upload_bundle(
            b"gz",
            match_id="m",
            license_key="k",
            install_id="i",
            base_url="https://e.test",
            opener=opener,
            sleep=delays.append,
            should_upload=lambda: sharing["on"],
        )
    assert not ok and len(calls) == 1 and delays == [5.0]
    assert "[BUNDLE] skipped: sharing turned off before upload of m" in caplog.text
    # ... and before the very first attempt too (building a bundle takes a while)
    calls.clear()
    assert not mb.upload_bundle(
        b"gz",
        match_id="m",
        license_key="k",
        install_id="i",
        base_url="https://e.test",
        opener=opener,
        sleep=delays.append,
        should_upload=lambda: False,
    )
    assert calls == []


# --- orchestration -------------------------------------------------------------


def test_process_match_saves_locally_and_uploads(world, caplog):
    opener = _Opener((200, "ok"))
    with caplog.at_level(logging.INFO, logger="arenamcp.match_bundle"):
        path = mb.process_match(
            world["ctx"],
            settings=_settings(),
            result="loss",
            final_state=_final_state(),
            packet_path=world["packet_path"],
            advice_history=[{"advice": "x"}],
            config={},
            bug_dir=world["bug_dir"],
            bundle_dir=world["bundle_dir"],
            base_url="https://e.test",
            opener=opener,
            sleep=lambda s: None,
        )
    assert path and path.name == f"{MATCH_ID}.json.gz" and path.parent == world["bundle_dir"]
    bundle = json.loads(gzip.decompress(path.read_bytes()))
    _assert_clean(json.dumps(bundle))
    assert opener.calls[0][1] == path.read_bytes()
    assert "[BUNDLE] saved" in caplog.text and "[BUNDLE] uploaded" in caplog.text
    # the same match is never bundled twice in one process
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="arenamcp.match_bundle"):
        assert (
            mb.process_match(
                world["ctx"],
                settings=_settings(),
                packet_path=world["packet_path"],
                bundle_dir=world["bundle_dir"],
                opener=opener,
            )
            is None
        )
    assert "[BUNDLE] skipped: already bundled" in caplog.text


def test_process_match_rechecks_consent_before_retrying(world, caplog):
    settings = _settings()

    def opener(url, data, headers, timeout):
        settings["share_match_logs"] = False
        return 503, "busy"

    with caplog.at_level(logging.INFO, logger="arenamcp.match_bundle"):
        path = mb.process_match(
            world["ctx"],
            settings=settings,
            packet_path=world["packet_path"],
            bundle_dir=world["bundle_dir"],
            opener=opener,
            sleep=lambda s: None,
        )
    assert path is not None  # the local copy is kept
    assert "[BUNDLE] skipped: sharing turned off before upload" in caplog.text
    assert "[BUNDLE] uploaded" not in caplog.text


def test_later_games_of_a_bo3_bundle_separately(world, caplog):
    import dataclasses

    ctx2 = dataclasses.replace(world["ctx"], game_number=2)
    assert ctx2.bundle_id == f"{MATCH_ID}-g2" and world["ctx"].bundle_id == MATCH_ID
    opener = _Opener((200, "ok"), (200, "ok"))
    kwargs = dict(
        settings=_settings(),
        packet_path=world["packet_path"],
        bundle_dir=world["bundle_dir"],
        opener=opener,
        sleep=lambda s: None,
    )
    with caplog.at_level(logging.INFO, logger="arenamcp.match_bundle"):
        p1 = mb.process_match(world["ctx"], **kwargs)
        p2 = mb.process_match(ctx2, **kwargs)
    assert p1 and p2 and p1.name == f"{MATCH_ID}.json.gz" and p2.name == f"{MATCH_ID}-g2.json.gz"
    bundle = json.loads(gzip.decompress(p2.read_bytes()))
    assert bundle["match_id"] == f"{MATCH_ID}-g2" and bundle["arena_match_id"] == MATCH_ID
    assert bundle["game_number"] == 2 and bundle["player_log"]["start_marker_found"]
    assert opener.calls[1][2]["X-MTGACoach-Match-ID"] == f"{MATCH_ID}-g2"
    assert "[BUNDLE] skipped: already bundled" not in caplog.text


@pytest.mark.parametrize(
    "settings, message",
    [
        (_settings(share_match_logs=False), "sharing off"),
        (_settings(license_key=""), "no license key"),
    ],
)
def test_process_match_skips_without_consent_or_key(world, caplog, settings, message):
    opener = _Opener((200, "ok"))
    with caplog.at_level(logging.INFO, logger="arenamcp.match_bundle"):
        path = mb.process_match(
            world["ctx"],
            settings=settings,
            packet_path=world["packet_path"],
            bundle_dir=world["bundle_dir"],
            opener=opener,
        )
    assert path is None and not opener.calls and not world["bundle_dir"].exists()
    assert f"[BUNDLE] skipped: {message}" in caplog.text


def test_process_match_skips_an_empty_match_but_not_an_empty_packet(tmp_path, world, caplog):
    empty_ctx = mb.begin_match("empty-match", coach_log_path=tmp_path / "none.log", player_log_path=None)
    with caplog.at_level(logging.INFO, logger="arenamcp.match_bundle"):
        assert (
            mb.process_match(
                empty_ctx, settings=_settings(), bundle_dir=world["bundle_dir"], opener=_Opener()
            )
            is None
        )
    assert "[BUNDLE] skipped: nothing recorded" in caplog.text
    # decisions=[] with real GRE traffic still bundles (late re-surfaced matches)
    packet = world["packet_path"]
    packet.write_text(json.dumps({**json.loads(packet.read_text()), "decisions": []}))
    opener = _Opener((200, "ok"))
    path = mb.process_match(
        world["ctx"],
        settings=_settings(),
        packet_path=packet,
        bundle_dir=world["bundle_dir"],
        opener=opener,
        sleep=lambda s: None,
    )
    assert path is not None and opener.calls


def test_process_match_never_raises(world, caplog, monkeypatch):
    monkeypatch.setattr(mb, "build_bundle", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaboom")))
    with caplog.at_level(logging.WARNING, logger="arenamcp.match_bundle"):
        assert mb.process_match(world["ctx"], settings=_settings(), packet_path=world["packet_path"]) is None
    assert "[BUNDLE] failed: kaboom" in caplog.text


def test_schedule_upload_runs_on_a_daemon_thread(world, caplog):
    opener = _Opener((200, "ok"))
    with caplog.at_level(logging.INFO, logger="arenamcp.match_bundle"):
        thread = mb.schedule_upload(
            world["ctx"],
            settings=_settings(),
            result="win",
            packet_path=world["packet_path"],
            bundle_dir=world["bundle_dir"],
            opener=opener,
            sleep=lambda s: None,
        )
        assert thread is not None and thread.daemon
        thread.join(timeout=30)
    assert not thread.is_alive() and opener.calls and "[BUNDLE] uploaded" in caplog.text
    with caplog.at_level(logging.INFO, logger="arenamcp.match_bundle"):
        assert mb.schedule_upload(world["ctx"], settings=_settings(share_match_logs=False)) is None
    assert "[BUNDLE] skipped: sharing off" in caplog.text


# --- engine hooks ----------------------------------------------------------------


def _engine(tmp_path):
    from arenamcp.standalone_postmatch import _PostMatchMixin

    class Watcher:
        log_path = tmp_path / "Player.log"
        file_position = 7

    class Server:
        watcher = Watcher()

    class Mcp:
        _server = Server()

    class Engine(_PostMatchMixin):
        def __init__(self):
            self.settings = _settings()
            self._mcp = Mcp()
            self._coach = None
            self._autopilot = None
            self._advice_history = [{"advice": "x"}]
            self.backend_name = "proxy"
            self.draft_mode = False
            self.set_code = None
            self._match_bundle_ctx = None

        def _get_latest_replay_path(self):
            return None

        def _get_served_model(self):
            return "served"

    return Engine()


def test_engine_begin_and_finish_consume_the_context_once(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    engine._begin_match_bundle(MATCH_ID)
    ctx = engine._match_bundle_ctx
    assert (
        ctx.match_id == MATCH_ID
        and ctx.player_log_start == 7
        and ctx.player_log_path == tmp_path / "Player.log"
    )
    scheduled: list[dict] = []
    monkeypatch.setattr(mb, "schedule_upload", lambda c, **kw: scheduled.append({"ctx": c, **kw}))
    engine._finish_match_bundle(
        result="win", final_state={"a": 1}, packet_path=Path("p.json"), reason="event-signal"
    )
    engine._finish_match_bundle(result="win", final_state=None, packet_path=None, reason="match-boundary")
    assert len(scheduled) == 1 and scheduled[0]["ctx"] is ctx and engine._match_bundle_ctx is None
    assert (
        scheduled[0]["advice_history"] == [{"advice": "x"}]
        and scheduled[0]["config"]["served_model"] == "served"
    )
    assert scheduled[0]["config"]["backend"] == "proxy" and scheduled[0]["packet_path"] == Path("p.json")
    engine._begin_match_bundle(None)
    assert engine._match_bundle_ctx is None


def test_engine_bo3_games_get_their_own_context_and_packet(tmp_path, monkeypatch, caplog):
    from arenamcp import match_packets

    monkeypatch.setattr(match_packets, "PACKETS_DIR", tmp_path / "packets")
    match_id = "bo3-" + "1234abcd" * 3
    engine = _engine(tmp_path)
    engine._has_explicit_game_end_evidence = lambda: False
    engine._detect_match_result = lambda: "loss"
    scheduled: list[dict] = []
    monkeypatch.setattr(mb, "schedule_upload", lambda c, **kw: scheduled.append({"ctx": c, **kw}))

    # Game 1: context + packet start on the match-id change; the event signal finishes both.
    engine._begin_match_bundle(match_id)
    assert match_packets.start_match_packet(match_id) is not None
    engine._finalize_match_packet(match_packets.stop_match_packet(), "loss")
    engine._finish_match_bundle(result="loss", final_state=None, packet_path=None, reason="event-signal")
    assert scheduled[-1]["ctx"].bundle_id == match_id and engine._match_bundle_ctx is None
    # The match boundary that follows every game end is quiet; any other reason is visible.
    with caplog.at_level(logging.INFO):
        engine._finish_match_bundle(
            result="loss", final_state=None, packet_path=None, reason="match-boundary"
        )
        assert "[BUNDLE] skipped: no match context" not in caplog.text
        engine._finish_match_bundle(result="loss", final_state=None, packet_path=None, reason="event-signal")
    assert "[BUNDLE] skipped: no match context (event-signal)" in caplog.text

    # Game 2 of the same match: the turn drop re-arms the context and restarts the packet.
    with caplog.at_level(logging.INFO):
        engine._begin_next_game_bundle(match_id)
    ctx2 = engine._match_bundle_ctx
    assert ctx2 is not None and ctx2.game_number == 2 and ctx2.bundle_id == f"{match_id}-g2"
    assert match_packets.get_current_packet().match_id == f"{match_id}-g2"
    assert f"[BUNDLE] game 2 of {match_id} started" in caplog.text
    # A turn drop with no game-end evidence is a resync of the same game: nothing changes.
    engine._begin_next_game_bundle(match_id)
    assert engine._match_bundle_ctx is ctx2 and len(scheduled) == 1
    # Game 2 ended but the signal was missed: the turn drop with evidence closes it and arms game 3.
    engine._has_explicit_game_end_evidence = lambda: True
    engine._begin_next_game_bundle(match_id)
    assert scheduled[-1]["ctx"] is ctx2 and scheduled[-1]["result"] == "loss"
    assert scheduled[-1]["packet_path"] and scheduled[-1]["packet_path"].name.endswith(f"{match_id}-g2.json")
    ctx3 = engine._match_bundle_ctx
    assert ctx3.game_number == 3 and ctx3.bundle_id == f"{match_id}-g3"
    assert match_packets.get_current_packet().match_id == f"{match_id}-g3"
    # A fresh match resets the game counter.
    engine._begin_match_bundle("other-" + "a" * 20)
    assert engine._match_bundle_ctx.game_number == 1
    match_packets.stop_match_packet()


def test_engine_finish_swallows_scheduler_errors(tmp_path, monkeypatch, caplog):
    engine = _engine(tmp_path)
    engine._begin_match_bundle(MATCH_ID)
    monkeypatch.setattr(mb, "schedule_upload", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")))
    with caplog.at_level(logging.WARNING):
        engine._finish_match_bundle(result="win", final_state=None, packet_path=None, reason="x")
    assert "[BUNDLE] could not schedule match bundle: no" in caplog.text


def test_engine_finalize_match_packet_saves_and_returns_the_path(tmp_path, monkeypatch):
    from arenamcp import match_packets

    monkeypatch.setattr(match_packets, "PACKETS_DIR", tmp_path / "packets")
    engine = _engine(tmp_path)
    assert engine._finalize_match_packet(None, "win") is None
    packet = match_packets.MatchPacket(MATCH_ID)
    path = engine._finalize_match_packet(packet, None)
    assert path and path.exists() and packet.result == "unknown"
    engine.set_share_match_logs(False)
    assert engine.settings["share_match_logs"] is False


def test_set_share_match_logs_reaches_the_engine_through_the_pipe():
    class Coach:
        def __init__(self):
            self.calls: list[bool] = []
            self.settings = _settings()

        def set_share_match_logs(self, enabled):
            self.calls.append(enabled)
            return enabled

    adapter = PipeAdapter()
    events: list[dict] = []
    adapter._emit = events.append  # type: ignore[method-assign]
    coach = Coach()
    adapter._coach = coach
    adapter._dispatch({"cmd": "set_share_match_logs", "enabled": False})
    adapter._dispatch({"cmd": "set_share_match_logs", "enabled": True})
    assert coach.calls == [False, True]
    assert not [e for e in events if e.get("type") == "error"]

    class Bare:
        settings = _settings()

    adapter._coach = Bare()
    adapter._dispatch({"cmd": "set_share_match_logs", "enabled": False})
    assert Bare.settings["share_match_logs"] is False


def test_the_tools_menu_toggles_match_log_sharing(monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from arenamcp.desktop.main_window import MainWindow

    window = MainWindow()
    try:
        sent: list[dict] = []
        monkeypatch.setattr(window._session._process, "send_payload", sent.append)
        (action,) = [
            a for a in window.findChildren(QAction) if a.text() == "Share Match Logs to Improve the Coach"
        ]
        assert action.isCheckable() and action.isChecked()  # default on
        action.setChecked(False)
        assert {"cmd": "set_share_match_logs", "enabled": False} in sent
        assert window._settings.get("share_match_logs") is False
        action.setChecked(True)
        assert {"cmd": "set_share_match_logs", "enabled": True} in sent
    finally:
        window.close()


def test_bug_reports_saved_during_a_match_are_attached_to_its_context(tmp_path, monkeypatch):
    from arenamcp import standalone_diagnostics as diag

    monkeypatch.setattr(diag, "LOG_DIR", tmp_path)
    monkeypatch.setattr(diag, "copy_to_clipboard", lambda _url: False)

    class Engine(diag._DiagnosticsMixin):
        _autopilot = None

        class ui:
            @staticmethod
            def log(*_a, **_k):
                pass

        def _collect_debug_info(self, progress_cb=None, extra_context=None):
            return {"settings": {"license_key": LICENSE}}

    engine = Engine()
    engine._match_bundle_ctx = mb.begin_match(MATCH_ID, coach_log_path=tmp_path / "none.log")
    path = engine.save_bug_report("test", announce=False)
    assert path and engine._match_bundle_ctx.bug_reports == [path]
