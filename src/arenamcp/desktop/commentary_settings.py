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
from arenamcp.commentary.config import PERSONAS, CommentaryConfig, speech_rate
from arenamcp.commentary.setup import VOICE_LABELS, discover, server_voices
from arenamcp.commentary.voices import VoicePairUnavailable, pair_options, voice_pair
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
        self.speed = combo(
            [
                (value, f"{value:g}×" + (" (default)" if value == 1.5 else ""))
                for value in (0.75, 1.0, 1.25, 1.5, 1.75, 2.0)
            ],
            speech_rate(settings),
        )
        self.server = QCheckBox("Server Chatterbox")
        self.server.setChecked(settings.get("speech_server_chatterbox", self.setup.prefer_server))
        self.private = QCheckBox("Discuss my hand and deck")
        self.private.setChecked(self.config.private_information)
        self.voice = QComboBox()
        self.voice_label = QLabel("Voice")
        self.style_label = QLabel("Style")
        self.style = combo(list(PERSONAS.items()), self.config.persona)
        self.pairs = {
            "kokoro": str(settings.get("commentary_kokoro_pair") or ""),
            "chatterbox": str(settings.get("commentary_chatterbox_pair") or ""),
        }
        self.pair_error = ""
        # Older per-role Chatterbox values were troubleshooting state. Only the
        # new explicit selector is a preference; otherwise the server decides.
        self.override = str(settings.get("chatterbox_voice_override") or "")
        self.kokoro_voice = str(settings.get("voice") or "am_adam")
        for label, control in (
            ("", self.enabled),
            ("Voice engine", self.provider),
            ("Hear", self.mode),
            ("", self.server),
            (self.style_label, self.style),
            ("", self.private),
            (self.voice_label, self.voice),
            ("Speed", self.speed),
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
        self.mode.currentIndexChanged.connect(self._update_controls)
        self._update_controls()

    def _voice_changed(self):
        if self.mode.currentData() == "commentary":
            self.pairs[self.provider.currentData()] = str(self.voice.currentData() or "")
        elif self.provider.currentData() == "kokoro":
            self.kokoro_voice = str(self.voice.currentData() or "am_adam")
        else:
            self.override = str(self.voice.currentData() or "")

    def _update_controls(self):
        chatterbox = self.provider.currentData() == "chatterbox"
        self.server.setVisible(chatterbox)
        self.voice.blockSignals(True)
        self.voice.clear()
        duo = self.mode.currentData() == "commentary"
        self.voice_label.setText("Voices" if duo else "Voice")
        self.private.setVisible(duo)
        self.style.setVisible(duo)
        self.style_label.setVisible(duo)
        self.pair_error = ""
        if duo:
            provider = (
                ("chatterbox_remote" if self.server.isChecked() else "chatterbox_local")
                if chatterbox
                else "kokoro"
            )
            try:
                pairs = pair_options({"tts_provider": provider, "voice": self.kokoro_voice}, self.catalog)
                options = [(key, label) for key, label, _voices in pairs]
            except VoicePairUnavailable as error:
                options = [("", "Waiting for server voice pair")]
                self.pair_error = str(error)
            selected = self.pairs[self.provider.currentData()]
        elif not chatterbox:
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
        if self.mode.currentData() == "commentary" and self.pair_error:
            return self.catalog_error or self.pair_error
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
                else "Preview auditions the selected voice or both announcers without saving. Announcers use play-by-play then analyst; advice stays silent. Server default + partner keeps the server lead and adds a distinct available voice."
            )
        )

    def _values(self):
        values = dict(self.setup.values)
        chatterbox = self.provider.currentData() == "chatterbox"
        duo = self.mode.currentData() == "commentary"
        values.update(
            tts_provider=("chatterbox_remote" if self.server.isChecked() else "chatterbox_local")
            if chatterbox
            else "kokoro",
            speech_server_chatterbox=self.server.isChecked(),
            autoplay_speech=self.mode.currentData(),
            commentary_persona=self.style.currentData(),
            commentary_speakers=2,
            commentary_kokoro_pair=self.pairs["kokoro"],
            commentary_chatterbox_pair=self.pairs["chatterbox"],
            chatterbox_voice_override=str(self.voice.currentData() or "")
            if chatterbox and not duo
            else self.override,
            voice=str(self.voice.currentData()) if not chatterbox and not duo else self.kokoro_voice,
        )
        values["chatterbox_voice"] = values["chatterbox_voice_override"] or (
            "server_default" if self.server.isChecked() else "default"
        )
        if duo:
            key = "commentary_chatterbox_pair" if chatterbox else "commentary_kokoro_pair"
            values[key] = str(self.voice.currentData() or "")
            # Validation uses the current catalog; never manufacture a pair.
            voice_pair(values, self.catalog)
        values["speech_speed"] = float(self.speed.currentData())
        values["chatterbox_speed"] = values["voice_speed"] = values["speech_speed"]
        return values

    def _apply(self):
        if (self.enabled.isChecked() or self.mode.currentData() == "commentary") and self._problem():
            self._update_status()
            return False
        values = self._values()
        config = replace(
            self.config,
            mode=self.mode.currentData(),
            speakers=2,
            persona=self.style.currentData(),
            private_information=self.private.isChecked(),
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
        self.session.set_speech_speed(values["speech_speed"])
        return True

    def _save(self):
        if self._apply():
            self.accept()

    def _preview(self):
        if not self._problem(provider_only=True):
            options = self._values()
            if self.catalog is not None:
                options["_commentary_voice_catalog"] = self.catalog
            self.session.preview_voice(options)
