"""Simple speech choices, isolated setup, and previews that never save preferences."""

from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLineEdit

from arenamcp.commentary.setup import SpeechSetup
from arenamcp.desktop import commentary_settings as ui


class Settings(dict):
    def set(self, key, value):
        self[key] = value


@pytest.fixture
def dialog(qapp, monkeypatch):
    monkeypatch.setattr(ui.qwen_public, "command", lambda: None)
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
    assert "Server Chatterbox" not in [w.text() for w in widget.findChildren(QCheckBox)]
    assert "Speech on" not in [w.text() for w in widget.findChildren(QCheckBox)]
    assert widget.provider.count() == 2 and widget.mode.count() == 2
    assert widget.style.count() == 2
    assert {widget.style.itemText(i) for i in range(2)} == {"Hype", "Technical"}
    assert widget.provider.currentData() == "kokoro"
    # Legacy muted=True without explicit voice_volume opens at 0% (silent) and unmutes on save.
    assert widget.volume.value() == 0
    assert widget._apply()  # inactive Chatterbox never blocks Kokoro
    assert settings["muted"] is False
    assert settings["voice_volume"] == 0.0
    session.set_speech_enabled.assert_called_once_with(True)


def test_preview_does_not_save_or_enable_speech(dialog):
    widget, settings, session = dialog
    before = dict(settings)
    widget.voice.setCurrentIndex(1)
    widget._preview()
    session.preview_voice.assert_called_once()
    assert session.preview_voice.call_args.args[0]["voice"] == widget.voice.currentData()
    assert settings == before
    session.set_speech_enabled.assert_not_called()


def test_unavailable_chatterbox_blocks_preview_but_can_save_preferences(dialog):
    widget, settings, session = dialog
    widget.provider.setCurrentIndex(1)
    widget.volume.setValue(75)
    widget._preview()
    assert not widget.preview.isEnabled()
    session.preview_voice.assert_not_called()
    assert "Sign in to Coach" in widget.status.text()
    assert widget._apply()
    assert settings["tts_provider"] == "chatterbox_remote"
    assert settings["muted"] is False
    assert settings["voice_volume"] == pytest.approx(0.75)


@pytest.mark.parametrize("catalog_state", ["pending", "failed", "one_voice"])
@pytest.mark.parametrize("volume_pct", [0, 80])
@pytest.mark.parametrize("saved_pair", ["", "mira|atlas"])
def test_save_closes_with_unavailable_announcer_voices(dialog, catalog_state, volume_pct, saved_pair):
    widget, settings, session = dialog
    widget.setup.server_configured = True
    widget.catalog_pending = True  # deterministic in-flight lookup; no network
    widget.pairs["chatterbox"] = saved_pair
    widget.provider.setCurrentIndex(widget.provider.findData("chatterbox"))
    widget.mode.setCurrentIndex(widget.mode.findData("commentary"))
    if catalog_state == "failed":
        widget._catalog_ready({"error": "Can't load server voices"})
    elif catalog_state == "one_voice":
        widget._catalog_ready({"voices": ["atlas"], "default": "atlas"})
    widget.volume.setValue(volume_pct)
    widget.speed.setCurrentIndex(widget.speed.findData(2.0))
    widget.show()
    assert widget.isVisible()
    assert not widget.preview.isEnabled()
    widget._preview()
    session.preview_voice.assert_not_called()

    widget.findChild(QDialogButtonBox).button(QDialogButtonBox.Save).click()

    assert widget.result() == QDialog.Accepted
    assert not widget.isVisible()
    assert settings["commentary_chatterbox_pair"] == saved_pair
    assert settings["autoplay_speech"] == "commentary"
    assert settings["tts_provider"] == "chatterbox_remote"
    assert settings["speech_speed"] == 2.0
    assert settings["muted"] is False
    assert settings["voice_volume"] == pytest.approx(volume_pct / 100.0)
    session.set_speech_enabled.assert_called_once_with(True)


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
    widget.volume.setValue(65)
    assert widget._apply()
    assert settings["chatterbox_voice_override"] == "mira"
    assert settings["tts_provider"] == "chatterbox_remote"
    assert settings["autoplay_speech"] == "commentary" and not settings["muted"]
    assert settings["commentary_persona"] == "hype"
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


def test_announcers_include_player_cards_without_a_switch(dialog):
    widget, settings, session = dialog
    settings["commentary_private_information"] = False  # Legacy preference.
    widget.mode.setCurrentIndex(widget.mode.findData("commentary"))
    assert widget._apply()
    assert session.configure_commentary.call_args.args[0]["private_information"] is True
    assert ui.CommentaryConfig.from_settings(settings).private_information
    assert not hasattr(widget, "private")


def test_kokoro_custom_role_voices_preview_save_and_reopen(dialog):
    from arenamcp.commentary.voices import voice_pair

    widget, settings, session = dialog
    widget.mode.setCurrentIndex(widget.mode.findData("commentary"))
    assert widget.voice.count() == 28 and widget.analyst.count() == 27
    widget.voice.setCurrentIndex(widget.voice.findData("bf_emma"))
    widget.analyst.setCurrentIndex(widget.analyst.findData("bm_fable"))
    widget._preview()
    assert voice_pair(session.preview_voice.call_args.args[0]) == ("bf_emma", "bm_fable")
    assert not settings.get("commentary_kokoro_pair")
    widget._apply()
    reopened = ui.CommentarySettingsDialog(settings, session)
    assert (reopened.voice.currentData(), reopened.analyst.currentData()) == ("bf_emma", "bm_fable")
    reopened.voice.setCurrentIndex(reopened.voice.findData("bm_fable"))
    assert reopened.analyst.findData("bm_fable") == -1
    assert voice_pair(reopened._values()) == ("bm_fable", reopened.analyst.currentData())
    reopened.close()


def test_local_pair_preview_save_with_player_cards(dialog):
    from arenamcp.commentary import qwen_public

    widget, settings, session = dialog
    widget._local_ready(True)
    widget.mode.setCurrentIndex(widget.mode.findData("commentary"))
    widget.public_voice.setCurrentIndex(widget.public_voice.findData(qwen_public.PROVIDER))
    before = settings.copy()
    widget._preview()
    options = session.preview_voice.call_args.args[0]
    assert qwen_public.selected(options) and widget.speed_label.text() == "Target pace"
    assert settings == before and widget.voice.isHidden() and widget.analyst.isHidden()
    widget._apply()
    assert qwen_public.selected(settings)
    assert widget._expressive()
    widget.mode.setCurrentIndex(widget.mode.findData("actions"))
    assert widget.public_voice.isHidden() and not qwen_public.selected(widget._values())


def test_dialog_accepts_real_settings_instance_with_in_memory_muted(qapp, monkeypatch, tmp_path):
    from arenamcp.settings import Settings as RealSettings

    monkeypatch.setattr("arenamcp.settings.SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(ui.qwen_public, "command", lambda: None)
    setup = SpeechSetup(
        {
            k: ""
            for k in (
                "commentary_python",
                "commentary_aoa_source",
                "chatterbox_server_url",
                "chatterbox_key_file",
            )
        },
        False,
        False,
        True,
        True,
    )
    monkeypatch.setattr(ui, "discover", lambda settings: setup)
    real = RealSettings()
    real._data.pop("voice_volume", None)
    real._data["muted"] = True
    session = Mock()
    widget = ui.CommentarySettingsDialog(real, session)
    try:
        assert widget.volume.value() == 0
        assert widget._apply()
        assert real.get("muted") is False
        assert real.get("voice_volume") == 0.0
    finally:
        widget.close()
