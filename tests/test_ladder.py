import json

from arenamcp.ladder import RankLog, rank_text, ranked_ladder


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
    for event in ("PremierDraft_YFRA", "QuickDraft FRA 20261027", "PickTwoDraft_MSH", "Limited Ladder"):
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
