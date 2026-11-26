import json

from arenamcp.ladder import (
    RankLog,
    event_display_name,
    event_rule_summary,
    pip_progress,
    rank_short,
    rank_text,
    ranked_ladder,
)


def test_rank_log_follows_split_updates_and_rotation(tmp_path):
    path = tmp_path / "Player.log"
    reader = RankLog()
    assert not reader.read(path)
    header = b"<== RankGetCombinedRankInfo(id)\n"
    snapshot = {
        "constructedSeasonOrdinal": 94,
        "constructedClass": "Mythic",
        "constructedPercentile": 81.1593,
        "limitedSeasonOrdinal": 94,
        "limitedLevel": 4,
    }
    path.write_bytes(b"unrelated\n" * 60000 + header + json.dumps(snapshot).encode()[:-1])
    assert reader.read(path)
    assert not reader.read(path)
    assert reader.snapshot == {}
    with path.open("ab") as log:
        log.write(b"}\n")
    assert not reader.read(path)
    assert rank_text(reader.snapshot, "Constructed") == "Mythic 81.2%"
    assert rank_text(reader.snapshot, "Limited") == "Bronze 4 · 0 pips"
    with path.open("ab") as log:
        log.write(header + b"bad response\n")
    assert not reader.read(path)
    assert reader.snapshot == snapshot
    path.write_bytes(header + b'{"limitedSeasonOrdinal":95,"limitedLevel":3}\n')
    assert not reader.read(path)
    assert rank_text(reader.snapshot, "Constructed") == "Unavailable"
    assert rank_text(reader.snapshot, "Limited") == "Bronze 3 · 0 pips"


def test_mythic_leaderboard_takes_precedence_over_percentile():
    assert (
        rank_text(
            {"limitedClass": "Mythic", "limitedPercentile": 99, "limitedLeaderboardPlace": 12}, "Limited"
        )
        == "Mythic #12"
    )


def test_ranked_event_formats():
    for event in (
        "PremierDraft_YFRA",
        "QuickDraft FRA 20261027",
        "PickTwoDraft_MSH",
        "ContenderDraft_TRK_20261123",
        "ContenderDraft TRK 20261123",
        "Limited Ladder",
    ):
        assert ranked_ladder(event) == "Limited"
    for event in ("Brawl Ladder", "Ladder", "Traditional Standard Ranked"):
        assert ranked_ladder(event) == "Constructed"
    for event in (
        "Play",
        "Sealed_TDM",
        "TradDraft_TDM",
        "CubeDraft PoweredTraditional",
        "Standard Unranked",
        "",
    ):
        assert ranked_ladder(event) is None


def test_pip_progress_rank_short_and_event_helpers():
    snap = {
        "constructedClass": "Mythic",
        "constructedPercentile": 88.18,
        "limitedClass": "Bronze",
        "limitedLevel": 2,
        "limitedStep": 1,
    }
    assert rank_short(snap, "Constructed") == "Mythic 88.2%"
    assert pip_progress(snap, "Constructed") is None
    assert rank_short(snap, "Limited") == "Bronze 2"
    assert pip_progress(snap, "Limited") == (1, 4)
    assert pip_progress({"limitedClass": "Silver", "limitedLevel": 1, "limitedStep": 3}, "Limited") == (3, 5)
    assert pip_progress({"limitedClass": "Gold", "limitedLevel": 3, "limitedStep": 4}, "Limited") == (4, 6)
    assert event_display_name("ContenderDraft_TRK_20261123") == "Contender Draft · TRK"
    assert event_rule_summary("ContenderDraft_TRK_20261123", "CreateMatch") == "7W prize · 3L out"
    assert event_rule_summary("TradDraft_TRK_20261123", "CreateMatch") == "3 matches"
    assert event_rule_summary("ContenderDraft_TRK_20261123", "PlayerDraft") == "Drafting"


def test_rank_log_tracks_active_event_and_dynamic_ranked_deltas(tmp_path):
    path = tmp_path / "Player.log"
    reader = RankLog()
    lines = [
        "<== RankGetCombinedRankInfo(1)",
        json.dumps(
            {
                "constructedSeasonOrdinal": 95,
                "constructedClass": "Mythic",
                "limitedSeasonOrdinal": 95,
                "limitedLevel": 3,
                "limitedMatchesWon": 0,
                "limitedMatchesLost": 0,
            }
        ),
        "<== EventGetCoursesV2(2)",
        json.dumps(
            {
                "Courses": [
                    {
                        "InternalEventName": "DualColorPrecons",
                        "CurrentModule": "CreateMatch",
                        "CurrentWins": 4,
                        "CurrentLosses": 17,
                    },
                    {
                        "InternalEventName": "ContenderDraft_TRK_20261123",
                        "CurrentModule": "CreateMatch",
                        "CurrentWins": 2,
                        "CurrentLosses": 1,
                    },
                ]
            }
        ),
        '{"matchGameRoomStateChangedEvent":{"gameRoomInfo":{"gameRoomConfig":{"eventId":"CustomMysteryDraft_20261126"}}}}',
        "<== RankGetCombinedRankInfo(3)",
        json.dumps(
            {
                "constructedSeasonOrdinal": 95,
                "constructedClass": "Mythic",
                "limitedSeasonOrdinal": 95,
                "limitedLevel": 3,
                "limitedMatchesWon": 1,
                "limitedMatchesLost": 0,
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n")
    reader.read(path)
    # Unknown event was learned as Limited via limitedMatchesWon increment.
    assert (
        ranked_ladder("CustomMysteryDraft_20261126", extra_ranked=reader.observed_ranked_events) == "Limited"
    )
    ev = reader.active_event()
    assert ev is not None
    assert ev["event_name"] == "ContenderDraft_TRK_20261123"
    assert ev["display_name"] == "Contender Draft · TRK"
    assert (ev["wins"], ev["losses"]) == (2, 1)

    # Entering a normal ladder match clears the active event banner.
    with path.open("a") as log:
        log.write('{"eventId":"Brawl_Ladder"}\n')
    reader.read(path)
    assert reader._active_event_name == ""


def test_account_follows_login_auth_switch_and_logout(tmp_path):
    path = tmp_path / "Player.log"
    reader = RankLog()
    path.write_text("[Accounts - Login] Logged in successfully. Display Name: First#12345\n")
    reader.read(path)
    assert reader.account_name == "First#12345"
    reader.snapshot = {"constructedClass": "Gold"}
    with path.open("a") as log:
        log.write(
            json.dumps({"authenticateResponse": {"clientId": "one", "screenName": "First#12345"}}) + "\n"
        )
        log.write(json.dumps({"otherPlayer": {"screenName": "Opponent"}}) + "\n")
    reader.read(path)
    assert reader.account_name == "First#12345"
    assert reader.snapshot
    with path.open("a") as log:
        log.write(json.dumps({"authenticateResponse": {"clientId": "two"}}) + "\n")
    reader.read(path)
    assert not reader.account_name
    assert not reader.snapshot
    with path.open("a") as log:
        log.write(json.dumps({"authenticateResponse": {"clientId": "two", "screenName": "Second"}}) + "\n")
    reader.read(path)
    assert reader.account_name == "Second"
    with path.open("a") as log:
        log.write("[Accounts - Logout] Logging out\n")
    reader.read(path)
    assert not reader.account_name


def test_account_reset_on_log_replacement_and_missing_file(tmp_path):
    path = tmp_path / "Player.log"
    reader = RankLog()
    path.write_text("[Accounts - Login] Logged in successfully. Display Name: Old\n")
    reader.read(path)
    assert reader.account_name == "Old"
    path.write_text("new session\n")
    reader.read(path)
    assert not reader.account_name
    path.unlink()
    reader.read(path)
    assert not reader.account_name
