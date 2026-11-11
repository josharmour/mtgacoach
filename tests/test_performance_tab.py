"""Historical performance tab: match history persistence + rating + UI render.

The coach records W/L + metadata at game end (match_history.py, via
standalone_postmatch) and backfills a best-effort 1-10 coach rating after
post-match analysis. The Performance tab renders the last few matches,
degrading unrated rows to W/L-only. The tab reads match_history FRESH from
disk on every refresh (the coach is a separate process, so it must never
trust a memoized in-memory list).
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from arenamcp.desktop.performance_tab import PerformanceTab, autopilot_deck_results
from arenamcp.match_history import MatchHistory, record_from_game_end
from arenamcp.standalone_postmatch import _PostMatchMixin


@pytest.fixture(autouse=True)
def exercise_table_layout(monkeypatch):
    # Exercise the Windows/Linux layout on Mac CI as well. The Cocoa-safe
    # layout is exercised explicitly below, without any native table widgets.
    monkeypatch.setattr("arenamcp.desktop.performance_tab._USE_TABLES", True)


_SNAP = {
    "turn": {"turn_number": 5},
    "players": [{"is_local": True, "life_total": 10}, {"seat_id": 2, "life_total": 20}],
}


@pytest.fixture
def seeded_file(tmp_path, monkeypatch):
    """Point the default HISTORY_FILE at a temp path and seed it with 3 matches."""
    import arenamcp.match_history as mh

    path = tmp_path / "history.json"
    monkeypatch.setattr(mh, "HISTORY_FILE", path)

    h = MatchHistory()  # uses patched HISTORY_FILE
    h.add_record(record_from_game_end("m_a", "win", _SNAP, replay_path=""))
    h.add_record(record_from_game_end("m_b", "loss", _SNAP, replay_path=""))
    h.add_record(record_from_game_end("m_c", "win", _SNAP, replay_path=""))
    h.set_rating("m_b", 7, "Good fundamentals; one over-extended combat swing.")
    return path


# ---- backend: record write + rating backfill ------------------------------


def test_backfill_rating_requires_match_id(tmp_path):
    h = MatchHistory(history_path=tmp_path / "h.json")
    h.add_record(record_from_game_end("mx", "win", {"players": []}, replay_path=""))
    h.set_rating("", 4)  # no match id: nothing to backfill
    h.set_rating("mx", None)  # no rating either
    assert [r.coach_rating for r in h.get_recent(10)] == [None]
    h.set_rating("mx", 4)
    assert [r.coach_rating for r in h.get_recent(10)] == [4]


def test_unrated_record_defaults_to_none(tmp_path):
    h = MatchHistory(history_path=tmp_path / "h.json")
    h.add_record(record_from_game_end("my", "loss", {"players": []}, replay_path=""))
    assert h.get_recent(10)[0].coach_rating is None


def test_rating_extraction_from_analysis():
    from arenamcp.standalone_postmatch import _PostMatchMixin

    x = _PostMatchMixin()
    assert x._coach_rating_from_analysis("Played well. Rating: 8/10") == 8
    assert x._coach_rating_from_analysis("Overall score: 7 out of 10") == 7
    assert x._coach_rating_from_analysis("No rating mentioned") is None


# ---- frontend: PerformanceTab renders the history -------------------------


def test_performance_tab_renders_rows_and_summary(seeded_file, qapp):
    tab = PerformanceTab()
    tab.refresh()
    assert tab._table.rowCount() == 3
    results = [tab._table.item(r, 0).text() for r in range(3)]
    scores = [tab._table.item(r, 1).text() for r in range(3)]
    assert results == ["W", "L", "W"]
    assert scores == ["—", "Legacy note", "—"]
    assert "2W / 1L" in tab._summary.text()
    assert "score" not in tab._summary.text()
    assert "/10" not in tab._summary.text()


def test_performance_tab_reloads_disk_on_refresh(seeded_file, qapp, tmp_path, monkeypatch):
    """A match written AFTER the tab was built must appear on the next refresh.

    This pins the original bug: the tab used a memoized in-memory singleton
    created at app start, so a match recorded by the coach subprocess was
    never visible until app restart.
    """
    tab = PerformanceTab()  # built -> reads the (seeded) file
    assert tab._table.rowCount() == 3

    # Coach subprocess writes a new match to the same file.
    MatchHistory().add_record(record_from_game_end("m_new", "win", _SNAP, replay_path=""))

    tab.refresh()
    assert tab._table.rowCount() == 4  # fresh disk read picks it up


def test_performance_tab_shows_score_reason_on_selection(seeded_file, qapp):
    tab = PerformanceTab()
    tab.refresh()
    # Select the L / 7/10 row (row index 1, newest-first).
    tab._table.selectRow(1)
    assert "Legacy model note (unverified" in tab._detail.text()
    assert "/10" not in tab._detail.text()
    assert "over-extended" in tab._detail.text()
    # Hover tooltip on the row also carries the reason.
    tooltip = tab._table.item(1, 1).toolTip()
    assert "/10" not in tooltip
    assert "legacy model note (unverified)" in tooltip


def test_performance_tab_non_rated_row_messages(seeded_file, qapp):
    tab = PerformanceTab()
    tab.refresh()
    tab._table.selectRow(0)  # W row with no rating
    assert "No saved model note" in tab._detail.text()


def test_performance_tab_empty_history_is_graceful(qapp, tmp_path, monkeypatch):
    import arenamcp.match_history as mh

    monkeypatch.setattr(mh, "HISTORY_FILE", tmp_path / "empty.json")
    tab = PerformanceTab()
    tab.refresh()
    assert tab._table.rowCount() == 0
    assert "0 shown" in tab._summary.text()


# ---- automatic advice-quality score ---------------------------------------


class _FakeBackend:
    def __init__(self, resp):
        self.resp = resp
        self.last_user = None

    def complete(self, system, user, **kw):
        self.last_user = user
        return self.resp


class _FakeCoach:
    def __init__(self, resp):
        self._backend = _FakeBackend(resp)


class _Host(_PostMatchMixin):
    """Borrow the real score methods; only _coach is needed as a collaborator."""

    def __init__(self, resp):
        self._coach = _FakeCoach(resp)


def _seed_record(match_id, result="win"):
    from arenamcp.match_history import MatchHistory, record_from_game_end

    MatchHistory().add_record(record_from_game_end(match_id, result, {"players": []}, replay_path=""))


_ADVICE = [
    {"game_snapshot": {"turn_number": 1, "phase": "Main"}, "advice": "Cast Elves"},
    {"game_snapshot": {"turn_number": 2, "phase": "Combat"}, "advice": "Attack with all"},
]


def test_auto_score_parses_and_writes_rating(tmp_path, monkeypatch, qapp):
    import arenamcp.match_history as mh

    monkeypatch.setattr(mh, "HISTORY_FILE", tmp_path / "history.json")
    _seed_record("m_auto")

    host = _Host('{"rating": 8, "reason": "Good lines, one over-extension"}')
    host._score_match_advice("m_auto", "win", _ADVICE)

    rec = mh.MatchHistory().get_recent(10)[0]
    assert rec.coach_rating == 8
    assert "over-extension" in rec.coach_score_reason
    assert "Cast Elves" in host._coach._backend.last_user


def test_auto_score_degrades_gracefully_on_bad_response(tmp_path, monkeypatch, qapp):
    import arenamcp.match_history as mh

    monkeypatch.setattr(mh, "HISTORY_FILE", tmp_path / "history.json")
    _seed_record("m_bad")
    mh.MatchHistory().set_rating("m_bad", 5)

    host = _Host("not json at all")
    host._score_match_advice("m_bad", "win", _ADVICE)
    assert mh.MatchHistory().get_recent(10)[0].coach_rating == 5  # unchanged


def test_auto_score_noop_without_advice(tmp_path, monkeypatch, qapp):
    import arenamcp.match_history as mh

    monkeypatch.setattr(mh, "HISTORY_FILE", tmp_path / "history.json")
    host = _Host('{"rating": 3}')
    host._score_match_advice("m_empty", "loss", [])  # must not raise


# -----------------------------------------------
# data capture (colors/format) + reason persistence


def test_write_performance_record_captures_colors_and_format(tmp_path, monkeypatch):
    import arenamcp.match_history as mh

    monkeypatch.setattr(mh, "HISTORY_FILE", tmp_path / "h.json")
    host = _Host('{"rating": 5}')
    final_state = {
        "opponent_played_cards": [
            {"name": "Lightning Helix", "mana_cost": "{R}{W}"},
            {"name": "Swiftwater Cliffs", "mana_cost": ""},
        ],
        "super_format": "Brawl",
    }
    host._write_performance_record("m_c", "loss", final_state, replay_path="")
    rec = mh.MatchHistory().get_recent(10)[0]
    assert "R" in rec.opponent_colors_seen and "W" in rec.opponent_colors_seen
    assert rec.format_name == "Brawl"


def test_selection_and_reason_persist_across_refresh(seeded_file, qapp):
    """The 3s auto-refresh must not wipe the selected row's explanation."""
    tab = PerformanceTab()
    tab.refresh()
    tab._table.selectRow(1)  # the L / 7/10 row
    assert "over-extended" in tab._detail.text()

    before = tab._detail.text()
    tab.refresh()  # simulates a poll rebuilding the table
    assert tab._detail.text() == before
    assert "over-extended" in tab._detail.text()


def test_sidebar_ranks_open_recent_ladder_matches(seeded_file, qapp, monkeypatch, tmp_path):
    from PySide6.QtGui import QAction

    from arenamcp.desktop.main_window import MainWindow
    from arenamcp.match_history import MatchRecord

    path = tmp_path / "Player.log"
    path.write_text(
        '<== RankGetCombinedRankInfo(id)\n{"constructedSeasonOrdinal":95,"constructedClass":"Diamond","constructedLevel":4,"constructedStep":1,"limitedSeasonOrdinal":95,"limitedLevel":3}\n'
    )
    monkeypatch.setenv("MTGA_LOG_PATH", str(path))
    monkeypatch.setenv("MTGACOACH_GAME_DEVICE", "desktop")
    monkeypatch.setattr(MainWindow, "_start_session", lambda self: None)
    history = MatchHistory()
    history.add_record(MatchRecord(match_id="draft", format_name="QuickDraft FRA", result="win"))
    for i in range(24):
        history.add_record(MatchRecord(match_id=str(i), format_name="Ladder", result="loss"))
    history.add_record(MatchRecord(match_id="casual", format_name="Play", result="win"))
    win = MainWindow()
    try:
        win.show()
        qapp.processEvents()
        strip = win.coach_panel.rank_strip
        assert strip.isVisible()
        assert "\n".join(strip.buttons["Constructed"].text().splitlines()[:2]).replace(
            " · ", "\n"
        ).splitlines() == [
            "Constructed",
            "Diamond 4",
            "1 pip",
        ]
        assert "\n".join(strip.buttons["Limited"].text().splitlines()[:2]).replace(
            " · ", "\n"
        ).splitlines() == [
            "Limited",
            "Bronze 3",
            "0 pips",
        ]
        for ladder, expected in (
            ("Limited", ["draft"]),
            ("Constructed", [str(i) for i in range(23, 13, -1)]),
        ):
            strip.buttons[ladder].click()
            assert win.coach_panel.isVisible()
            assert [row.match_id for row in strip.recent[ladder]] == expected
            assert f"Recent {ladder} · newest first" in win.coach_panel.detail_pager.title.text()
            win.coach_panel._close_detail()
        next(action for action in win.findChildren(QAction) if action.text() == "Match History…").trigger()
        assert win.performance_tab._row_records[0].match_id == "casual"
        assert len(win.performance_tab._row_records) == 20
    finally:
        win.close()


def test_mac_history_uses_accessible_buttons_and_retains_selection(seeded_file, qapp, monkeypatch):
    from PySide6.QtGui import QAccessible
    from PySide6.QtWidgets import QTableWidget

    monkeypatch.setattr("arenamcp.desktop.performance_tab._USE_TABLES", False)
    tab = PerformanceTab()
    tab.show()
    qapp.processEvents()
    try:
        assert tab.findChildren(QTableWidget) == []
        cards = tab._cards
        record = next(r for r in tab._row_records if r.coach_rating is not None)
        button = cards.buttons[record.match_id]
        button.click()
        assert "Legacy model note (unverified" in tab._detail.text()
        assert record.coach_score_reason in tab._detail.text()
        interface = QAccessible.queryAccessibleInterface(button)
        assert interface.role() == QAccessible.Role.CheckBox  # a checkable QPushButton
        assert (record.opponent_name or "opponent") in interface.text(QAccessible.Text.Name)
        for _ in range(4):
            tab.refresh()
            qapp.processEvents()
            assert cards.buttons[record.match_id] is button
            assert button.isChecked()
            assert tab._selected_match_id() == record.match_id
        tab.set_ladder("Limited")
        assert tab._selected_match_id() is None
        assert cards.buttons == {}
        assert "No ranked Limited" in tab._summary.text()
    finally:
        tab.close()


def test_rank_strip_picks_up_new_result_without_changing_geometry(seeded_file, qapp, monkeypatch, tmp_path):
    from arenamcp.desktop.rank_strip import RankStrip
    from arenamcp.match_history import MatchRecord

    monkeypatch.setenv("MTGA_LOG_PATH", str(tmp_path / "missing.log"))
    monkeypatch.setenv("MTGACOACH_GAME_DEVICE", "desktop")
    strip = RankStrip()
    strip.resize(600, 100)
    strip.show()
    strip.refresh()
    before = strip.size()
    history = MatchHistory()
    for i, result in enumerate(("loss", "win") * 5 + ("draw",)):
        history.add_record(MatchRecord(match_id=str(i), format_name="Ladder", result=result))
    strip.refresh()
    assert [r.match_id for r in strip.recent["Constructed"]] == [str(i) for i in range(10, 0, -1)]
    assert strip.buttons["Constructed"].text().endswith("Newest → oldest\nD W L W L W L W L W")
    history.set_result("10", "win")
    strip.refresh()
    assert strip.buttons["Constructed"].text().endswith("Newest → oldest\nW W L W L W L W L W")
    assert strip.size() == before
    strip.recent["Constructed"] = [MatchRecord(result="win")] * 10
    strip.resize(304, 100)
    qapp.processEvents()
    button = strip.buttons["Constructed"]
    lines = button.text().splitlines()[-2:]
    if lines[0] == "Newest → oldest":
        assert lines[1] == "WWWWWWWWWW"
    else:
        assert lines[0] in ("Newest WWWWW", "New WWWWW")
        assert lines[1] in ("WWWWW Oldest", "WWWWW Old")
    assert all(
        button.fontMetrics().horizontalAdvance(line) <= button.width() - 28
        for line in button.text().splitlines()
    )
    strip.close()


# ---- per-deck autopilot results (P1) --------------------------------------


def _deck_record(match_id, result, *, autopilot=True, identity="hash-mono-blue", name="Mono Blue Tempo"):
    from arenamcp.match_history import MatchRecord

    return MatchRecord(
        match_id=match_id,
        timestamp="2026-11-10T12:00:00+00:00",
        result=result,
        format_name="Ladder",
        deck_identity=identity,
        deck_name=name,
        autopilot=autopilot,
    )


def test_autopilot_deck_results_count_only_autopilot_games_of_one_deck():
    """Real logged case: evidence bundle 00651f77… (2026-11-10 Ladder loss,
    autopilot enabled, 60-card deck). Aggregation must group by deck identity,
    count only autopilot games, exclude draws, and fall back to the deck name
    for records written before identity tracking existed."""
    records = [
        _deck_record("a1", "win"),
        _deck_record("a2", "loss"),
        _deck_record("a3", "draw"),  # not a decided game
        _deck_record("a4", "win", autopilot=False),  # advisor game: the player drove
        _deck_record("a5", "win", identity="", name="Mono Blue Tempo"),  # pre-identity record
        _deck_record("b1", "loss", identity="hash-other", name="Other Deck"),
    ]

    entries = autopilot_deck_results(records)

    by_name = {entry["name"]: entry for entry in entries}
    assert by_name["Mono Blue Tempo"]["games"] == 3  # a1, a2, a5 — not the draw or advisor win
    assert (by_name["Mono Blue Tempo"]["wins"], by_name["Mono Blue Tempo"]["losses"]) == (2, 1)
    assert by_name["Other Deck"]["games"] == 1
    # Sorted most-played first.
    assert [entry["name"] for entry in entries] == ["Mono Blue Tempo", "Other Deck"]


def test_win_rate_hidden_below_twenty_decided_autopilot_games():
    """A win rate is noise below the 20-decided-game sample floor (same floor
    the deck-building invitation uses); it appears at exactly 20."""
    nineteen = [_deck_record(f"n{i}", "loss") for i in range(19)]
    assert all(entry["win_rate"] is None for entry in autopilot_deck_results(nineteen))

    twenty = nineteen + [_deck_record("n19", "win")]
    (entry,) = autopilot_deck_results(twenty)
    assert entry["win_rate"] == pytest.approx(1 / 20)


def test_performance_tab_shows_per_deck_section_only_with_autopilot_history(seeded_file, qapp):
    """The section stays hidden while history has no autopilot games (legacy
    records), and appears once per-deck autopilot results exist."""
    tab = PerformanceTab()
    tab.refresh()
    assert not tab._decks.isVisibleTo(tab)  # seeded records predate P1

    import arenamcp.match_history as mh

    history = mh.MatchHistory()
    for i in range(20):
        history.add_record(_deck_record(f"d{i}", "win" if i % 3 == 0 else "loss"))

    tab.refresh()
    assert tab._decks.isVisibleTo(tab)
    assert "AUTOPILOT RESULTS BY DECK" in tab._decks_caption.text()
    # 7 wins (i % 3 == 0 -> i in 0..18 step 3), 13 losses, 20 decided games.
    assert "Mono Blue Tempo · 20 games · 7W–13L · 35% win rate" in tab._decks.text()


def test_write_performance_record_stamps_autopilot_and_deck_identity(tmp_path, monkeypatch):
    """Game-end capture: the record written by the coach carries the autopilot
    flag and the deck identity derived from the final state's deck list."""
    import arenamcp.match_history as mh

    monkeypatch.setattr(mh, "HISTORY_FILE", tmp_path / "h.json")
    host = _Host('{"rating": 5}')
    final_state = {
        "opponent_played_cards": [{"name": "Lightning Helix", "mana_cost": "{R}{W}"}],
        "format_name": "Ladder",
        "deck_cards": [111] * 4 + [222] * 20 + [333] * 36,
    }
    host._autopilot_enabled = True
    host._write_performance_record("m_p1", "loss", final_state, replay_path="")

    rec = mh.MatchHistory().get_recent(10)[0]
    assert rec.autopilot is True
    assert rec.deck_identity
