from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.deck_candidates import arena_export, compare_collection
from arenamcp.deck_recommendation import claim_invitation, invitation
from arenamcp.match_history import MatchRecord

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


def records(wins, losses, event="Ladder"):
    # Post-P1 shape: every record carries the deck identity + autopilot flag
    # written at game end (see match_history.record_from_game_end).
    return [
        MatchRecord(
            match_id=str(i),
            timestamp=(NOW - timedelta(minutes=losses + wins - i)).isoformat(),
            result=result,
            format_name=event,
            deck_identity="deck-hash-" + event,
            deck_name="Test Deck",
            autopilot=True,
        )
        for i, result in enumerate(["win"] * wins + ["loss"] * losses)
    ]


@pytest.mark.parametrize(
    "rank,wins,losses,expected",
    [
        ("Gold", 6, 14, True),
        ("Gold", 7, 13, False),
        ("Platinum", 9, 11, True),
        ("Diamond", 10, 10, False),
        ("Mythic", 0, 20, False),
        ("Unknown", 0, 20, False),
        ("Platinum", 0, 19, False),
    ],
)
def test_rank_threshold_and_sample(rank, wins, losses, expected):
    result = invitation(records(wins, losses), {"constructedClass": rank}, now=NOW)
    assert bool(result) is expected


@pytest.mark.parametrize(
    "event", ["Play", "QuickDraft_FRA", "Traditional Standard Ranked", "TradLadder", "BO3 Ranked"]
)
def test_excludes_non_constructed_and_game_level_bo3(event):
    assert invitation(records(0, 30, event), {"constructedClass": "Diamond"}, now=NOW) is None


def test_window_queue_draws_and_old_records():
    history = records(0, 30)
    for rec in history[:10]:
        rec.timestamp = (NOW - timedelta(days=31)).isoformat()
    result = invitation(history, {"constructedClass": "Gold"}, now=NOW)
    assert result.losses == 20
    history[-1].result = "draw"
    assert invitation(history, {"constructedClass": "Gold"}, now=NOW) is None
    history = records(0, 30) + records(20, 0)
    for i, rec in enumerate(history):
        rec.match_id = str(i)
    assert invitation(history, {"constructedClass": "Diamond"}, now=NOW) is None
    history = records(0, 30) + records(0, 1, "Historic Ladder")
    assert invitation(history, {"constructedClass": "Diamond"}, now=NOW) is None


def test_cooldown_survives_restart_and_duplicate_event(tmp_path):
    path = tmp_path / "cooldown.json"
    assert claim_invitation(path, "first", now=NOW)
    assert not claim_invitation(path, "second", now=NOW + timedelta(hours=23))
    assert not claim_invitation(path, "first", now=NOW + timedelta(days=2))
    assert claim_invitation(path, "second", now=NOW + timedelta(days=2))


def card(name, rarity="rare", arena_id=10, type_line="Creature"):
    return SimpleNamespace(name=name, rarity=rarity, arena_id=arena_id, type_line=type_line)


def test_cost_counts_reprints_and_both_piles_and_unknown_rarities():
    db = Mock()
    db.get_card_by_arena_id.side_effect = lambda _: card("Spell")
    db.get_card_by_name.side_effect = lambda name: card(name) if name == "Spell" else None
    deck = {"mainboard": ["4 Spell"], "sideboard": ["2 Spell", "1 Unknown"]}
    cost = compare_collection(deck, {10: 1, 11: 2}, {"rare": 3}, db)
    assert cost["spend"] == {"rare": 3}
    assert cost["unknown"] == ["Unknown"]
    assert not cost["affordable"]
    deck["sideboard"] = ["2 Spell"]
    assert compare_collection(deck, {10: 1, 11: 2}, {"rare": 3}, db)["affordable"]
    assert not compare_collection(deck, {10: 1, 11: 2}, {"rare": 2}, db)["affordable"]


def test_basic_lands_and_export_validation():
    db = Mock()
    db.get_card_by_name.return_value = card("Island", rarity="common", type_line="Basic Land — Island")
    assert compare_collection({"mainboard": ["60 Island"]}, {}, {}, db)["owned"]
    assert arena_export({"mainboard": ["60 Island (SET) 123"]}) == "Deck\n60 Island"
    with pytest.raises(ValueError):
        arena_export({"mainboard": ["4 Spell"]})
    with pytest.raises(ValueError):
        arena_export({"mainboard": ["unparsed source content"]})
    db.get_card_by_name.return_value = card(
        "Snow-Covered Island", rarity="common", type_line="Basic Snow Land — Island"
    )
    cost = compare_collection({"mainboard": ["20 Snow-Covered Island"]}, {}, {}, db)
    assert cost["spend"] == {"common": 20}
    assert not cost["owned"]


def test_local_arena_rarity_handles_name_only_catalog():
    db = Mock()
    db.get_card_by_name.return_value = card("Spell", rarity="", arena_id=0)
    db.get_raw_mtgadb.return_value.get_rarity_by_name.return_value = "rare"
    cost = compare_collection({"mainboard": ["4 Spell", "56 Forest"]}, {}, {"rare": 4}, db)
    assert cost["affordable"]
    assert cost["spend"] == {"rare": 4}
    assert cost["unknown"] == []


def test_later_source_can_resolve_shadowed_arena_printing():
    db = Mock()
    db.get_card_by_name.return_value = card("Spell", rarity="", arena_id=0)
    db.get_raw_mtgadb.return_value = None
    source = Mock()
    source.get_card_by_name.return_value = card("Spell", rarity="uncommon")
    db.sources = [source]
    cost = compare_collection({"mainboard": ["4 Spell", "56 Forest"]}, {}, {"uncommon": 4}, db)
    assert cost["affordable"]
    assert cost["spend"] == {"uncommon": 4}


def test_postmatch_delivers_once_without_analysis(tmp_path, monkeypatch):
    import arenamcp.deck_recommendation as dr
    import arenamcp.match_history as mh
    from arenamcp.match_history import MatchHistory
    from arenamcp.standalone_postmatch import _PostMatchMixin

    monkeypatch.setattr(mh, "HISTORY_FILE", tmp_path / "history.json")
    log = tmp_path / "Player.log"
    log.write_text(
        '<== RankGetCombinedRankInfo(id)\n{"constructedSeasonOrdinal":95,"constructedClass":"Platinum"}\n'
    )
    monkeypatch.setenv("MTGA_LOG_PATH", str(log))
    monkeypatch.setenv("MTGACOACH_GAME_DEVICE", "desktop")
    actual_invitation = dr.invitation
    monkeypatch.setattr(dr, "invitation", lambda rows, snapshot: actual_invitation(rows, snapshot, now=NOW))
    history = MatchHistory()
    for rec in records(0, 20):
        history.add_record(rec)
    host = _PostMatchMixin()
    host.ui = Mock()
    host.speak_advice = Mock()
    host._offer_deck_building(history, rec)
    host._offer_deck_building(history, rec)
    host.ui.advice.assert_called_once()
    host.speak_advice.assert_called_once()
    assert "Would you like help" in host.ui.advice.call_args.args[0]


def test_goldfish_reads_current_public_deck_data(monkeypatch):
    import arenamcp.mtggoldfish as goldfish

    response = Mock()
    response.content = b"""<script>
    initializeDeckComponents('display-id', '123', "4%20Spell%0A56%20Island%0Asideboard%0A2%20Answer%0A", '', false);
    </script>"""
    monkeypatch.setattr(goldfish.requests, "get", Mock(return_value=response))
    client = goldfish.MTGGoldfishClient()
    client._read_cache = Mock(return_value={"mainboard": [], "sideboard": []})
    client._write_cache = Mock()
    deck = client.get_deck_list("https://www.mtggoldfish.com/deck/123")
    assert deck["mainboard"] == ["4 Spell", "56 Island"]
    assert deck["sideboard"] == ["2 Answer"]
    assert "Sideboard\n2 Answer" in arena_export(deck)
    client._write_cache.assert_called_once()


def test_invitation_pools_only_same_deck_autopilot_games():
    """Real logged case: evidence bundle 00651f77… (2026-11-10 Ladder loss,
    autopilot enabled). The sample must count only games the autopilot played
    with the SAME deck as the game just ended — advisor-mode games, other
    decks, and pre-P1 records (autopilot unknown) measure other things."""
    noise = [
        # Advisor game (the player drove): same deck, must not count.
        MatchRecord(
            match_id="advisor",
            timestamp=(NOW - timedelta(minutes=1)).isoformat(),
            result="win",
            format_name="Ladder",
            deck_identity="deck-hash-Ladder",
            deck_name="Test Deck",
            autopilot=False,
        ),
        # Autopilot but a different deck.
        MatchRecord(
            match_id="other-deck",
            timestamp=(NOW - timedelta(minutes=2)).isoformat(),
            result="win",
            format_name="Ladder",
            deck_identity="other-hash",
            deck_name="Other Deck",
            autopilot=True,
        ),
        # Pre-P1 record: autopilot unknown, no identity.
        MatchRecord(
            match_id="legacy",
            timestamp=(NOW - timedelta(minutes=3)).isoformat(),
            result="win",
            format_name="Ladder",
        ),
    ]
    result = invitation(noise + records(0, 20), {"constructedClass": "Gold"}, now=NOW)

    assert result is not None
    assert (result.wins, result.losses) == (0, 20)
    assert result.deck == "Test Deck"
    assert "with Test Deck" in result.message


def test_invitation_needs_deck_identity_on_latest_game():
    """Pre-P1 history (no deck identity) has no per-deck sample: no invitation
    may quote a win rate it cannot attribute to a deck."""
    legacy = records(0, 20)
    for rec in legacy:
        rec.deck_identity = ""
        rec.autopilot = None
    assert invitation(legacy, {"constructedClass": "Gold"}, now=NOW) is None


def test_invitation_does_not_pool_identity_less_records_sharing_a_display_name():
    """Reviewer repro (Dotty 2026-11-11, offscreen history): autopilot=True
    records with EMPTY deck identities and the same display name as the latest
    game must not produce a 20-loss prompt — a shared display name is not a
    deck identity, so identity-less records never pool by name."""
    name_only = records(0, 20)
    for rec in name_only:
        rec.deck_identity = ""  # identity tracking wrote nothing
        rec.autopilot = True  # the autopilot definitely played them
    assert invitation(name_only, {"constructedClass": "Gold"}, now=NOW) is None


def test_invitation_ignores_name_only_history_even_when_the_latest_game_has_an_identity():
    """Same repro with the latest game identified: name-only records carrying
    the SAME display name still never enter the pool, so a 20-loss streak of
    unidentified games cannot fire the invitation for an identified deck."""
    pool = records(0, 20)
    for rec in pool:
        rec.deck_identity = ""
        rec.autopilot = True
    latest = MatchRecord(
        match_id="latest",
        timestamp=NOW.isoformat(),
        result="loss",
        format_name="Ladder",
        deck_identity="deck-hash-Ladder",
        deck_name="Test Deck",
        autopilot=True,
    )
    assert invitation(pool + [latest], {"constructedClass": "Gold"}, now=NOW) is None
