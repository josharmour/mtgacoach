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

from arenamcp.desktop.performance_tab import PerformanceTab
from arenamcp.match_history import MatchHistory, record_from_game_end
from arenamcp.standalone_postmatch import _PostMatchMixin

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
    assert scores == ["—", "7/10", "—"]
    assert "2W / 1L" in tab._summary.text()
    assert "avg coach score 7.0/10" in tab._summary.text()


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
    assert "score 7/10" in tab._detail.text()
    assert "over-extended" in tab._detail.text()
    # Hover tooltip on the row also carries the reason.
    tooltip = tab._table.item(1, 1).toolTip()
    assert "/10" in tooltip
    assert "over-extended" in tooltip


def test_performance_tab_non_rated_row_messages(seeded_file, qapp):
    tab = PerformanceTab()
    tab.refresh()
    tab._table.selectRow(0)  # W row with no rating
    assert "no score" in tab._detail.text()


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
