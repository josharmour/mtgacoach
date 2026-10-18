"""A finished match stops tactical advice and inputs until the next match starts."""

from unittest.mock import Mock

from arenamcp.standalone import StandaloneCoach


def test_completed_match_stops_coaching_until_new_match(monkeypatch):
    from arenamcp import server

    monkeypatch.setattr(server, "_completed_match_for_navigation", {"match_id": "finished"})
    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach.ui = Mock()
    assert coach._completed_match_wait({"match_id": "finished"})
    assert coach._completed_match_wait({"match_id": "finished"})
    coach.ui.advice.assert_called_once_with("Match finished.", "MATCH")
    assert not coach._completed_match_wait({"match_id": "new-match"})
    assert not coach._completed_match_wait({})


def test_low_life_without_match_result_does_not_stop_coaching(monkeypatch):
    from arenamcp import server

    monkeypatch.setattr(server, "_completed_match_for_navigation", {})
    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach.ui = Mock()
    state = {"match_id": "active", "players": [{"is_local": True, "life_total": -2}]}
    assert not coach._completed_match_wait(state)
    coach.ui.advice.assert_not_called()
