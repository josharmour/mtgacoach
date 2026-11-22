"""Regression tests for between-match coaching brief and configurable auto-queue loss safeguard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from arenamcp.between_match_coach import summarize_between_matches
from arenamcp.match_history import MatchRecord
from arenamcp.standalone_auto_queue import AUTO_QUEUE_LOSS_LIMIT, _AutoQueueMixin


def _rec(
    idx: int,
    result: str,
    *,
    deck_id: str,
    deck_name: str,
    hours_ago: float = 1.0,
    now: datetime,
    account: str = "acct1",
) -> MatchRecord:
    ts = (now - timedelta(hours=hours_ago)).isoformat()
    return MatchRecord(
        match_id=f"m-{idx}",
        timestamp=ts,
        result=result,
        deck_identity=deck_id,
        deck_name=deck_name,
        format_name="Ladder",
        autopilot=True,
        local_account_id=account,
    )


def test_winning_deck_on_three_loss_skid_recommends_keep_queueing():
    now = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)
    recs = [
        _rec(i, "win", deck_id="id-a", deck_name="Deck Alpha", hours_ago=20 - i * 0.5, now=now)
        for i in range(11)
    ] + [
        _rec(100 + i, "loss", deck_id="id-a", deck_name="Deck Alpha", hours_ago=5 - i * 0.5, now=now)
        for i in range(9)
    ]
    brief = summarize_between_matches(recs, recs[-1], consecutive_losses=3, now=now)
    assert brief is not None
    assert brief.action == "keep_queueing"
    assert brief.suggest_deck_building is False
    assert "Keep queueing Deck Alpha" in brief.headline
    assert "No deck switch needed." in brief.detail


def test_struggling_deck_with_stronger_existing_deck_recommends_switch_deck():
    now = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)
    # Deck Alpha: 12W-10L (54.5%)
    recs = [
        _rec(
            i, "win" if i < 12 else "loss", deck_id="id-a", deck_name="Deck Alpha", hours_ago=30 - i, now=now
        )
        for i in range(22)
    ]
    # Deck Beta: 2W-9L (18.2%)
    recs += [
        _rec(
            100 + i,
            "win" if i < 2 else "loss",
            deck_id="id-b",
            deck_name="Deck Beta",
            hours_ago=3.0 - i * 0.2,
            now=now,
        )
        for i in range(11)
    ]
    brief = summarize_between_matches(recs, recs[-1], consecutive_losses=3, now=now)
    assert brief is not None
    assert brief.action == "switch_deck"
    assert brief.suggest_deck_building is False
    assert brief.best_alternative is not None
    assert brief.best_alternative.deck_name == "Deck Alpha"
    assert "Switch back to Deck Alpha" in brief.headline
    assert "switch back before trying to build a new deck." in brief.detail


def test_struggling_deck_without_stronger_alternative_recommends_review_or_tune():
    now = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)
    recs = [
        _rec(
            i,
            "win" if i < 2 else "loss",
            deck_id="id-c",
            deck_name="Deck Gamma",
            hours_ago=4.0 - i * 0.3,
            now=now,
        )
        for i in range(10)
    ]
    brief = summarize_between_matches(recs, recs[-1], consecutive_losses=3, now=now)
    assert brief is not None
    assert brief.action == "review_or_tune"
    assert brief.suggest_deck_building is True
    assert "Deck Gamma is struggling" in brief.headline


def test_small_sample_holds_off_unless_three_straight_losses_with_proven_alt():
    now = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)
    small = [
        _rec(1, "win", deck_id="id-d", deck_name="Deck Delta", hours_ago=1.0, now=now),
        _rec(2, "loss", deck_id="id-d", deck_name="Deck Delta", hours_ago=0.5, now=now),
        _rec(3, "loss", deck_id="id-d", deck_name="Deck Delta", hours_ago=0.2, now=now),
    ]
    brief = summarize_between_matches(small, small[-1], consecutive_losses=2, now=now)
    assert brief is not None
    assert brief.action == "small_sample"
    assert brief.suggest_deck_building is False

    # Control: 0W-3L start while Deck Alpha is proven 8W-4L (67%) -> suggest switching back
    proven_alt = [
        _rec(
            10 + i,
            "win" if i < 8 else "loss",
            deck_id="id-a",
            deck_name="Deck Alpha",
            hours_ago=24 - i,
            now=now,
        )
        for i in range(12)
    ]
    cold_start = [
        _rec(50 + i, "loss", deck_id="id-d", deck_name="Deck Delta", hours_ago=0.6 - i * 0.2, now=now)
        for i in range(3)
    ]
    brief2 = summarize_between_matches(proven_alt + cold_start, cold_start[-1], consecutive_losses=3, now=now)
    assert brief2 is not None
    assert brief2.action == "switch_deck"
    assert "Deck Alpha" in brief2.headline


class _FakeSettings:
    def __init__(self, initial: dict | None = None) -> None:
        self._data = dict(initial or {})

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value


class _FakeUI:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, str]] = []
        self.logs: list[str] = []
        self.advices: list[tuple[str, str]] = []

    def status(self, k: str, v: str) -> None:
        self.statuses.append((k, v))

    def log(self, msg: str) -> None:
        self.logs.append(msg)

    def advice(self, text: str, label: str) -> None:
        self.advices.append((text, label))


class _QueueHarness(_AutoQueueMixin):
    def __init__(self, loss_limit: int | None = None) -> None:
        init = {} if loss_limit is None else {"auto_queue_loss_limit": loss_limit}
        self.settings = _FakeSettings(init)
        self.ui = _FakeUI()
        self._auto_queue_enabled = True
        self._auto_queue_since = 100.0
        self._queue_restore_streak = False
        self._auto_queue_streak = {"since": 100.0, "through": 100.0, "losses": 0}

    def set_auto_queue(self, enabled: bool) -> bool:
        self._auto_queue_enabled = enabled
        return enabled


def test_configurable_auto_queue_loss_limit_5_and_disabled_0():
    # Limit = 5: 3 losses do NOT stop, 5th loss DOES stop
    h5 = _QueueHarness(loss_limit=5)
    for i in range(1, 5):
        ok = h5._record_queue_outcome(h5._auto_queue_streak, f"m{i}", "loss", 100.0 + i)
        assert ok is True
        assert h5._auto_queue_enabled is True
    ok5 = h5._record_queue_outcome(h5._auto_queue_streak, "m5", "loss", 105.0)
    assert ok5 is False
    assert h5._auto_queue_enabled is False
    assert any("5 consecutive match losses" in msg for msg in h5.ui.logs)

    # Limit = 0 (Off): 6 consecutive losses never stop auto-queue
    h0 = _QueueHarness(loss_limit=0)
    for i in range(1, 7):
        ok = h0._record_queue_outcome(h0._auto_queue_streak, f"m{i}", "loss", 100.0 + i)
        assert ok is True
        assert h0._auto_queue_enabled is True
    assert h0.ui.advices == []

    # Default (None) remains AUTO_QUEUE_LOSS_LIMIT (3)
    hdef = _QueueHarness(loss_limit=None)
    assert hdef._loss_limit() == AUTO_QUEUE_LOSS_LIMIT


def test_summarize_edge_cases_empty_dedup_account_naive_now_and_missing_deck():
    assert summarize_between_matches([]) is None

    # Timezone-naive `now` and naive record timestamps must not raise TypeError
    naive_now = datetime(2026, 11, 21, 18, 0)
    recs = [
        MatchRecord(
            match_id="dup-1",
            timestamp="2026-11-21T17:00:00",
            result="loss",
            deck_identity="",
            deck_name="",
            local_account_id="acct1",
        ),
        # Duplicate match_id with updated result should overwrite the earlier one
        MatchRecord(
            match_id="dup-1",
            timestamp="2026-11-21T17:05:00",
            result="win",
            deck_identity="",
            deck_name="",
            local_account_id="acct1",
        ),
        # Different account should be excluded
        MatchRecord(
            match_id="other-acct",
            timestamp="2026-11-21T17:10:00",
            result="loss",
            deck_identity="id-x",
            deck_name="Other Acct Deck",
            local_account_id="acct2",
        ),
    ]
    brief = summarize_between_matches(recs, recs[1], now=naive_now)
    assert brief is not None
    assert brief.current_deck.wins == 1
    assert brief.current_deck.losses == 0
    assert brief.headline[0].isupper()


def test_offer_deck_building_suppressed_for_keep_queueing_and_switch_deck():
    from arenamcp.between_match_coach import BetweenMatchBrief, DeckStats
    from arenamcp.standalone_postmatch import _PostMatchMixin

    class _PostHarness(_PostMatchMixin):
        def __init__(self) -> None:
            self.called = False

        @property
        def ui(self):
            self.called = True
            return _FakeUI()

    rec = MatchRecord(match_id="m1", result="loss", format_name="Ladder")
    for action in ("keep_queueing", "switch_deck"):
        h = _PostHarness()
        brief = BetweenMatchBrief(
            action=action,
            headline="h",
            detail="d",
            current_deck=DeckStats("id-a", "Deck A", 5, 5),
            session_wins=1,
            session_losses=3,
            consecutive_losses=3,
        )
        # Should return immediately without touching RankLog or UI
        h._offer_deck_building(history=None, record=rec, brief=brief)
        assert h.called is False


def test_loss_safeguard_dialog_suppresses_deck_builder_on_keep_and_switch(monkeypatch):
    from PySide6.QtWidgets import QApplication

    from arenamcp.desktop.main_window import MainWindow
    from arenamcp.standalone_auto_queue import LOSS_SAFEGUARD_LABEL, LOSS_SAFEGUARD_MESSAGE

    app = QApplication.instance() or QApplication([])
    assert app is not None

    now = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)
    proven_alt = [
        _rec(
            10 + i,
            "win" if i < 8 else "loss",
            deck_id="id-a",
            deck_name="Deck Alpha",
            hours_ago=24 - i,
            now=now,
        )
        for i in range(12)
    ]
    cold_start = [
        _rec(50 + i, "loss", deck_id="id-d", deck_name="Deck Delta", hours_ago=0.6 - i * 0.2, now=now)
        for i in range(3)
    ]
    small_switch = summarize_between_matches(
        proven_alt + cold_start, cold_start[-1], consecutive_losses=3, now=now
    )
    assert small_switch is not None and small_switch.action == "switch_deck"

    win = MainWindow.__new__(MainWindow)
    # Minimal stub for QMessageBox parent + coach_panel
    from PySide6.QtWidgets import QMainWindow

    QMainWindow.__init__(win)
    win.coach_panel = type(
        "_P",
        (),
        {"_clear_loss_safeguard": lambda self: None, "_open_safeguard_deck_suggestions": lambda self: None},
    )()

    # 1. Small-sample switch_deck brief suppresses "Suggest decks"
    win._on_loss_safeguard(small_switch.message, LOSS_SAFEGUARD_LABEL)
    btn_texts = [b.text() for b in win._loss_safeguard_dialog.buttons()]
    assert "Clear safeguard" in btn_texts
    assert "Suggest decks (cards & wildcards)" not in btn_texts
    win._loss_safeguard_dialog.close()

    # 2. Raw LOSS_SAFEGUARD_MESSAGE keeps "Suggest decks"
    win._on_loss_safeguard(LOSS_SAFEGUARD_MESSAGE, LOSS_SAFEGUARD_LABEL)
    btn_texts2 = [b.text() for b in win._loss_safeguard_dialog.buttons()]
    assert "Suggest decks (cards & wildcards)" in btn_texts2
    win._loss_safeguard_dialog.close()


def test_compact_coach_loss_limit_button_cycles():
    from arenamcp.desktop.compact_coach import CompactCoachPanel

    panel = CompactCoachPanel.__new__(CompactCoachPanel)
    panel._settings = _FakeSettings({})
    logged: list[str] = []
    panel.append_log = lambda msg, role="status": logged.append(msg)

    class _Btn:
        def __init__(self) -> None:
            self.text = ""

        def setText(self, t: str) -> None:
            self.text = t

    panel.loss_limit_btn = _Btn()
    panel._render_loss_limit_button()
    assert panel.loss_limit_btn.text == "Loss stop: 3 losses"

    expected = [
        (5, "Loss stop: 5 losses"),
        (7, "Loss stop: 7 losses"),
        (0, "Loss stop: Off"),
        (3, "Loss stop: 3 losses"),
    ]
    for exp_val, exp_label in expected:
        panel._cycle_auto_queue_loss_limit()
        assert panel._settings.get("auto_queue_loss_limit") == exp_val
        assert panel.loss_limit_btn.text == exp_label
    assert len(logged) == 4


def test_evaluate_queueable_decks_ranks_proven_winning_deck_above_noise_and_cold_deck():
    from arenamcp.between_match_coach import evaluate_queueable_decks

    now = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)
    # Deck A: proven 32W-29L (52.5%)
    recs = [
        _rec(
            i,
            "win" if i < 32 else "loss",
            deck_id="id-a",
            deck_name="Deck Alpha",
            hours_ago=48 - i * 0.5,
            now=now,
        )
        for i in range(61)
    ]
    # Deck B: struggling 2W-9L (18.2%)
    recs += [
        _rec(
            200 + i,
            "win" if i < 2 else "loss",
            deck_id="id-b",
            deck_name="Deck Beta",
            hours_ago=5 - i * 0.2,
            now=now,
        )
        for i in range(11)
    ]
    # Deck C: 1W-0L (100% tiny sample noise)
    recs.append(_rec(300, "win", deck_id="id-c", deck_name="Deck Noise", hours_ago=1.0, now=now))

    pairs = [
        {"deck_id": "d-b", "deck_name": "Deck Beta", "event_id": "Ladder", "deck_identity": "id-b"},
        {"deck_id": "d-c", "deck_name": "Deck Noise", "event_id": "Ladder", "deck_identity": "id-c"},
        {"deck_id": "d-a", "deck_name": "Deck Alpha", "event_id": "Ladder", "deck_identity": "id-a"},
        # Paid draft event must be ignored
        {
            "deck_id": "d-draft",
            "deck_name": "Draft Pile",
            "event_id": "PremierDraft_FDN",
            "deck_identity": "id-d",
        },
    ]
    plan = evaluate_queueable_decks(recs, pairs, now=now, account_id="acct1")
    assert len(plan.decks) == 3
    assert plan.recommended is not None
    assert plan.recommended.deck_name == "Deck Alpha"
    assert plan.decks[-1].deck_name == "Deck Beta"
    assert "Deck Alpha" in plan.summary and "Recommended" in plan.summary
    assert "Draft Pile" not in plan.summary

    empty_plan = evaluate_queueable_decks(recs, [], now=now)
    assert empty_plan.recommended is None
    assert empty_plan.decks == ()


def test_load_queueable_pairs_filters_to_free_constructed_events(tmp_path):
    import json

    from arenamcp.between_match_coach import load_queueable_pairs

    log_file = tmp_path / "Player.log"
    ev_free = json.dumps(
        {
            "request": json.dumps(
                {"EventName": "Ladder", "Summary": {"DeckId": "deck-1", "Name": "My Ladder Deck"}}
            )
        }
    )
    ev_paid = json.dumps(
        {
            "request": json.dumps(
                {"EventName": "QuickDraft_FDN", "Summary": {"DeckId": "deck-2", "Name": "Draft Deck"}}
            )
        }
    )
    log_file.write_text(f"==> EventSetDeckV3 {ev_free}\n==> EventSetDeckV3 {ev_paid}\n", encoding="utf-8")
    pairs = load_queueable_pairs(log_path=log_file)
    assert len(pairs) == 1
    assert pairs[0]["deck_id"] == "deck-1"
    assert pairs[0]["deck_name"] == "My Ladder Deck"
    assert pairs[0]["event_id"] == "Ladder"


def test_auto_queue_winning_mode_resolves_target_and_dedupes_advice(monkeypatch):
    from arenamcp.between_match_coach import QUEUE_PLAN_LABEL, QueueableDeck, QueuePlan

    deck = QueueableDeck("d-a", "Deck Alpha", "Ladder", "id-a", 32, 29)
    fake_plan = QueuePlan(decks=(deck,), recommended=deck, summary="Queueable decks (1 found)")
    monkeypatch.setattr(
        "arenamcp.between_match_coach.evaluate_live_queue_plan", lambda log_path=None: fake_plan
    )

    # Same mode returns None
    h_same = _QueueHarness()
    h_same.settings.set("auto_queue_mode", "same")
    assert h_same._resolve_winning_queue_target() is None

    # Winning mode returns target (with quest_mode=True) and announces once per target signature
    h_win = _QueueHarness()
    h_win.settings.set("auto_queue_mode", "winning")
    target = h_win._resolve_winning_queue_target()
    assert target == {"deck_id": "d-a", "deck_name": "Deck Alpha", "event_id": "Ladder", "quest_mode": True}
    assert len(h_win.ui.advices) == 1
    assert h_win.ui.advices[0][1] == QUEUE_PLAN_LABEL

    # Second call with same target does not duplicate advice
    assert h_win._resolve_winning_queue_target() == target
    assert len(h_win.ui.advices) == 1


def test_winning_target_arms_auto_queue_navigator_for_ladder_and_historic_ladder():
    from arenamcp.auto_queue import AutoQueueNavigator
    from arenamcp.between_match_coach import QueueableDeck

    for event_id in ("Ladder", "Historic_Ladder"):
        nav = AutoQueueNavigator(
            backend=object(),
            get_game_state=lambda: {},
            bridge=None,
        )
        nav.set_enabled(True)
        deck = QueueableDeck("deck-99", "Deck Alpha", event_id, "id-a", 32, 29)
        target = deck.to_target()
        assert nav.start_idle_grind(target, {}) is True
        assert nav._quest_mode is True
        assert nav._quest_deck_id == "deck-99"
        assert nav._last_event_id == event_id

        # Post-match note_match_end with merged winning target also preserves quest_mode + deck_id
        nav2 = AutoQueueNavigator(
            backend=object(),
            get_game_state=lambda: {},
            bridge=None,
        )
        nav2.set_enabled(True)
        completed = {"match_id": "m-end-1", "event_id": "Play", "deck_name": "Old Deck", **target}
        assert nav2.note_match_end(completed, confirmed_match_end=True) is True
        assert nav2._quest_mode is True
        assert nav2._quest_deck_id == "deck-99"
        assert nav2._last_event_id == event_id


def test_load_queueable_pairs_enriches_identity_and_handles_corrupt_or_missing_logs(tmp_path, monkeypatch):
    import json

    from arenamcp.between_match_coach import load_queueable_pairs
    from arenamcp.deck_library import DeckRecord

    # Missing file returns []
    assert load_queueable_pairs(log_path=tmp_path / "does_not_exist.log") == []

    # Corrupt / non-dict JSON lines do not crash, and valid lines get enriched via load_log_decks([path])
    log_file = tmp_path / "Player.log"
    ev_valid = json.dumps(
        {
            "request": json.dumps(
                {"EventName": "Historic_Ladder", "Summary": {"DeckId": "deck-42", "Name": ""}}
            )
        }
    )
    ev_bad_shape = json.dumps({"request": json.dumps(["not-a-dict"])})
    ev_bad_summary = json.dumps({"request": json.dumps({"EventName": "Ladder", "Summary": "oops"})})
    log_file.write_text(
        f"==> EventSetDeckV3 {ev_bad_shape}\n"
        f"==> EventSetDeckV3 {ev_bad_summary}\n"
        f"==> EventSetDeckV3 {ev_valid}\n",
        encoding="utf-8",
    )

    seen_paths = []

    def _fake_load_log_decks(paths=None):
        seen_paths.append(paths)
        return {"deck-42": DeckRecord(deck_id="deck-42", name="Enriched Deck", main=[(100, 4)])}

    monkeypatch.setattr("arenamcp.deck_library.load_log_decks", _fake_load_log_decks)
    pairs = load_queueable_pairs(log_path=log_file)
    assert seen_paths == [[log_file]]
    assert len(pairs) == 1
    assert pairs[0]["deck_name"] == "Enriched Deck"
    assert pairs[0]["deck_identity"] != ""


def test_compact_coach_same_vs_winning_auto_queue_toggles(monkeypatch):
    from PySide6.QtWidgets import QApplication

    from arenamcp.between_match_coach import QueueableDeck, QueuePlan
    from arenamcp.desktop.compact_coach import CompactCoachPanel

    app = QApplication.instance() or QApplication([])
    assert app is not None

    deck_a = QueueableDeck("d-a", "Deck Alpha", "Ladder", "id-a", 32, 29)
    deck_b = QueueableDeck("d-b", "Deck Beta", "Ladder", "id-b", 2, 9)
    fake_plan = QueuePlan(decks=(deck_a, deck_b), recommended=deck_a, summary="Queueable decks (2 found)")
    monkeypatch.setattr(
        "arenamcp.between_match_coach.evaluate_live_queue_plan", lambda log_path=None: fake_plan
    )

    class _Btn:
        def __init__(self) -> None:
            self.text = ""
            self.props: dict[str, str] = {}
            self.tooltip = ""

        def setText(self, t: str) -> None:
            self.text = t

        def setProperty(self, k: str, v: str) -> None:
            self.props[k] = v

        def setToolTip(self, t: str) -> None:
            self.tooltip = t

    class _Lbl:
        def __init__(self) -> None:
            self.text = ""
            self.visible = False

        def setText(self, t: str) -> None:
            self.text = t

        def setVisible(self, v: bool) -> None:
            self.visible = v

    panel = CompactCoachPanel.__new__(CompactCoachPanel)
    panel._settings = _FakeSettings({})
    panel._auto_queue_enabled = False
    panel._auto_queue_detail = ""
    panel._grind_enabled = False
    panel.auto_queue_btn = _Btn()
    panel.auto_queue_winning_btn = _Btn()
    panel.after_match_btn = _Btn()
    panel.auto_queue_status_label = _Lbl()
    sent: list[bool] = []
    panel._session = type("_S", (), {"set_auto_queue": lambda self, en: sent.append(en)})()
    logged: list[str] = []
    panel.append_log = lambda msg, role="status": logged.append(msg)
    monkeypatch.setattr("arenamcp.desktop.compact_coach.theme.repolish", lambda w: None)

    # 1. Click Winning decks ON
    panel._toggle_auto_queue_winning()
    assert panel._auto_queue_enabled is True
    assert panel._settings.get("auto_queue_mode") == "winning"
    assert panel.auto_queue_winning_btn.text == "Winning decks: On"
    assert panel.auto_queue_btn.text == "Auto-queue: Off"
    assert panel.after_match_btn.text == "Queue: Winning"
    assert any("Auto-queue (Winning decks) ON" in m for m in logged)

    # 2. Switch directly to Same deck
    panel._toggle_auto_queue()
    assert panel._auto_queue_enabled is True
    assert panel._settings.get("auto_queue_mode") == "same"
    assert panel.auto_queue_btn.text == "Auto-queue: On"
    assert panel.auto_queue_winning_btn.text == "Winning decks: Off"
    assert panel.after_match_btn.text == "Queue: On"
    assert any("Tip: Deck Alpha" in m for m in logged)

    # 3. Turn Off
    panel._toggle_auto_queue()
    assert panel._auto_queue_enabled is False
    assert panel.after_match_btn.text == "After match…"
    assert sent == [True, True, False]
