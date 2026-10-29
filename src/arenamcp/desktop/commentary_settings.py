"""One speech control surface, backed by automatic discovery of existing setup."""

from __future__ import annotations

import threading
from dataclasses import replace

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from arenamcp.chatterbox_remote import ChatterboxError
from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.setup import VOICE_LABELS, discover, server_voices
from arenamcp.kokoro_voices import KOKORO_VOICES


def combo(options, value):
    widget = QComboBox()
    for key, label in options:
        widget.addItem(label, key)
    widget.setCurrentIndex(max(0, widget.findData(value)))
    return widget


class _CatalogSignals(QObject):
    finished = Signal(object)


class CommentarySettingsDialog(QDialog):
    def __init__(self, settings, session, parent=None):
        super().__init__(parent)
        self.settings, self.session = settings, session
        self.setup = discover(settings)
        self.config = CommentaryConfig.from_settings(settings)
        self.catalog = None
        self.catalog_pending = False
        self.catalog_error = ""
        self.setWindowTitle("Speech & voices")
        self.resize(470, 340)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)
        self.enabled = QCheckBox("Speech on")
        self.enabled.setChecked(not bool(settings.get("muted", False)))
        self.provider = combo(
            [("kokoro", "Kokoro"), ("chatterbox", "Chatterbox")],
            "chatterbox"
            if str(settings.get("tts_provider", "kokoro")).startswith("chatterbox")
            else "kokoro",
        )
        self.mode = combo(
            [("actions", "Coach advice"), ("commentary", "Announcers narrate the game")], self.config.mode
        )
        self.server = QCheckBox("Server Chatterbox")
        self.server.setChecked(settings.get("speech_server_chatterbox", self.setup.prefer_server))
        self.voice = QComboBox()
        # Older per-role Chatterbox values were troubleshooting state. Only the
        # new explicit selector is a preference; otherwise the server decides.
        self.override = str(settings.get("chatterbox_voice_override") or "")
        self.kokoro_voice = str(settings.get("voice") or "am_adam")
        for label, control in (
            ("", self.enabled),
            ("Voice engine", self.provider),
            ("Hear", self.mode),
            ("", self.server),
            ("Voice", self.voice),
        ):
            form.addRow(label, control)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.preview = QPushButton("Preview")
        self.preview.clicked.connect(self._preview)
        layout.addWidget(self.preview)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.catalog_signals = _CatalogSignals(self)
        self.catalog_signals.finished.connect(self._catalog_ready)
        self.provider.currentIndexChanged.connect(self._update_controls)
        self.server.toggled.connect(self._update_controls)
        self.voice.currentIndexChanged.connect(self._voice_changed)
        self.enabled.toggled.connect(self._update_status)
        self.mode.currentIndexChanged.connect(self._update_status)
        self._update_controls()

    def _voice_changed(self):
        if self.provider.currentData() == "kokoro":
            self.kokoro_voice = str(self.voice.currentData() or "am_adam")
        else:
            self.override = str(self.voice.currentData() or "")

    def _update_controls(self):
        chatterbox = self.provider.currentData() == "chatterbox"
        self.server.setVisible(chatterbox)
        self.voice.blockSignals(True)
        self.voice.clear()
        if not chatterbox:
            options, selected = KOKORO_VOICES, self.kokoro_voice
        elif self.server.isChecked():
            default = (self.catalog or {}).get("default")
            label = "Server default" + (f" ({default.title()})" if default else "")
            options = [("", label)] + [
                (key, VOICE_LABELS[key]) for key in (self.catalog or {}).get("voices", [])
            ]
            selected = self.override
        else:
            options = [("", "Chatterbox original voice")] + [
                (key, VOICE_LABELS[key]) for key in ("atlas", "mira")
            ]
            selected = self.override
        for key, label in options:
            self.voice.addItem(label, key)
        self.voice.setCurrentIndex(max(0, self.voice.findData(selected)))
        self.voice.blockSignals(False)
        if (
            chatterbox
            and self.server.isChecked()
            and self.setup.server_configured
            and self.catalog is None
            and not self.catalog_pending
            and not self.catalog_error
        ):
            self.catalog_pending = True
            signals, endpoint = self.catalog_signals, self.setup.values["chatterbox_server_url"]

            def read():
                try:
                    value = server_voices(endpoint)
                except ChatterboxError as error:
                    value = {"error": str(error)}
                try:
                    signals.finished.emit(value)
                except RuntimeError:
                    pass  # The dialog was closed during the bounded read.

            threading.Thread(target=read, daemon=True, name="speech-voice-list").start()
        self._update_status()

    def _catalog_ready(self, result):
        self.catalog_pending = False
        if "error" in result:
            self.catalog_error = result["error"]
        else:
            self.catalog = result
        self._update_controls()

    def _problem(self, *, provider_only=False):
        if self.provider.currentData() == "chatterbox":
            if self.server.isChecked() and not self.setup.server_configured:
                return "Server Chatterbox isn't configured. Connect your speech server in ArenaOnAir; Coach will reuse that setup automatically."
            if not self.server.isChecked() and not self.setup.local_configured:
                return "Local Chatterbox isn't installed on this computer. Turn on Server Chatterbox to use your existing server, or choose Kokoro."
        if (
            not provider_only
            and self.mode.currentData() == "commentary"
            and not self.setup.announcers_available
        ):
            return "Announcers need ArenaOnAir installed on this computer. Coach advice is available now."
        return ""

    def _update_status(self):
        problem = self._problem()
        self.preview.setEnabled(not self._problem(provider_only=True))
        self.status.setText(
            problem
            or self.catalog_error
            or (
                "Loading voices from your speech server…"
                if self.catalog_pending
                else "Preview auditions this voice without saving or turning speech on. Save applies your choices. Announcers replace spoken Coach advice; essential alerts still get through."
            )
        )

    def _values(self):
        values = dict(self.setup.values)
        chatterbox = self.provider.currentData() == "chatterbox"
        values.update(
            tts_provider=("chatterbox_remote" if self.server.isChecked() else "chatterbox_local")
            if chatterbox
            else "kokoro",
            speech_server_chatterbox=self.server.isChecked(),
            chatterbox_voice_override=str(self.voice.currentData() or "") if chatterbox else self.override,
            voice=str(self.voice.currentData()) if not chatterbox else self.kokoro_voice,
        )
        values["chatterbox_voice"] = values["chatterbox_voice_override"] or (
            "server_default" if self.server.isChecked() else "default"
        )
        values["commentary_chatterbox_analyst"] = values["chatterbox_voice"]
        values["chatterbox_speed"] = self.settings.get("chatterbox_speed", 1.0)
        values["voice_speed"] = self.settings.get("voice_speed", 1.0)
        return values

    def _apply(self):
        if self.enabled.isChecked() and self._problem():
            self._update_status()
            return False
        values = self._values()
        config = replace(
            self.config,
            mode=self.mode.currentData(),
            generative=True,
            muted=False,
            output_owner="coach",
            python=values["commentary_python"],
            aoa_source=values["commentary_aoa_source"],
        )
        values.update(autoplay_speech=config.mode, muted=not self.enabled.isChecked())
        values.update(
            {"commentary_" + key: value for key, value in config.to_wire().items() if key != "mode"}
        )
        for key, value in values.items():
            self.settings.set(key, value)
        self.session.configure_commentary(config.to_wire())
        self.session.set_speech_enabled(self.enabled.isChecked())
        self.session.set_voice(values["voice"])
        return True

    def _save(self):
        if self._apply():
            self.accept()

    def _preview(self):
        if not self._problem(provider_only=True):
            self.session.preview_voice(self._values())
