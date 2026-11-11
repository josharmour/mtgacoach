from datetime import datetime, timezone

from arenamcp.match_history import MatchRecord
from arenamcp.session_recap import SessionRecaps


def record(mid, result="loss"):
    return MatchRecord(match_id=mid, timestamp=datetime.now(timezone.utc).isoformat(), result=result)


def test_crash_recovery_does_not_require_close_handler(tmp_path):
    path = tmp_path / "recaps.json"
    store = SessionRecaps(path)
    old = record("old")
    store.begin([old])
    new = record("new", "win")
    restarted = SessionRecaps(path)
    restarted.begin([old, new])
    assert "1 recorded games: 1 wins" in restarted.pending()["text"]


def test_display_is_not_acknowledgement_and_explicit_done_is(tmp_path):
    path = tmp_path / "recaps.json"
    store = SessionRecaps(path)
    store.begin([])
    rows = [record("a"), record("b")]
    recap = store.capture(rows)
    assert store.pending() == recap
    assert SessionRecaps(path).pending() == recap
    store.acknowledge(recap["id"])
    assert store.capture(rows) is None
    assert SessionRecaps(path).pending() is None
    assert store.capture(rows + [record("c")]) is not None


def test_empty_and_historical_records_do_not_offer(tmp_path):
    store = SessionRecaps(tmp_path / "recaps.json")
    store.begin([])
    assert store.capture([]) is None
    old = MatchRecord(match_id="imported", timestamp="2020-01-01T00:00:00", result="win")
    assert store.capture([old]) is None


def test_repeated_capture_deduplicates_and_excludes_personal_data(tmp_path):
    store = SessionRecaps(tmp_path / "recaps.json")
    store.begin([])
    row = record("a")
    row.opponent_name = "private opponent"
    row.replay_path = "/private/path"
    row.coach_rating = 7
    recap = store.capture([row])
    assert store.capture([row]) == recap
    assert len(store.data["pending"]) == 1
    assert "private" not in store.path.read_text()
    assert "7.0/10" in recap["text"]


def test_bad_storage_recovers(tmp_path):
    path = tmp_path / "recaps.json"
    path.write_text("broken")
    store = SessionRecaps(path)
    store.begin([])
    assert store.capture([record("a")])


def test_desktop_offer_survives_unseen_close_and_done_acknowledges(qapp, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QWidget

    from arenamcp.desktop.session_recap import SessionRecapController

    monkeypatch.setattr("arenamcp.desktop.session_recap.Path.home", lambda: tmp_path)
    rows = []
    monkeypatch.setattr(SessionRecapController, "records", staticmethod(lambda: rows))
    window = QWidget()
    window._closed = False
    controller = SessionRecapController(window)
    rows.append(record("one"))
    assert controller.closing() is False
    recap = controller.store.pending()
    assert recap is not None
    controller.dialog.reject()  # Displaying or closing the offer is not acknowledgement.
    assert controller.closing() is True  # Fast second close is allowed.
    assert controller.store.pending() is not None
    controller.review(recap)
    controller.dialog.buttons()[0].click()  # Explicit Done.
    assert controller.store.pending() is None
    window._closed = True
    window.close()


def test_recovers_completed_three_loss_analysis_only_for_session(tmp_path):
    store = SessionRecaps(tmp_path / "recaps.json")
    store.begin([])
    rows = [record("third-g2")]
    review = {
        "matches": ["first", "second", "third"],
        "recommendation": "Review removal slots",
        "analysis": "Evidence remains incomplete.",
    }
    unrelated = {"matches": ["other"], "analysis": "Do not include"}
    recap = store.capture(rows, [review, unrelated])
    assert "Review removal slots" in recap["text"]
    assert "Evidence remains incomplete" in recap["text"]
    assert "Do not include" not in recap["text"]
    store.acknowledge(recap["id"])
    assert store.capture(rows, [review, unrelated]) is None
