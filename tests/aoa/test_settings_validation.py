"""Provider selection validates only the active provider, before any settings write."""

from unittest.mock import Mock

import pytest

from arenamcp.desktop import commentary_settings as ui


class Settings(dict):
    def set(self, key, value):
        self[key] = value


@pytest.fixture
def dialog(qapp, monkeypatch):
    settings = Settings(tts_provider="kokoro", commentary_persona="esports")
    session = Mock()
    warning = Mock()
    monkeypatch.setattr(ui.QMessageBox, "warning", warning)
    widget = ui.CommentarySettingsDialog(settings, session)
    yield widget, settings, session, warning
    widget.close()


@pytest.mark.parametrize("endpoint", ["", "not a URL"])
def test_kokoro_ignores_inactive_chatterbox_settings(dialog, monkeypatch, endpoint):
    widget, settings, session, warning = dialog
    widget.fields["chatterbox_server_url"].setText(endpoint)
    for name in ("speech_endpoint", "read_speech_key", "validate_model"):
        monkeypatch.setattr(ui, name, Mock(side_effect=AssertionError("inactive provider validated")))
    assert widget._apply()
    assert settings["tts_provider"] == "kokoro"
    session.configure_commentary.assert_called_once()
    warning.assert_not_called()


@pytest.mark.parametrize("action", ["_save", "_preview"])
def test_unsaved_remote_selection_explains_empty_endpoint_and_writes_nothing(dialog, action):
    widget, settings, session, warning = dialog
    before = dict(settings)
    widget.provider.setCurrentIndex(widget.provider.findData("chatterbox_remote"))
    getattr(widget, action)()
    message = warning.call_args.args[2]
    assert "Chatterbox server is selected" in message
    assert "URL is empty" in message and "Kokoro" in message
    assert widget.tabs.currentWidget() is widget.runtime_tab
    assert settings == before  # Saved Kokoro does not reveal the unsaved selection.
    session.configure_commentary.assert_not_called()
    session.preview_voice.assert_not_called()


def test_valid_endpoint_still_requires_separate_key_file(dialog):
    widget, settings, session, warning = dialog
    widget.provider.setCurrentIndex(widget.provider.findData("chatterbox_remote"))
    widget.fields["chatterbox_server_url"].setText("https://speech.example/v1")
    assert not widget._apply()
    assert "separate speech-key file" in warning.call_args.args[2]
    assert settings["tts_provider"] == "kokoro"
    session.configure_commentary.assert_not_called()


def test_configured_remote_saves_normalized_url_and_previews(dialog, tmp_path):
    widget, settings, session, warning = dialog
    key = tmp_path / "speech.key"
    key.write_text("test-only-key")
    key.chmod(0o600)
    widget.provider.setCurrentIndex(widget.provider.findData("chatterbox_remote"))
    widget.fields["chatterbox_server_url"].setText("https://speech.example/v1")
    widget.fields["chatterbox_key_file"].setText(str(key))
    widget._preview()
    assert settings["chatterbox_server_url"] == "https://speech.example/v1/audio/speech"
    assert settings["tts_provider"] == "chatterbox_remote"
    session.preview_voice.assert_called_once()
    warning.assert_not_called()


def test_invalid_remote_url_remains_rejected_without_exposing_its_content(dialog):
    widget, settings, session, warning = dialog
    widget.provider.setCurrentIndex(widget.provider.findData("chatterbox_remote"))
    widget.fields["chatterbox_server_url"].setText(
        "https://user:TEST_SECRET@speech.example/v1?key=TEST_SECRET"
    )
    assert not widget._apply()
    assert "TEST_SECRET" not in warning.call_args.args[2]
    assert settings["tts_provider"] == "kokoro"
    session.configure_commentary.assert_not_called()


def test_local_provider_does_not_validate_remote_endpoint(dialog, monkeypatch):
    widget, settings, session, warning = dialog
    widget.provider.setCurrentIndex(widget.provider.findData("chatterbox_local"))
    monkeypatch.setattr(ui, "speech_endpoint", Mock(side_effect=AssertionError("inactive remote validated")))
    assert not widget._apply()
    assert "Python executable" in warning.call_args.args[2]
    assert settings["tts_provider"] == "kokoro"


def test_cancel_has_no_settings_or_session_effects(dialog):
    widget, settings, session, warning = dialog
    before = dict(settings)
    widget.provider.setCurrentIndex(widget.provider.findData("chatterbox_remote"))
    widget.reject()
    assert settings == before
    assert not session.mock_calls
    warning.assert_not_called()


def test_controls_explain_inactive_booth_and_provider_requirements(dialog):
    widget, settings, session, warning = dialog
    assert not widget.cast.isEnabled() and not widget.persona.isEnabled()
    assert "Choose Arena On Air commentary" in widget.mode_note.text()
    assert not widget.fields["chatterbox_server_url"].isEnabled()
    assert "no Chatterbox server or key" in widget.provider_note.text()
    widget.mode.setCurrentIndex(widget.mode.findData("commentary"))
    assert "one voice" in widget.mode_note.text()
    assert not widget.cast.isEnabled()
    widget.generation.setCurrentIndex(widget.generation.findData(True))
    assert widget.cast.isEnabled() and widget.persona.isEnabled()
    widget.provider.setCurrentIndex(widget.provider.findData("chatterbox_remote"))
    assert widget.fields["chatterbox_server_url"].isEnabled()
    assert not widget.fields["chatterbox_local_python"].isEnabled()
    assert "separate speech-key file" in widget.provider_note.text()
    assert not session.mock_calls


@pytest.mark.parametrize("field", ["commentary_python", "commentary_aoa_source"])
def test_active_commentary_rejects_invalid_runtime_path_before_saving(dialog, tmp_path, field):
    widget, settings, session, warning = dialog
    before = dict(settings)
    widget.mode.setCurrentIndex(widget.mode.findData("commentary"))
    widget.generation.setCurrentIndex(widget.generation.findData(True))
    widget.fields[field].setText(str(tmp_path / "missing"))
    assert not widget._apply()
    assert settings == before and not session.mock_calls
    assert widget.tabs.currentWidget() is widget.runtime_tab
    warning.assert_called_once()


def test_inactive_commentary_runtime_does_not_block_action_narration(dialog, tmp_path):
    widget, settings, session, warning = dialog
    for field in ("commentary_python", "commentary_aoa_source"):
        widget.fields[field].setText(str(tmp_path / "missing"))
    assert widget._apply()
    session.preview_voice.assert_not_called()
    warning.assert_not_called()


def test_save_and_reopen_preserves_choices_without_starting_audio(dialog, tmp_path):
    widget, settings, session, warning = dialog
    source = tmp_path / "aoa" / "src" / "arenaonair"
    source.mkdir(parents=True)
    (source / "llm_booth.py").write_text("# isolated fixture; never imported")
    widget.mode.setCurrentIndex(widget.mode.findData("commentary"))
    widget.generation.setCurrentIndex(widget.generation.findData(True))
    widget.fields["commentary_aoa_source"].setText(str(source.parents[1]))
    widget._save()
    session.configure_commentary.assert_called_once()
    session.preview_voice.assert_not_called()
    reopened = ui.CommentarySettingsDialog(settings, session)
    try:
        assert reopened.mode.currentData() == "commentary"
        assert reopened.generation.currentData() is True
        assert reopened.provider.currentData() == "kokoro"
        assert reopened.persona.currentData() == "esports"
        assert reopened.fields["commentary_aoa_source"].text() == str(source.parents[1])
    finally:
        reopened.close()


def test_local_runtime_requires_executable_not_just_existing_file(dialog, tmp_path):
    widget, settings, session, warning = dialog
    python = tmp_path / "python"
    python.write_text("not executable")
    python.chmod(0o600)
    widget.provider.setCurrentIndex(widget.provider.findData("chatterbox_local"))
    widget.fields["chatterbox_local_python"].setText(str(python))
    assert not widget._apply()
    assert "Python executable" in warning.call_args.args[2]
    assert not session.mock_calls
