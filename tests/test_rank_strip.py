"""Account identity and Mock B Live Event + Compact Ladders on RankStrip."""

import json

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from arenamcp.desktop import theme
from arenamcp.desktop.rank_strip import RankStrip
from arenamcp.match_history import MatchHistory, MatchRecord


def test_account_header_unknown_named_and_narrow(monkeypatch):
    app = QApplication.instance() or QApplication([])
    strip = RankStrip()
    strip.resize(540, 200)
    strip._render()
    assert "Not detected yet" in strip.account_label.text()
    strip._reader.account_name = "<Player>#12345"
    strip._render()
    assert strip.account_label.text() == "Arena account · <Player>#12345"
    assert strip.account_label.textFormat() == Qt.PlainText
    assert strip.buttons["Constructed"].text().startswith("Constructed")
    strip.event_banner.hide()
    assert strip.event_banner.isHidden()
    strip._reader.account_name = "Very long account name " * 10
    strip.resize(240, 200)
    strip._render()
    assert strip.account_label.text().endswith("…")
    assert strip._reader.account_name.strip() in strip.account_label.toolTip()
    strip.close()
    app.processEvents()


def test_mock_b_shows_active_event_banner_and_classifies_contender_draft(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    log_path = tmp_path / "Player.log"
    hist_path = tmp_path / "history.json"
    log_path.write_text(
        "\n".join(
            [
                "<== RankGetCombinedRankInfo(1)",
                json.dumps(
                    {
                        "constructedSeasonOrdinal": 95,
                        "constructedClass": "Mythic",
                        "constructedPercentile": 88.2,
                        "limitedSeasonOrdinal": 95,
                        "limitedLevel": 2,
                        "limitedStep": 1,
                    }
                ),
                "<== EventGetCoursesV2(2)",
                json.dumps(
                    {
                        "Courses": [
                            {
                                "InternalEventName": "ContenderDraft_TRK_20261123",
                                "CurrentModule": "CreateMatch",
                                "CurrentWins": 2,
                                "CurrentLosses": 1,
                            }
                        ]
                    }
                ),
            ]
        )
        + "\n"
    )
    history = MatchHistory(hist_path)
    history.add_record(MatchRecord(match_id="m1", format_name="ContenderDraft TRK 20261123", result="win"))
    history.add_record(MatchRecord(match_id="m2", format_name="ContenderDraft TRK 20261123", result="win"))
    history.add_record(MatchRecord(match_id="m3", format_name="ContenderDraft TRK 20261123", result="loss"))
    history.add_record(MatchRecord(match_id="m4", format_name="Play", result="win"))

    import arenamcp.match_history as mh

    monkeypatch.setattr(mh, "HISTORY_FILE", hist_path)
    monkeypatch.setenv("MTGA_LOG_PATH", str(log_path))
    monkeypatch.setenv("MTGACOACH_GAME_DEVICE", "desktop")

    strip = RankStrip()
    strip.resize(440, 220)
    strip.show()
    strip.refresh()

    assert not strip.event_banner.isHidden()
    assert strip.active_event is not None
    assert strip.active_event["display_name"] == "Contender Draft · TRK"
    assert [r.match_id for r in strip.recent["Limited"]] == ["m3", "m2", "m1"]
    assert [r.match_id for r in strip.recent_event] == ["m3", "m2", "m1"]
    assert [r.match_id for r in strip.recent_unranked] == ["m4"]

    clicked: list[str] = []
    strip.history_requested.connect(clicked.append)
    strip.event_banner.click()
    assert clicked == ["Event"]

    for name in (theme.THEME_DARK, theme.THEME_LIGHT, theme.THEME_HIGH_CONTRAST):
        theme.apply_theme(app, name)
        strip.repaint()
        app.processEvents()

    strip.close()
    app.processEvents()
