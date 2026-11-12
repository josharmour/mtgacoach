"""Session recap offers are gated on completed three-loss reviews (R1).

Owner-reported regression (2026-11-11): every close/start forced a recap even
for a single win. Offers now require a completed, non-superseded three-loss
review from the authoritative loss-streak store, dismissal is durable per
streak across restarts, and legacy/superseded pending data never pops up.
"""

import hashlib
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


def test_bo3_game_rows_reach_the_recap_after_relaunch_baseline(tmp_path):
    """Coordinator integration repro (2026-11-11): history holding only the six
    BO3 game rows a-g1..c-g2 for completed review matches a,b,c qualified via
    _covers but capture selected games by exact match id, so after a relaunch's
    store.begin baselined every row the recap rendered zero games and looked up
    no packets. All six rows must reach written_review and the packet/action
    lookup, without adopting an unrelated match that merely shares a prefix.
    """
    path = tmp_path / "recaps.json"
    packets_dir = path.parent / "match_packets"
    packets_dir.mkdir()
    rows = [record(m) for m in ("a-g1", "a-g2", "b-g1", "b-g2", "c-g1", "c-g2", "ab-g1")]
    for row in rows:
        (packets_dir / f"packet_x_{row.match_id}.json").write_text(
            json.dumps({"match_id": row.match_id, "decisions": [{"outcome": "EXECUTED"}]}),
            encoding="utf-8",
        )
    reviews = [report(("a", "b", "c"))]
    first = SessionRecaps(path)
    first.begin([])
    assert first.capture(rows, reviews) is not None
    # Relaunch: begin() baselines every history row and stamps a new session;
    # the qualifying review must still render its six game rows (the unrelated
    # "ab-g1" match shares a prefix with "a" but is not part of the streak).
    relaunched = SessionRecaps(path)
    relaunched.begin(rows, reviews)
    recap = relaunched.capture(rows, reviews)
    assert recap is not None
    assert "across 6 recorded games" in recap["text"]
    assert "saved autoplay decision/action entries for 6 of 6 games" in recap["text"]
    assert recap["actions"] == ["history", "deck"]
    # Dismissal and correction behavior are unchanged on this identity.
    pending = relaunched.pending(reviews)
    assert pending is not None and pending["id"] == recap["id"]
    relaunched.acknowledge(recap["id"])
    assert relaunched.capture(rows, reviews) is None
    assert relaunched.pending([report(("d", "e", "f"))]) is None


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


# --- Legacy acknowledgement migration (R1 rework) -------------------------
# Pre-gating dismissals were saved as "<32-hex session uuid>:<id>:<result>,..."
# (optionally ":<16-hex review digest>"). They must keep suppressing their
# explicitly covered triplets after migration, without suppressing anything else.

LEGACY_SESSION = "a" * 32


def legacy_ack(matches, digest=None):
    body = ",".join(f"{m}:loss" for m in matches)
    return f"{LEGACY_SESSION}:{body}" + (f":{digest}" if digest else "")


def modern_identity(review):
    return "review:" + ",".join(review["matches"])


def synthetic_world(digest=None, extra_legacy=None, acknowledged_extra=()):
    """15 matches / 5 completed triplets in the exact saved legacy schema."""
    matches = [f"old-match-{i:02d}" for i in range(15)]
    reviews = [report(tuple(matches[i:i + 3])) for i in range(0, 15, 3)]
    acknowledged = [legacy_ack(matches, digest=digest), *acknowledged_extra]
    if extra_legacy is not None:
        acknowledged.append(extra_legacy)
    state = {
        "active": {"id": "b" * 32, "started": "2026-11-12T03:06:00+00:00", "baseline": list(matches)},
        "acknowledged": acknowledged,
        "pending": [{
            "id": modern_identity(reviews[1]),
            "session_id": "b" * 32,
            "text": "Already queued modern offer",
        }],
    }
    return matches, reviews, state


def seed_store(tmp_path, state):
    path = tmp_path / "recaps.json"
    path.write_text(json.dumps(state))
    return path


def world_rows(matches):
    return [record(m) for m in matches]


def test_legacy_covered_triplets_stay_silent_and_migrate_atomically(tmp_path):
    """The coordinator's canonical scenario: one legacy ack, 15 matches, 5 triplets."""
    digest = hashlib.sha256(b"x").hexdigest()[:16]
    matches, reviews, state = synthetic_world(digest=digest)
    path = seed_store(tmp_path, state)
    store = SessionRecaps(path)
    store.begin(world_rows(matches), reviews)
    # Already-queued modern pending for a legacy-covered triplet is removed.
    assert store.pending(reviews) is None
    saved = json.loads(path.read_text())
    # Original legacy entry preserved; modern identities adopted; pending cleaned.
    assert legacy_ack(matches, digest=digest) in saved["acknowledged"]
    assert all(modern_identity(r) in saved["acknowledged"] for r in reviews)
    assert saved["pending"] == []
    # Repeated start/close/relaunch stays silent.
    for _ in range(3):
        SessionRecaps(path).begin(world_rows(matches), reviews)
        assert store.pending(reviews) is None


def test_legacy_ack_without_digest_suffix_also_migrates(tmp_path):
    matches, reviews, state = synthetic_world(digest=None)
    path = seed_store(tmp_path, state)
    store = SessionRecaps(path)
    store.begin(world_rows(matches), reviews)
    assert store.pending(reviews) is None
    saved = json.loads(path.read_text())
    assert legacy_ack(matches) in saved["acknowledged"]
    assert all(modern_identity(r) in saved["acknowledged"] for r in reviews)


def test_first_modern_ack_honoured_remaining_legacy_triplets_not_reoffered(tmp_path):
    """First modern review ack saved correctly; the other four stay silent via legacy."""
    matches, reviews, state = synthetic_world(digest=None)
    path = seed_store(tmp_path, state)
    store = SessionRecaps(path)
    store.begin(world_rows(matches), reviews)
    for review in reviews[1:]:
        assert store.capture(world_rows(review["matches"]), reviews) is None
        assert store.pending(reviews) is None


def test_new_triplet_absent_from_legacy_ack_still_offers(tmp_path):
    matches = [f"old-match-{i:02d}" for i in range(12)]
    new_review = report(("fresh-a", "fresh-b", "fresh-c"))
    reviews = [report(tuple(matches[i:i + 3])) for i in range(0, 12, 3)] + [new_review]
    state = {
        "active": {"id": "b" * 32, "started": "2026-11-12T03:06:00+00:00", "baseline": matches},
        "acknowledged": [legacy_ack(matches)],
        "pending": [],
    }
    path = seed_store(tmp_path, state)
    store = SessionRecaps(path)
    store.begin(world_rows(matches + ["fresh-a", "fresh-b", "fresh-c"]), reviews)
    recap = store.pending(reviews)
    assert recap is not None and recap["id"] == modern_identity(new_review)


def test_malformed_unrelated_and_partial_legacy_entries_do_not_suppress(tmp_path):
    triplet = ("m1", "m2", "m3")
    review = report(triplet)
    malformed = [
        "not-a-legacy-string",
        "abc:m1:loss,m2:loss,m3:loss",          # session uuid too short
        LEGACY_SESSION + ":m1:loss,m2:loss",     # pair without result colon
        LEGACY_SESSION + ":",                    # empty body
        LEGACY_SESSION + ":m1:loss:m2:loss:m3:loss",  # stray inner colons
        12345,                                   # non-string entry
    ]
    unrelated = legacy_ack(["other-1", "other-2", "other-3"])
    partial = legacy_ack(["m1", "m2"])           # two of three covered only
    for extra in [*malformed, unrelated, partial]:
        state = {
            "active": {"id": "b" * 32, "started": "2026-11-12T03:06:00+00:00", "baseline": list(triplet)},
            "acknowledged": [extra],
            "pending": [],
        }
        path = seed_store(tmp_path, state)
        store = SessionRecaps(path)
        store.begin(world_rows(triplet), [review])
        recap = store.pending([review])
        assert recap is not None and recap["id"] == modern_identity(review), extra


def test_bo3_game_row_coverage_boundaries(tmp_path):
    # Complete explicit game-row coverage suppresses; partial does not; three
    # game rows of one match never stand in for three matches; a game row of a
    # longer id never covers a shorter prefix match.
    complete = legacy_ack(["ma-g1", "ma-g2", "ma-g3", "mb-g1", "mb-g2", "mb-g3", "mc-g1", "mc-g2", "mc-g3"])
    partial_rows = legacy_ack(["ma-g1", "ma-g2", "ma-g3"])
    prefix_trap = legacy_ack(["abc-g1"])         # must not cover match "ab"
    for extra, expect_offer in [(complete, False), (partial_rows, True), (prefix_trap, True)]:
        triplet = ("ma", "mb", "mc")
        review = report(triplet)
        state = {
            "active": {"id": "b" * 32, "started": "2026-11-12T03:06:00+00:00", "baseline": list(triplet)},
            "acknowledged": [extra],
            "pending": [],
        }
        path = seed_store(tmp_path, state)
        store = SessionRecaps(path)
        store.begin(world_rows(triplet), [review])
        recap = store.pending([review])
        assert (recap is not None) == expect_offer, extra


def test_modern_acks_remain_stable_across_migration_and_new_dismissals(tmp_path):
    digest = hashlib.sha256(b"x").hexdigest()[:16]
    matches, reviews, state = synthetic_world(digest=digest)
    path = seed_store(tmp_path, state)
    store = SessionRecaps(path)
    store.begin(world_rows(matches), reviews)
    assert store.pending(reviews) is None
    # A genuinely new streak offers; dismissing it must not disturb migrated state.
    new_review = report(("new-a", "new-b", "new-c"))
    rows = world_rows(matches + ["new-a", "new-b", "new-c"])
    recap = store.capture(rows + [], reviews + [new_review])
    assert recap is not None and recap["id"] == modern_identity(new_review)
    store.acknowledge(recap["id"])
    saved = json.loads(path.read_text())
    assert legacy_ack(matches, digest=digest) in saved["acknowledged"]
    assert all(modern_identity(r) in saved["acknowledged"] for r in reviews)
    assert modern_identity(new_review) in saved["acknowledged"]
    # Relaunch: everything acknowledged stays silent.
    fresh = SessionRecaps(path)
    fresh.begin(rows, reviews + [new_review])
    assert fresh.pending(reviews + [new_review]) is None
