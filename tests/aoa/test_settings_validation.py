"""Simple speech choices, isolated setup, and previews that never save preferences."""

from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QLineEdit

from arenamcp.commentary.setup import SpeechSetup
from arenamcp.desktop import commentary_settings as ui


class Settings(dict):
    def set(self, key, value):
        self[key] = value


@pytest.fixture
def dialog(qapp, monkeypatch):
    values = {
        key: ""
        for key in (
            "commentary_python",
            "commentary_aoa_source",
            "chatterbox_server_url",
            "chatterbox_key_file",
        )
    }
    setup = SpeechSetup(values, False, False, True, True)
    monkeypatch.setattr(ui, "discover", lambda settings: setup)
    settings = Settings(
        tts_provider="kokoro",
        commentary_persona="esports",
        muted=True,
        chatterbox_voice="mira",
        commentary_chatterbox_analyst="atlas",
    )
    session = Mock()
    widget = ui.CommentarySettingsDialog(settings, session)
    yield widget, settings, session
    widget.close()


def test_simple_surface_has_no_manual_paths_or_backend_controls(dialog):
    widget, settings, session = dialog
    assert not widget.findChildren(QLineEdit)
    assert widget.provider.count() == 2 and widget.mode.count() == 2
    assert widget.provider.currentData() == "kokoro"
    assert widget._apply()  # inactive Chatterbox never blocks Kokoro
    assert settings["muted"] is True
    session.set_speech_enabled.assert_called_once_with(False)


def test_preview_does_not_save_or_enable_speech(dialog):
    widget, settings, session = dialog
    before = dict(settings)
    widget.voice.setCurrentIndex(1)
    widget._preview()
    session.preview_voice.assert_called_once()
    assert session.preview_voice.call_args.args[0]["voice"] == widget.voice.currentData()
    assert settings == before
    session.set_speech_enabled.assert_not_called()


def test_unavailable_chatterbox_is_actionable_and_cannot_start_or_save_enabled(dialog):
    widget, settings, session = dialog
    before = dict(settings)
    widget.provider.setCurrentIndex(1)
    widget.server.setChecked(False)
    widget.enabled.setChecked(True)
    widget._preview()
    assert not widget._apply()
    assert "Server Chatterbox" in widget.status.text() and "Kokoro" in widget.status.text()
    assert settings == before and not session.mock_calls


def test_cancel_preserves_all_settings(dialog):
    widget, settings, session = dialog
    before = dict(settings)
    widget.provider.setCurrentIndex(1)
    widget.reject()
    assert settings == before and not session.mock_calls


def test_actual_catalog_default_and_explicit_selection(dialog):
    widget, settings, session = dialog
    widget.catalog = {"voices": ["atlas", "mira", "default"], "default": "atlas"}
    widget.setup.server_configured = True
    widget.provider.setCurrentIndex(1)
    assert widget.voice.currentData() == ""  # ignore both old troubleshooting voices
    assert widget.voice.currentText() == "Server default (Atlas)"
    widget._preview()
    assert session.preview_voice.call_args.args[0]["chatterbox_voice"] == "server_default"
    widget.voice.setCurrentIndex(widget.voice.findData("mira"))
    widget.mode.setCurrentIndex(1)
    widget.enabled.setChecked(True)
    assert widget._apply()
    assert settings["chatterbox_voice_override"] == "mira"
    assert settings["tts_provider"] == "chatterbox_remote"
    assert settings["autoplay_speech"] == "commentary" and not settings["muted"]
    assert settings["commentary_persona"] == "esports"
    reopened = ui.CommentarySettingsDialog(settings, session)
    reopened._catalog_ready(widget.catalog)
    assert reopened.voice.currentData() == ""  # automatic pair, separate from advice voice
    assert settings["chatterbox_voice_override"] == "mira"
    reopened.close()


def test_catalog_failure_does_not_offer_invented_voices(dialog):
    widget, settings, session = dialog
    widget.provider.setCurrentIndex(1)
    widget._catalog_ready({"error": "Server unavailable"})
    assert widget.voice.count() == 1
    assert widget.voice.currentData() == ""


def test_speed_default_preview_save_and_reopen(dialog):
    widget, settings, session = dialog
    assert widget.speed.currentData() == 1.5
    before = dict(settings)
    widget.speed.setCurrentIndex(widget.speed.findData(1.75))
    widget._preview()
    assert session.preview_voice.call_args.args[0]["speech_speed"] == 1.75
    assert settings == before
    assert widget._apply()
    session.set_speech_speed.assert_called_once_with(1.75)
    reopened = ui.CommentarySettingsDialog(settings, session)
    assert reopened.speed.currentData() == 1.75
    reopened.close()


@pytest.mark.parametrize("style", ui.PERSONAS)
def test_two_role_style_pair_preview_and_save(dialog, style):
    widget, settings, session = dialog
    widget.catalog = {"voices": ["atlas", "mira", "default"], "default": "atlas"}
    widget.setup.server_configured = True
    widget.provider.setCurrentIndex(1)
    widget.mode.setCurrentIndex(1)
    widget.style.setCurrentIndex(widget.style.findData(style))
    widget.voice.setCurrentIndex(widget.voice.findData("mira|atlas"))
    before = dict(settings)
    widget._preview()
    options = session.preview_voice.call_args.args[0]
    assert options["commentary_chatterbox_pair"] == "mira|atlas"
    assert options["commentary_persona"] == style and options["commentary_speakers"] == 2
    assert options["_commentary_voice_catalog"] == widget.catalog
    assert settings == before
    assert widget._apply()
    assert "_commentary_voice_catalog" not in settings
    assert settings["commentary_speakers"] == 2
    reopened = ui.CommentarySettingsDialog(settings, session)
    reopened._catalog_ready(widget.catalog)
    assert reopened.voice.currentData() == "mira|atlas" and reopened.style.currentData() == style
    reopened.close()


def test_own_hand_and_deck_preference_is_visible_and_persists(dialog):
    widget, settings, session = dialog
    widget.mode.setCurrentIndex(widget.mode.findData("commentary"))
    assert not widget.private.isHidden()
    widget.private.setChecked(True)
    assert widget._apply()
    assert settings["commentary_private_information"] is True
    assert session.configure_commentary.call_args.args[0]["private_information"] is True
    widget.mode.setCurrentIndex(widget.mode.findData("actions"))
    assert widget.private.isHidden()
