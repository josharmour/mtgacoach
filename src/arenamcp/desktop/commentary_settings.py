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

from arenamcp.chatterbox_remote import HOSTED_BASE, ChatterboxError
from arenamcp.commentary import qwen_public
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
    local_ready = Signal(bool)


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
        self.private = QCheckBox("Discuss my hand and deck")
        self.private.setChecked(self.config.private_information)
        self.public_label = QLabel("Public announcers")
        self.public_voice = combo([("kokoro", "Kokoro voices")], "kokoro")
        self.saved_public_voice = settings.get("commentary_public_voice", "kokoro")
        self.local_available = False
        self.speed_label = QLabel("Speed")
        self.voice = QComboBox()
        self.analyst = QComboBox()
        self.analyst_label = QLabel("Color analyst")
        self.voice_label = QLabel("Voice")
        self.style_label = QLabel("Style")
        self.style = combo(list(PERSONAS.items()), self.config.persona)
        self.style.setToolTip(
            "Hype: playful reactions and rising energy on big moments. "
            "Technical: measured delivery, dry wit and precise strategy. "
            "Both casters follow the changing game, with a distinct analyst voice."
        )
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
            (self.style_label, self.style),
            ("", self.private),
            (self.public_label, self.public_voice),
            (self.voice_label, self.voice),
            (self.analyst_label, self.analyst),
            (self.speed_label, self.speed),
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
        self.voice.currentIndexChanged.connect(self._voice_changed)
        self.analyst.currentIndexChanged.connect(self._analyst_changed)
        self.enabled.toggled.connect(self._update_status)
        self.mode.currentIndexChanged.connect(self._update_controls)
        self.public_voice.currentIndexChanged.connect(self._update_controls)
        self.private.toggled.connect(self._update_controls)
        self.catalog_signals.local_ready.connect(self._local_ready)
        if self.saved_public_voice == qwen_public.PROVIDER:
            self.public_voice.addItem("Qwen3 · checking local pack…", qwen_public.PROVIDER)
            self.public_voice.setCurrentIndex(1)
        if qwen_public.command() is not None:
            signals = self.catalog_signals

            def probe_local():
                ready = qwen_public.available()
                try:
                    signals.local_ready.emit(ready)
                except RuntimeError:
                    pass  # The dialog was closed.

            threading.Thread(target=probe_local, daemon=True, name="local-voice-probe").start()
        else:
            self._local_ready(False)
        self._update_controls()

    def _local_ready(self, available):
        self.local_available = available
        pos = self.public_voice.findData(qwen_public.PROVIDER)
        label = "Qwen3 · Ryan & Serena (on device)" if available else "Qwen3 · local pack unavailable"
        if pos >= 0:
            self.public_voice.setItemText(pos, label)
        elif available:
            self.public_voice.addItem(label, qwen_public.PROVIDER)
        self._update_controls()

    def _expressive(self):
        return (
            self._kokoro_duo()
            and not self.private.isChecked()
            and self.public_voice.currentData() == qwen_public.PROVIDER
        )

    def _kokoro_duo(self):
        return self.mode.currentData() == "commentary" and self.provider.currentData() == "kokoro"

    def _set_analysts(self, lead, preferred):
        self.analyst.blockSignals(True)
        self.analyst.clear()
        for key, label in KOKORO_VOICES:
            if key != lead:
                self.analyst.addItem(label, key)
        if self.analyst.findData(preferred) < 0:
            preferred = "af_heart" if lead != "af_heart" else "am_adam"
        self.analyst.setCurrentIndex(self.analyst.findData(preferred))
        self.analyst.blockSignals(False)

    def _analyst_changed(self):
        if self._kokoro_duo():
            self.pairs["kokoro"] = f"{self.voice.currentData()}|{self.analyst.currentData()}"

    def _voice_changed(self):
        if self._kokoro_duo():
            self._set_analysts(self.voice.currentData(), self.analyst.currentData())
            self._analyst_changed()
            return
        if self.mode.currentData() == "commentary":
            self.pairs[self.provider.currentData()] = str(self.voice.currentData() or "")
        elif self.provider.currentData() == "kokoro":
            self.kokoro_voice = str(self.voice.currentData() or "am_adam")
        else:
            self.override = str(self.voice.currentData() or "")

    def _update_controls(self):
        chatterbox = self.provider.currentData() == "chatterbox"
        self.voice.blockSignals(True)
        self.voice.clear()
        duo = self.mode.currentData() == "commentary"
        self.voice_label.setText("Voices" if duo else "Voice")
        kokoro_duo = self._kokoro_duo()
        choice_visible = kokoro_duo and (
            self.local_available or self.saved_public_voice == qwen_public.PROVIDER
        )
        self.public_label.setVisible(choice_visible)
        self.public_voice.setVisible(choice_visible)
        self.public_voice.setEnabled(not self.private.isChecked())
        expressive = self._expressive()
        self.voice.setVisible(not expressive)
        self.voice_label.setVisible(not expressive)
        self.speed_label.setText("Target pace" if expressive else "Speed")
        self.speed.setToolTip("Guides the local announcers’ speaking pace." if expressive else "")
        self.analyst_label.setVisible(kokoro_duo and not expressive)
        self.analyst.setVisible(kokoro_duo and not expressive)
        self.private.setVisible(duo)
        self.style.setVisible(duo)
        self.style_label.setVisible(duo)
        self.pair_error = ""
        if kokoro_duo:
            self.voice_label.setText("Play-by-play")
            prefs = {
                "tts_provider": "kokoro",
                "voice": self.kokoro_voice,
                "commentary_kokoro_pair": self.pairs["kokoro"],
            }
            try:
                lead, analyst = voice_pair(prefs)
            except VoicePairUnavailable:
                self.pairs["kokoro"] = ""
                lead, analyst = voice_pair({"voice": self.kokoro_voice})
            options, selected = KOKORO_VOICES, lead
            self._set_analysts(lead, analyst)
        elif duo:
            provider = "chatterbox_remote" if chatterbox else "kokoro"
            try:
                pairs = pair_options({"tts_provider": provider, "voice": self.kokoro_voice}, self.catalog)
                options = [(key, label) for key, label, _voices in pairs]
            except VoicePairUnavailable as error:
                options = [("", "Waiting for server voice pair")]
                self.pair_error = str(error)
            selected = self.pairs[self.provider.currentData()]
        elif not chatterbox:
            options, selected = KOKORO_VOICES, self.kokoro_voice
        else:
            default = (self.catalog or {}).get("default")
            label = "Server default" + (f" ({default.title()})" if default else "")
            options = [("", label)] + [
                (key, VOICE_LABELS[key]) for key in (self.catalog or {}).get("voices", [])
            ]
            selected = self.override
        for key, label in options:
            self.voice.addItem(label, key)
        self.voice.setCurrentIndex(max(0, self.voice.findData(selected)))
        self.voice.blockSignals(False)
        if (
            chatterbox
            and self.setup.server_configured
            and self.catalog is None
            and not self.catalog_pending
            and not self.catalog_error
        ):
            self.catalog_pending = True
            signals = self.catalog_signals
            api_key = str(self.settings.get("license_key") or "")

            def read():
                try:
                    value = server_voices(api_key=api_key)
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
        if self._expressive() and not self.local_available:
            return "The local expressive voice pack is unavailable. Choose Kokoro voices or install the Mac voice pack."
        if self.provider.currentData() == "chatterbox" and not self.setup.server_configured:
            return "Sign in to Coach to use Chatterbox."
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
                "Ryan calls the play and Serena offers analysis. Their delivery follows the moment; Preview auditions both."
                if self._expressive()
                else ""
            )
            or (
                "Hand and deck commentary uses your Kokoro pair."
                if self._kokoro_duo()
                and self.private.isChecked()
                and self.public_voice.currentData() == qwen_public.PROVIDER
                else ""
            )
            or (
                "Loading Chatterbox voices…"
                if self.catalog_pending
                else "Preview auditions the selected voice or both announcers without saving. Announcers use play-by-play then analyst; advice stays silent. Server default + partner keeps the server lead and adds a distinct available voice."
            )
        )

    def _values(self):
        values = dict(self.setup.values)
        chatterbox = self.provider.currentData() == "chatterbox"
        duo = self.mode.currentData() == "commentary"
        values.update(
            tts_provider="chatterbox_remote" if chatterbox else "kokoro",
            speech_server_chatterbox=True,
            chatterbox_server_url=HOSTED_BASE + "/speech",
            chatterbox_key_file="",
            autoplay_speech=self.mode.currentData(),
            commentary_persona=self.style.currentData(),
            commentary_public_voice=self.public_voice.currentData(),
            commentary_private_information=self.private.isChecked(),
            commentary_speakers=2,
            commentary_kokoro_pair=self.pairs["kokoro"],
            commentary_chatterbox_pair=self.pairs["chatterbox"],
            chatterbox_voice_override=str(self.voice.currentData() or "")
            if chatterbox and not duo
            else self.override,
            voice=str(self.voice.currentData()) if not chatterbox and not duo else self.kokoro_voice,
        )
        values["chatterbox_voice"] = values["chatterbox_voice_override"] or "server_default"
        if duo and chatterbox and not self.pair_error:
            values["commentary_chatterbox_pair"] = str(self.voice.currentData() or "")
        # A failed/pending catalog leaves a placeholder in the selector. Keep
        # the saved pair until real choices are available again.
        values["speech_speed"] = float(self.speed.currentData())
        values["chatterbox_speed"] = values["voice_speed"] = values["speech_speed"]
        return values

    def _apply(self):
        # Availability controls playback, not whether preferences can be saved
        # (including turning speech off while the server is unreachable).
        values = self._values()
        config = replace(
            self.config,
            mode=self.mode.currentData(),
            speakers=2,
            persona=self.style.currentData(),
            focus="analysis",
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
            if self.mode.currentData() == "commentary":
                voice_pair(options, self.catalog)
            if self.catalog is not None:
                options["_commentary_voice_catalog"] = self.catalog
            self.session.preview_voice(options)
