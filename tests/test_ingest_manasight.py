"""Regression tests for `tools/training/ingest_manasight.py`.

The load-bearing claims this pins:

1. A `Player.log` blob reader that survives pretty-printed JSON, braces
   inside strings, and gzip.
2. **Local-seat detection must NOT trust `ConnectResp`.** Measured over the
   real corpus, ConnectResp named the wrong seat in 14 of the 70 match
   segments carrying one, while the GRE request addressee was right 106/106
   (cross-checked against hand visibility). A wrong seat silently emits the
   OPPONENT's hand, board and menu, which is indistinguishable downstream
   from a bad model — so it gets a test, not a comment.
3. Request/response pairing survives `msgId` reuse, which a session log has
   (one log = many matches) and a `.rply` does not.
4. **Schema parity with `tools/eval/replay/menu_groundtruth.py`** — the whole
   point of the ingester is that the two corpora concatenate. The same match
   is fed through both extractors and the records are compared field by
   field.
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.eval.replay import menu_groundtruth  # noqa: E402
from tools.training import ingest_manasight as ing  # noqa: E402

# ---------------------------------------------------------------------------
# Card DB stub — tests must not need an MTGA install.
# ---------------------------------------------------------------------------


class _NullCardDB:
    """Resolves nothing; `card_facts` then yields `#<grpId>` placeholder names."""

    def get_card_by_arena_id(self, _gid):
        return None


@pytest.fixture(autouse=True)
def _stub_card_db(monkeypatch):
    monkeypatch.setattr(menu_groundtruth, "_CARD_DB", _NullCardDB())
    monkeypatch.setattr(menu_groundtruth, "_CARD_CACHE", {})


# ---------------------------------------------------------------------------
# Fixture match: one Main-1 priority window, land drop taken.
# ---------------------------------------------------------------------------

LOCAL_SEAT = 2
OPP_SEAT = 1

FULL_STATE = {
    "type": "GREMessageType_GameStateMessage",
    "systemSeatIds": [LOCAL_SEAT],
    "msgId": 10,
    "gameStateId": 5,
    "gameStateMessage": {
        "type": "GameStateType_Full",
        "gameStateId": 5,
        "gameInfo": {
            "superFormat": "SuperFormat_Constructed",
            "variant": "GameVariant_Normal",
            "gameNumber": 1,
        },
        "turnInfo": {
            "turnNumber": 3,
            "phase": "Phase_Main1",
            "step": "Step_Main1",
            "activePlayer": LOCAL_SEAT,
            "priorityPlayer": LOCAL_SEAT,
        },
        "players": [
            {"systemSeatNumber": 1, "lifeTotal": 20, "handSize": 4},
            {"systemSeatNumber": 2, "lifeTotal": 18, "handSize": 2},
        ],
        "zones": [
            {"zoneId": 31, "type": "ZoneType_Hand", "ownerSeatId": LOCAL_SEAT},
            {"zoneId": 32, "type": "ZoneType_Hand", "ownerSeatId": OPP_SEAT},
            {"zoneId": 28, "type": "ZoneType_Battlefield"},
        ],
        "gameObjects": [
            {
                "instanceId": 101,
                "grpId": 95815,
                "zoneId": 31,
                "ownerSeatId": LOCAL_SEAT,
                "controllerSeatId": LOCAL_SEAT,
                "cardTypes": ["CardType_Land"],
            },
            {
                "instanceId": 102,
                "grpId": 92081,
                "zoneId": 31,
                "ownerSeatId": LOCAL_SEAT,
                "controllerSeatId": LOCAL_SEAT,
                "cardTypes": ["CardType_Creature"],
            },
            {
                "instanceId": 201,
                "grpId": 95815,
                "zoneId": 28,
                "ownerSeatId": LOCAL_SEAT,
                "controllerSeatId": LOCAL_SEAT,
                "cardTypes": ["CardType_Land"],
                "isTapped": False,
            },
            {
                "instanceId": 301,
                "grpId": 96760,
                "zoneId": 28,
                "ownerSeatId": OPP_SEAT,
                "controllerSeatId": OPP_SEAT,
                "cardTypes": ["CardType_Creature"],
            },
        ],
    },
}

ACTIONS_REQ = {
    "type": "GREMessageType_ActionsAvailableReq",
    "systemSeatIds": [LOCAL_SEAT],
    "msgId": 11,
    "gameStateId": 5,
    "actionsAvailableReq": {
        "actions": [
            {
                "actionType": "ActionType_Cast",
                "grpId": 92081,
                "instanceId": 102,
                "manaCost": [{"color": ["ManaColor_White"], "count": 1}],
            },
            {"actionType": "ActionType_Play", "grpId": 95815, "instanceId": 101},
            {
                "actionType": "ActionType_Activate_Mana",
                "grpId": 95815,
                "instanceId": 201,
                "abilityGrpId": 1001,
            },
            {"actionType": "ActionType_Pass"},
        ],
        "inactiveActions": [
            {"actionType": "ActionType_Cast", "grpId": 99999, "instanceId": 999},
        ],
    },
}

PERFORM_RESP = {
    "type": "ClientMessageType_PerformActionResp",
    "gameStateId": 5,
    "respId": 11,
    "performActionResp": {
        "actions": [{"actionType": "ActionType_Play", "grpId": 95815, "instanceId": 101}],
        "autoPassPriority": "AutoPassPriority_Yes",
    },
}

ROOM_EVENT = {
    "matchGameRoomStateChangedEvent": {
        "gameRoomInfo": {
            "stateType": "MatchGameRoomStateType_Playing",
            "gameRoomConfig": {
                "matchId": "match-0001",
                "reservedPlayers": [
                    {"systemSeatId": 1, "teamId": 1, "eventId": "Ladder"},
                    {"systemSeatId": 2, "teamId": 2, "eventId": "Ladder"},
                ],
            },
        }
    }
}

# ConnectResp deliberately names the WRONG seat — this is the real-corpus bug.
CONNECT_RESP_WRONG = {
    "type": "GREMessageType_ConnectResp",
    "systemSeatIds": [OPP_SEAT],
    "msgId": 1,
    "connectResp": {"status": "ConnectionStatus_Success"},
}


def _write_player_log(
    path: Path,
    *,
    connect_resp: dict | None = CONNECT_RESP_WRONG,
    extra_messages: list[dict] | None = None,
    extra_out: list[dict] | None = None,
) -> Path:
    """Write a gzipped Player.log fragment in MTGA's real on-disk layout."""
    lines: list[str] = [
        "Mono path[0] = 'C:/Program Files/Wizards of the Coast/MTGA'",
        "DETAILED LOGS: ENABLED",
        # A decoy blob with braces inside a string: the scanner must not
        # desynchronise on it, and the marker pre-filter must skip it.
        '{"unrelated": "a } brace { in a string", "note": "ignore me"}',
        "[UnityCrossThreadLogger]MatchGameRoomStateChangedEvent",
        json.dumps(ROOM_EVENT),
    ]
    gre_msgs = ([connect_resp] if connect_resp else []) + [FULL_STATE, ACTIONS_REQ]
    gre_msgs += extra_messages or []
    # GreToClientEvent: always a single line in real logs.
    lines.append("[UnityCrossThreadLogger]1/1/2026 0:00:00 AM: Match to <redacted>: GreToClientEvent")
    lines.append(json.dumps({"transactionId": "t1", "greToClientEvent": {"greToClientMessages": gre_msgs}}))
    # ClientToGREMessage: always pretty-printed across many lines in real logs.
    for payload in [PERFORM_RESP] + (extra_out or []):
        lines.append("[UnityCrossThreadLogger]1/1/2026 0:00:00 AM: ABC to Match: ClientToGremessage")
        lines.append(
            json.dumps(
                {
                    "requestId": 1,
                    "clientToMatchServiceMessageType": "ClientToMatchServiceMessageType_ClientToGREMessage",
                    "payload": payload,
                },
                indent=2,
            )
        )
    lines.append(
        '{"constructedSeasonOrdinal": 90, "constructedClass": "Silver", '
        '"constructedLevel": 3, "limitedClass": "Gold", "limitedLevel": 2}'
    )
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def _write_rply(path: Path) -> Path:
    """Same match as a `.rply`, so both extractors can be run over it."""
    connect_ok = dict(CONNECT_RESP_WRONG, systemSeatIds=[LOCAL_SEAT])
    lines = [
        "#Version2",
        json.dumps(
            {"Local": {"ScreenName": "tester"}, "Opponent": {"ScreenName": "opp"}, "BattlefieldId": "bf"}
        ),
    ]
    for i, msg in enumerate([connect_ok, FULL_STATE, ACTIONS_REQ]):
        lines.append(f"IN-{1000 + i}:{json.dumps(msg)}")
    lines.append(f"OUT-2000:{json.dumps(PERFORM_RESP)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Blob reader
# ---------------------------------------------------------------------------


def test_blob_reader_handles_gzip_multiline_and_braces_in_strings(tmp_path):
    log = _write_player_log(tmp_path / "s.log.gz")
    blobs = list(ing.iter_json_blobs(log))
    kinds = [next(iter(b)) if len(b) == 1 else sorted(b)[0] for b in blobs]
    # The decoy `{ } in a string` blob is filtered out by the marker pre-filter,
    # and the pretty-printed ClientToGREMessage is reassembled intact.
    assert not any("unrelated" in b for b in blobs)
    assert any("greToClientEvent" in b for b in blobs)
    assert any(b.get("clientToMatchServiceMessageType") for b in blobs)
    assert any("matchGameRoomStateChangedEvent" in b for b in blobs)
    out = next(b for b in blobs if b.get("clientToMatchServiceMessageType"))
    assert out["payload"]["performActionResp"]["actions"][0]["grpId"] == 95815
    assert kinds  # sanity: we actually parsed something


def test_unterminated_blob_does_not_hang_or_eat_the_file(tmp_path):
    p = tmp_path / "trunc.log"
    p.write_text('{"greToClientEvent": {"greToClientMessages": [\n{"type": "x"}\n', encoding="utf-8")
    assert list(ing.iter_json_blobs(p, max_blob_lines=5)) == []


def test_payload_as_json_string_is_coerced():
    assert ing._coerce_payload('{"a": 1}') == {"a": 1}
    assert ing._coerce_payload({"a": 1}) == {"a": 1}
    assert ing._coerce_payload("not json") is None
    assert ing._coerce_payload(None) is None


# ---------------------------------------------------------------------------
# Local-seat detection — the expensive-to-get-wrong one
# ---------------------------------------------------------------------------


def test_local_seat_prefers_request_addressee_over_wrong_connect_resp(tmp_path):
    """ConnectResp lies in 20% of real corpus segments; requests never did."""
    log = _write_player_log(tmp_path / "s.log.gz")
    matches, _ = ing.parse_player_log(log)
    assert len(matches) == 1
    seat, method = ing.detect_local_seat_player_log(matches[0].messages)
    assert (seat, method) == (LOCAL_SEAT, "request_addressee")

    # And the stdlib-of-record detector (correct for .rply) gets it wrong here,
    # which is exactly why this module does not reuse it.
    from tools.eval.replay.state import detect_local_seat as rply_detect

    assert rply_detect(matches[0].messages) == OPP_SEAT


def test_local_seat_falls_back_to_connect_resp_when_no_requests():
    from tools.eval.replay.reader import ReplayMessage

    msgs = [
        ReplayMessage(direction="IN", timestamp_us=0, payload=dict(CONNECT_RESP_WRONG, systemSeatIds=[2]))
    ]
    assert ing.detect_local_seat_player_log(msgs) == (2, "connect_resp_fallback")
    assert ing.detect_local_seat_player_log([]) == (None, "undetermined")


def test_wrong_seat_would_be_caught_by_hand_visibility_crosscheck(tmp_path):
    """The cross-check must actually fire when the seat is wrong."""
    log = _write_player_log(tmp_path / "s.log.gz")
    matches, _ = ing.parse_player_log(log)
    lm = matches[0]
    _, good = ing.extract_match(lm, "s.log.gz")
    assert good.hand_visible_local > good.hand_visible_opp

    # Force the opposite seat and confirm the counters invert (i.e. a silent
    # wrong-seat ingest is detectable, not invisible).
    for m in lm.messages:
        if m.msg_type == "GREMessageType_ActionsAvailableReq":
            m.payload["systemSeatIds"] = [OPP_SEAT]
    _, bad = ing.extract_match(lm, "s.log.gz")
    assert bad.local_seat == OPP_SEAT
    assert bad.hand_visible_opp > bad.hand_visible_local


# ---------------------------------------------------------------------------
# Pairing across msgId reuse
# ---------------------------------------------------------------------------


def test_pairing_survives_msg_id_reuse():
    """A session log reuses msgIds; a flat {msgId: resp} map would mispair."""
    from tools.eval.replay.reader import ReplayMessage

    def req(mid):
        return ReplayMessage(
            direction="IN",
            timestamp_us=0,
            payload={
                "type": "GREMessageType_ActionsAvailableReq",
                "msgId": mid,
                "systemSeatIds": [2],
                "actionsAvailableReq": {"actions": []},
            },
        )

    def resp(rid, tag):
        return ReplayMessage(
            direction="OUT",
            timestamp_us=0,
            payload={
                "type": "ClientMessageType_PerformActionResp",
                "respId": rid,
                "performActionResp": {"actions": [{"actionType": tag}]},
            },
        )

    msgs = [req(11), resp(11, "first"), req(11), resp(11, "second")]
    pairs = ing.pair_requests_ordered(msgs)
    assert pairs[0].payload["performActionResp"]["actions"][0]["actionType"] == "first"
    assert pairs[2].payload["performActionResp"]["actions"][0]["actionType"] == "second"

    # The naive flat map used for .rply would hand BOTH requests the last response.
    from tools.eval.replay.decisions import pair_request_response

    flat = pair_request_response(msgs)
    assert flat[11].payload["performActionResp"]["actions"][0]["actionType"] == "second"


# ---------------------------------------------------------------------------
# Extraction + schema parity with menu_groundtruth
# ---------------------------------------------------------------------------


def test_extract_match_recovers_menu_and_pick(tmp_path):
    log = _write_player_log(tmp_path / "s.log.gz")
    matches, ranks = ing.parse_player_log(log)
    records, stats = ing.extract_match(matches[0], "s.log.gz")

    assert len(records) == 1
    rec = records[0]
    assert rec["local_seat"] == LOCAL_SEAT
    assert rec["real_menu_size"] == 4
    assert rec["real_menu_key"] == sorted(["Cast:92081", "Play:95815", "Activate_Mana:95815", "Pass"])
    # Spell-level key drops the per-land mana tap.
    assert "Activate_Mana:95815" not in rec["real_menu_key_spell"]
    assert rec["real_menu_inactive_key"] == ["Cast:99999"]
    assert rec["real_pick"]["status"] == "ok"
    assert rec["real_pick"]["match"] == "exact"
    assert rec["real_pick"]["action_type"] == "ActionType_Play"
    assert rec["real_menu"][rec["real_pick"]["menu_index"]]["grp_id"] == 95815
    assert rec["is_start_of_main1"] is True
    assert rec["state"]["hand_grp_ids"] == [95815, 92081]
    assert rec["state"]["life"] == {"you": 18, "opp": 20}
    assert rec["source_corpus"] == "manasight"
    assert rec["source_license"] == "MIT OR Apache-2.0"
    assert rec["is_bot_match"] is False and rec["is_ranked_ladder"] is True
    assert rec["match_id"] == "match-0001"
    assert stats.emitted == 1 and stats.pick_not_in_menu == 0
    assert ranks and ranks[0].constructed_class == "Silver"


def test_records_are_schema_compatible_with_menu_groundtruth(tmp_path):
    """Same match through both extractors must merge without a translation step."""
    rply_records, _ = menu_groundtruth.extract_replay(_write_rply(tmp_path / "m.rply"), all_decisions=True)
    matches, _ = ing.parse_player_log(_write_player_log(tmp_path / "s.log.gz"))
    mana_records, _ = ing.extract_match(matches[0], "s.log.gz")

    assert len(rply_records) == 1 and len(mana_records) == 1
    a, b = rply_records[0], mana_records[0]

    missing = set(a) - set(b)
    assert not missing, f"manasight records are missing menu_groundtruth keys: {missing}"
    assert set(a["state"]) == set(b["state"])

    for key in (
        "real_menu",
        "real_menu_size",
        "real_menu_key",
        "real_menu_key_spell",
        "real_menu_inactive",
        "real_menu_inactive_key",
        "turn_number",
        "phase",
        "step",
        "active_player",
        "priority_player",
        "is_own_turn",
        "is_start_of_main1",
        "main1_window_ordinal",
        "local_seat",
        "super_format",
        "variant",
        "game_number",
        "msg_id",
        "state",
    ):
        assert a[key] == b[key], f"field {key!r} diverged: {a[key]!r} != {b[key]!r}"

    for key in (
        "status",
        "match",
        "menu_index",
        "action_type",
        "grp_id",
        "instance_id",
        "n_actions_submitted",
    ):
        assert a["real_pick"][key] == b["real_pick"][key]


# ---------------------------------------------------------------------------
# Classification helpers (drive the honest-yield numbers)
# ---------------------------------------------------------------------------


def test_meaningful_options_drops_pass_and_mana():
    menu = [
        {"action_type": "ActionType_Cast"},
        {"action_type": "ActionType_Pass"},
        {"action_type": "ActionType_Activate_Mana"},
        {"action_type": "ActionType_FloatMana"},
        {"action_type": "ActionType_Play"},
    ]
    assert [e["action_type"] for e in ing.meaningful_options(menu)] == ["ActionType_Cast", "ActionType_Play"]


@pytest.mark.parametrize(
    "event,expect",
    [
        ("AIBotMatch", (True, False)),
        ("AIBotMatch_Rebalanced", (True, False)),
        ("Ladder", (False, True)),
        ("Traditional_Explorer_Ladder", (False, True)),
        ("Play", (False, False)),
        ("PremierDraft_ECL_20260120", (False, False)),
        (None, (False, False)),
    ],
)
def test_classify_event(event, expect):
    assert ing.classify_event(event) == expect


def test_summarize_ranks_reports_omitted_class_explicitly():
    ranks = [
        ing.RankSnapshot("a.log", None, 3, 1, 2, "Gold", 4, 90),
        ing.RankSnapshot("b.log", "Silver", 2, 5, 5, None, 1, 90),
    ]
    out = ing.summarize_ranks(ranks)
    assert out["determinable"] is True
    assert out["constructed_class_snapshots"]["<omitted>"] == 1
    assert out["constructed_class_snapshots"]["Silver"] == 1
    assert out["best_constructed_class_per_file"]["Silver"] == 1


# ---------------------------------------------------------------------------
# End-to-end CLI
# ---------------------------------------------------------------------------


def test_cli_writes_jsonl_summary_and_attribution(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_player_log(corpus / "session_test.log.gz")
    out = tmp_path / "out" / "records.jsonl"
    summary_path = tmp_path / "out" / "summary.json"

    rc = ing.main(
        ["--corpus-dir", str(corpus), "--out", str(out), "--summary-out", str(summary_path), "--no-digests"]
    )
    assert rc == 0

    records = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["menus_emitted"] == 1
    assert summary["source"]["license"] == "MIT OR Apache-2.0"
    assert summary["source"]["attribution_required"] is True
    assert summary["local_seat_detection"]["matches_where_opponent_hand_was_more_visible"] == 0
    assert summary["PASS_CAVEAT"]["pass_picks"] == 0
    assert summary["player_skill"]["determinable"] is True

    attrib = out.with_suffix(".ATTRIBUTION.txt").read_text(encoding="utf-8")
    assert "manasight-corpus" in attrib
    assert "MIT OR Apache-2.0" in attrib


def test_cli_missing_corpus_dir_is_a_clean_error(tmp_path, capsys):
    assert ing.main(["--corpus-dir", str(tmp_path / "nope"), "--out", str(tmp_path / "o.jsonl")]) == 2
    assert "corpus dir not found" in capsys.readouterr().err
