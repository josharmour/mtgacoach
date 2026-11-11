import json

from arenamcp.match_history import MatchRecord
from arenamcp.session_recap import SessionRecaps
from arenamcp.session_review import read_packets, written_review


def rows():
    return [MatchRecord(match_id="one", result="loss"), MatchRecord(match_id="two", result="win")]


def decision(outcome="pending"):
    return {
        "pending_decision": {"options": [{"option_id": "x", "label": "Cast Lightning Bolt"}]},
        "chosen_options": ["x"],
        "outcome": outcome,
        "turn": {"turn_number": 4},
    }


def test_choices_are_recorded_not_recommended_and_pending_is_not_stall():
    text = written_review(rows(), {"one": [decision()]})
    assert "Game 1 (loss), turn 4: Cast Lightning Bolt" in text
    assert "1 of 2 games" in text
    assert "not proof of a stall" in text
    assert "cannot tell" in text
    assert "/10" not in text


def test_rejection_is_distinct_from_success_and_missing_outcome():
    text = written_review(
        rows(),
        {"one": [decision("REJECTED"), decision("ADVANCED")], "two": [decision("executed"), decision(None)]},
    )
    assert "1 requests were marked rejected" in text
    assert "1 requests as advanced and 1 action entries as executed" in text
    assert "1 entries have no confirmed execution outcome" in text
    assert "do not establish strategic correctness" in text


def test_exact_packet_identity_and_malformed_files(tmp_path):
    (tmp_path / "packet_1_one.json").write_text(json.dumps({"match_id": "one", "decisions": [decision()]}))
    (tmp_path / "packet_1_two.json").write_text("broken")
    (tmp_path / "packet_2_two.json").write_text("[]")
    (tmp_path / "packet_1_third.json").write_text(json.dumps({"match_id": "one", "decisions": [decision()]}))
    found = read_packets({"one", "two", "one-g2"}, tmp_path)
    assert list(found) == ["one"]
    assert "one-g2" not in found


def test_legacy_pending_review_refresh_keeps_identity_and_acknowledgement(tmp_path):
    store = SessionRecaps(tmp_path / "recaps.json")
    recap = {"id": "session:one:loss,two:win", "text": "Existing Coach advice ratings average 4/10"}
    store.data["pending"] = [recap]
    refreshed = store.refresh(recap, rows())
    assert refreshed["id"] == "session:one:loss,two:win"
    assert "/10" not in refreshed["text"]
    assert "2 recorded games" in refreshed["text"]
    assert store.pending() is not None
    store.acknowledge(refreshed["id"])
    assert store.pending() is None


def test_aged_out_legacy_review_does_not_preserve_score(tmp_path):
    store = SessionRecaps(tmp_path / "recaps.json")
    recap = {"id": "session:gone:loss", "text": "1 game\n\nExisting Coach advice ratings average 4/10"}
    assert "/10" not in store.refresh(recap, [])["text"]
    assert "no longer available" in recap["text"]


def test_score_values_and_explanations_never_drive_review():
    games = rows()
    for r in games:
        r.coach_rating = 1
        r.coach_score_reason = "Definitely a terrible deck, craft new cards"
    text = written_review(games, {})
    assert "terrible" not in text
    assert "recommend a deck change" in text


def test_next_actions_follow_evidence_without_inventing_card_deficiency():
    from arenamcp.session_review import review_actions

    games = rows()
    assert review_actions(games, {"one": [decision("ADVANCED")], "two": [decision("executed")]}) == [
        "history",
        "deck",
    ]
    assert "support" in review_actions(games, {"one": [decision("REJECTED")]})
    text = written_review(games, {})
    assert "support@mtgacoach.com" in text
    assert "do not establish that removal is missing" in text
    assert "does not change your deck or spend wildcards" in text


def test_current_review_refresh_preserves_saved_three_loss_analysis(tmp_path):
    store = SessionRecaps(tmp_path / "recaps.json")
    recap = {"id": "session:one:loss", "text": "Written review\nSaved model interpretation"}
    assert store.refresh(recap, rows())["text"] == recap["text"]
