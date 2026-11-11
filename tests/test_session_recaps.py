"""Session recap offers are gated on completed three-loss reviews (R1).

Owner-reported regression (2026-11-11): every close/start forced a recap even
for a single win. Offers now require a completed, non-superseded three-loss
review from the authoritative loss-streak store, dismissal is durable per
streak across restarts, and legacy/superseded pending data never pops up.
"""

import json
from datetime import datetime, timezone

from arenamcp.match_history import MatchRecord
from arenamcp.session_recap import SessionRecaps


def record(mid, result="loss"):
    return MatchRecord(match_id=mid, timestamp=datetime.now(timezone.utc).isoformat(), result=result)


def losses(*mids):
    return [record(m) for m in mids]


def report(matches=("a", "b", "c"), rec="Review removal slots", analysis="Evidence remains incomplete."):
    """Shape of a completed loss_streak job report (authoritative eligibility evidence)."""
    return {"matches": list(matches), "recommendation": rec, "analysis": analysis}


def make_controller(monkeypatch, tmp_path, rows, reports):
    from PySide6.QtWidgets import QWidget

    from arenamcp.desktop.session_recap import SessionRecapController

    monkeypatch.setattr("arenamcp.desktop.session_recap.Path.home", lambda: tmp_path)
    monkeypatch.setattr(SessionRecapController, "records", staticmethod(lambda: list(rows)))
    monkeypatch.setattr(SessionRecapController, "reviews", staticmethod(lambda: list(reports)))
    window = QWidget()
    window._closed = False
    return SessionRecapController(window), window


def test_ordinary_sessions_never_offer(tmp_path):
    """Zero/one/two losses and wins close normally; a still-pending analysis queues nothing."""
    store = SessionRecaps(tmp_path / "recaps.json")
    store.begin([])
    assert store.capture([], []) is None
    assert store.capture([record("w1", "win"), record("w2", "win")], []) is None
    assert store.capture(losses("l1"), []) is None
    assert store.capture(losses("l1", "l2"), []) is None
    # Three losses whose analysis has not completed yet: no generic popup while waiting.
    assert store.capture(losses("l1", "l2", "l3"), []) is None
    assert store.pending([]) is None


def test_qualifying_completed_review_offers(tmp_path):
    store = SessionRecaps(tmp_path / "recaps.json")
    store.begin([])
    recap = store.capture(losses("a", "b", "c"), [report()])
    assert recap is not None
    assert recap["id"].startswith("review:")
    assert "Review removal slots" in recap["text"]
    assert "Evidence remains incomplete" in recap["text"]
    assert "Nothing has been sent to support" in recap["text"]


def test_invalid_or_uncovered_review_evidence_does_not_offer(tmp_path):
    """Unknown/manual games never recorded, wrong match counts and duplicates are not evidence."""
    store = SessionRecaps(tmp_path / "recaps.json")
    store.begin([])
    rows = losses("a", "b", "c")
    # Matches the history never recorded (manual/unknown games are not session evidence).
    assert store.capture(rows, [report(("gone1", "gone2", "gone3"))]) is None
    # Not a three-loss review.
    assert store.capture(rows, [report(("a", "b"))]) is None
    assert store.capture(rows, [report(("a", "a", "b"))]) is None
    assert store.capture(rows, [{}]) is None
    assert store.capture(rows, ["not-a-dict"]) is None


def test_malformed_report_evidence_fails_closed(tmp_path):
    """Verifier reproduction (2026-11-11): matches=123 crashed startup; 'abc' posed as three matches."""
    store = SessionRecaps(tmp_path / "recaps.json")
    store.begin([])
    rows = losses("a", "b", "c")
    malformed = [
        {"matches": 123},
        {"matches": "abc"},
        {"matches": [1, 2, 3]},
        {"matches": ["a", "", "c"]},
        {"matches": ["a", "b", None]},
        {"matches": ["a", "b", "c", "d"]},
    ]
    for bad in malformed:
        assert store.capture(rows, [bad]) is None
    # Malformed entries are skipped; a well-formed report beside them still qualifies.
    recap = store.capture(rows, malformed + [report()])
    assert recap is not None
    # The pending-verification path fails closed on the same malformed evidence.
    assert store.pending(malformed) is None
    assert store.pending(malformed + [report()])["id"] == recap["id"]


def test_controller_construction_survives_malformed_report_evidence(qapp, monkeypatch, tmp_path):
    """A malformed completed report must not raise while the controller starts up."""
    rows = losses("a", "b", "c")
    malformed = [{"matches": 123}, {"matches": "abc"}]
    # The first launch seeds the active session; the second replays capture
    # against it, which is where malformed evidence used to raise TypeError.
    for _ in range(2):
        controller, window = make_controller(monkeypatch, tmp_path, rows, malformed)
        try:
            assert controller.dialog is None
            assert controller.closing() is True
        finally:
            window.close()
            window.deleteLater()


def test_bo3_game_rows_are_not_matches(tmp_path):
    """Three game-history rows of one BO3 match never infer a streak on their own."""
    store = SessionRecaps(tmp_path / "recaps.json")
    store.begin([])
    bo3_rows = [record("m1-g1"), record("m1-g2"), record("m1-g3")]
    assert store.capture(bo3_rows, []) is None
    # Even a review naming m1 does not qualify while its co-matches are absent...
    assert store.capture(bo3_rows, [report(("m1", "x", "y"))]) is None
    # ...and a game row does map to its match when covering a real review.
    rows = [record("first"), record("second"), record("third-g2")]
    recap = store.capture(rows, [report(("first", "second", "third"))])
    assert recap is not None


def test_repeated_start_close_and_restart_deduplicate(tmp_path):
    path = tmp_path / "recaps.json"
    store = SessionRecaps(path)
    store.begin([])
    rows = losses("a", "b", "c")
    first = store.capture(rows, [report()])
    again = store.capture(rows, [report()])
    assert again == first
    assert len(store.data["pending"]) == 1
    restarted = SessionRecaps(path)
    restarted.begin(rows, [report()])
    assert restarted.pending([report()])["id"] == first["id"]
    assert len(restarted.data["pending"]) == 1


def test_acknowledged_review_is_not_reoffered_by_unrelated_games(tmp_path):
    path = tmp_path / "recaps.json"
    store = SessionRecaps(path)
    store.begin([])
    rows = losses("a", "b", "c")
    recap = store.capture(rows, [report()])
    store.acknowledge(recap["id"])
    assert store.capture(rows + [record("z", "win")], [report()]) is None
    assert store.capture(rows + [record("z", "win")], [report()]) is None
    # A distinct qualifying streak may still prompt after dismissal of streak A.
    fresh = SessionRecaps(path)
    fresh.begin(rows + [record("z", "win")])
    assert fresh.capture(rows + losses("d", "e", "f"), [report(), report(("d", "e", "f"))]) is not None


def test_unseen_review_recovery_after_close(tmp_path):
    """Analysis completes after close; the next launch offers it instead of forgetting it."""
    path = tmp_path / "recaps.json"
    store = SessionRecaps(path)
    store.begin([])
    rows = losses("a", "b", "c")
    assert store.capture(rows, []) is None  # closed while analysis was still pending
    # Review completes while the app is closed; relaunch recovers it.
    restarted = SessionRecaps(path)
    restarted.begin(rows, [report()])
    assert restarted.pending([report()]) is not None


def test_superseded_corrected_jobs_lose_eligibility_but_distinct_streaks_offer(tmp_path):
    path = tmp_path / "recaps.json"
    store = SessionRecaps(path)
    store.begin([])
    rows = losses("a", "b", "c")
    recap = store.capture(rows, [report()])
    assert recap is not None
    # Result correction supersedes the job: its report disappears from completed reviews.
    corrected_reviews = [report(("d", "e", "f"))]
    assert store.pending(corrected_reviews) is None  # stale pending data must not pop up
    # The corrected-away streak no longer qualifies...
    assert store.capture(rows + losses("d", "e", "f"), corrected_reviews) is not None


def test_legacy_pending_recaps_do_not_pop_up_after_upgrade(tmp_path):
    path = tmp_path / ".arenamcp" / "session_recaps.json"
    path.parent.mkdir(parents=True)
    legacy = {
        "pending": [
            {
                # Pre-gating identity format without verifiable qualifying evidence.
                "id": "deadbeef:a:loss,b:loss,c:loss",
                "text": "...advice ratings average...",
                "actions": [],
            }
        ]
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")
    store = SessionRecaps(path)
    assert store.pending([report()]) is None


def test_crash_recovery_tracks_sessions_but_only_offers_qualifying_reviews(tmp_path):
    path = tmp_path / "recaps.json"
    old = record("old")
    store = SessionRecaps(path)
    store.begin([old])
    killed_mid_session = record("new", "win")
    restarted = SessionRecaps(path)
    restarted.begin([old, killed_mid_session], [])
    # No qualifying review yet: recovery rebuilds tracking silently.
    assert restarted.pending([]) is None
    assert restarted.data["active"]["baseline"] == ["old", killed_mid_session.match_id]
    # With a completed three-loss review the recovered session does offer.
    third = SessionRecaps(path)
    third.begin([old] + losses("a", "b", "c"), [report()])
    assert third.pending([report()]) is not None


def test_bad_storage_recovers(tmp_path):
    path = tmp_path / "recaps.json"
    path.write_text("broken")
    store = SessionRecaps(path)
    store.begin([])
    assert store.capture(losses("a", "b", "c"), [report()])


def test_display_is_not_acknowledgement_and_explicit_done_is(tmp_path):
    path = tmp_path / "recaps.json"
    store = SessionRecaps(path)
    store.begin([])
    rows = losses("a", "b", "c")
    recap = store.capture(rows, [report()])
    assert store.pending([report()]) == recap
    assert SessionRecaps(path).pending([report()]) == recap  # survives restart unacknowledged
    store.acknowledge(recap["id"])
    assert store.capture(rows, [report()]) is None
    assert SessionRecaps(path).pending([report()]) is None


def test_repeated_capture_deduplicates_and_excludes_personal_data(tmp_path):
    store = SessionRecaps(tmp_path / "recaps.json")
    store.begin([])
    rows = losses("a", "b", "c")
    for row in rows:
        row.opponent_name = "private opponent"
        row.replay_path = "/private/path"
        row.coach_rating = 7
    recap = store.capture(rows, [report()])
    assert store.capture(rows, [report()]) == recap
    assert len(store.data["pending"]) == 1
    assert "private" not in store.path.read_text()
    assert "/10" not in recap["text"]
    assert "not have enough saved decision evidence" in recap["text"]


def test_desktop_nonqualifying_close_returns_true_and_opens_no_dialog(qapp, monkeypatch, tmp_path):
    scenarios = [
        [],
        [record("w1", "win")],
        losses("l1"),
        [record("l1", "draw")],
    ]
    for n, rows in enumerate(scenarios):
        controller, window = make_controller(monkeypatch, tmp_path / f"s{n}", rows, [])
        try:
            assert controller.closing() is True
            assert controller.dialog is None
        finally:
            window.close()
            window.deleteLater()


def test_dismiss_button_keeps_streak_silent_across_restart_but_new_streak_offers(qapp, monkeypatch, tmp_path):
    """Owner regression: dismiss A via the actual button, relaunch stays silent, B may offer."""
    rows = losses("a", "b", "c")
    reports = [report()]
    controller, window = make_controller(monkeypatch, tmp_path, rows, reports)
    try:
        assert controller.closing() is False
        box = controller.dialog
        assert box is not None
        recap = controller.store.pending(reports)
        assert recap is not None
        identity = recap["id"]
        dismiss = next(b for b in box.buttons() if b.text() == "Don't show this recap again")
        dismiss.click()
        # Durable per-streak acknowledgement persisted to disk.
        stored = json.loads((tmp_path / ".arenamcp" / "session_recaps.json").read_text())
        assert identity in stored["acknowledged"]

        # Reconstruct store/controller exactly as on relaunch.
        controller2, window2 = make_controller(monkeypatch, tmp_path, rows, reports)
        try:
            assert controller2.store.pending(controller2.reviews()) is None
            controller2.offer_previous()
            assert controller2.dialog is None  # dismissed streak A stays silent after restart
            # A new distinct qualifying streak may prompt again.
            rows.extend(losses("d", "e", "f"))
            reports.append(report(("d", "e", "f")))
            assert controller2.closing() is False
            assert controller2.dialog is not None
        finally:
            window2.close()
            window2.deleteLater()
    finally:
        window.close()
        window.deleteLater()


def test_acknowledgement_outlives_review_store_retention_boundary(qapp, monkeypatch, tmp_path):
    """Verifier reproduction (2026-11-11): eleven real-button dismissals must all stick.

    The loss-streak store retains about twenty completed jobs; trimming the
    acknowledgement list to ten let the first dismissed streak reoffer and
    reopen a dialog after reconstruction.
    """
    rows: list = []
    reports: list = []
    for index in range(11):
        triplet = (f"s{index}a", f"s{index}b", f"s{index}c")
        rows.extend(losses(*triplet))
        reports.append(report(triplet))
        controller, window = make_controller(monkeypatch, tmp_path, rows, reports)
        try:
            assert controller.closing() is False
            box = controller.dialog
            assert box is not None
            dismiss = next(b for b in box.buttons() if b.text() == "Don't show this recap again")
            dismiss.click()
        finally:
            window.close()
            window.deleteLater()
    stored = json.loads((tmp_path / ".arenamcp" / "session_recaps.json").read_text())
    assert len(stored["acknowledged"]) == 11
    # Relaunch: every dismissed streak stays silent, none reoffers a dialog.
    controller, window = make_controller(monkeypatch, tmp_path, rows, reports)
    try:
        controller.offer_previous()
        assert controller.dialog is None
        assert controller.closing() is True
    finally:
        window.close()
        window.deleteLater()


def test_desktop_second_close_allowed_and_done_button_acknowledges(qapp, monkeypatch, tmp_path):
    """Displaying or closing the offer never acknowledges; only the Done button does."""
    from PySide6.QtWidgets import QDialogButtonBox

    rows = losses("a", "b", "c")
    reports = [report()]
    controller, window = make_controller(monkeypatch, tmp_path / "done", rows, reports)
    try:
        assert controller.closing() is False
        recap = controller.store.pending(reports)
        assert recap is not None
        controller.dialog.reject()  # Closing the offer is not acknowledgement.
        assert controller.closing() is True  # A fast second close always works.
        assert controller.store.pending(reports) == recap
        controller.review(recap)
        done = next(b for b in controller.dialog.findChild(QDialogButtonBox).buttons() if b.text() == "Done")
        done.click()
        assert controller.store.pending(reports) is None
    finally:
        window.close()
        window.deleteLater()


def test_review_actions_navigate_or_prepare_draft_without_acknowledging(qapp, monkeypatch, tmp_path):
    from types import SimpleNamespace

    rows = losses("a", "b", "c")
    reports = [report()]
    controller, window = make_controller(monkeypatch, tmp_path / "nav", rows, reports)
    try:
        calls = []
        window._show_performance_view = lambda: calls.append("history")
        window._show_decks_view = lambda: calls.append("decks")
        window.decks_tab = SimpleNamespace(open_builder=lambda: calls.append("builder"))
        monkeypatch.setattr("arenamcp.support.contact_josh", lambda parent, summary: calls.append(summary))
        recap = {
            "id": "review:a,b,c",
            "text": "Recorded rejection; cause unknown.",
            "actions": ["history", "deck", "support"],
        }
        controller.store.data["pending"] = [recap]
        controller.review(recap)
        assert calls == []
        controller.take_action("support", recap, controller.dialog)
        assert "not a verified bug diagnosis" in calls[0]
        controller.take_action("deck", recap, controller.dialog)
        assert calls[-2:] == ["decks", "builder"]
        controller.review(recap)
        controller.take_action("history", recap, controller.dialog)
        assert calls[-1] == "history"
        # Navigation and drafting never consume the offer; it stays available.
        assert controller.store.pending(reports) == recap
    finally:
        window.close()
        window.deleteLater()


def test_desktop_offer_previous_filters_legacy_and_superseded_pending(qapp, monkeypatch, tmp_path):
    seeded = {
        "pending": [
            {
                # Pre-gating identity format without verifiable qualifying evidence.
                "id": "deadbeef:a:loss,b:loss,c:loss",
                "text": "...advice ratings average...",
                "actions": [],
            },
            {"id": "review:a,b,c", "text": "real recap text", "actions": []},
        ]
    }
    home = tmp_path / "mixed"
    (home / ".arenamcp").mkdir(parents=True)
    (home / ".arenamcp" / "session_recaps.json").write_text(json.dumps(seeded), encoding="utf-8")
    controller, window = make_controller(monkeypatch, home, losses("a", "b", "c"), [report()])
    try:
        controller.offer_previous()
        assert controller.dialog is not None
        # The verifiable recap was selected; the legacy entry was filtered out.
        selected = controller.store.pending(controller.reviews())
        assert selected is not None and selected["id"] == "review:a,b,c"
    finally:
        window.close()
        window.deleteLater()

    # Only unverifiable pending data: nothing pops up after the upgrade.
    home2 = tmp_path / "legacy-only"
    (home2 / ".arenamcp").mkdir(parents=True)
    (home2 / ".arenamcp" / "session_recaps.json").write_text(
        json.dumps({"pending": [seeded["pending"][0]]}), encoding="utf-8"
    )
    controller2, window2 = make_controller(monkeypatch, home2, losses("a", "b", "c"), [report()])
    try:
        controller2.offer_previous()
        assert controller2.dialog is None
    finally:
        window2.close()
        window2.deleteLater()
