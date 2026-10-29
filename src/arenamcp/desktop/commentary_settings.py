"""Opt-in commentary/voice controls; saving applies without an engine restart."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from arenamcp.chatterbox_remote import ChatterboxError, read_speech_key, speech_endpoint
from arenamcp.commentary.config import PERSONAS, CommentaryConfig
from arenamcp.commentary.local_voice_worker import validate_model


def combo(options, value):
    widget = QComboBox()
    for key, label in options:
        widget.addItem(label, key)
    widget.setCurrentIndex(max(0, widget.findData(value)))
    return widget


class CommentarySettingsDialog(QDialog):
    def __init__(self, settings, session, parent=None):
        super().__init__(parent)
        self.settings, self.session = settings, session
        self.setWindowTitle("Arena On Air & Voice")
        self.resize(570, 640)
        root = QVBoxLayout(self)
        tabs = QTabWidget()
        self.tabs = tabs
        root.addWidget(tabs)
        basic, advanced = QWidget(), QWidget()
        self.runtime_tab = advanced
        form, runtime = QFormLayout(basic), QFormLayout(advanced)
        tabs.addTab(basic, "Commentary & voice")
        tabs.addTab(advanced, "Runtime & connections")
        config = CommentaryConfig.from_settings(settings)
        self.mode = combo(
            [
                ("actions", "Action narration"),
                ("commentary", "Arena On Air commentary"),
                ("essential", "Essential messages only"),
            ],
            config.mode,
        )
        self.persona = combo(list(PERSONAS.items()), config.persona)
        self.cast = combo([(1, "One commentator"), (2, "Two commentators")], config.speakers)
        self.focus = combo([(v, v.title()) for v in ("calls", "balanced", "analysis")], config.focus)
        self.rate = QSpinBox()
        self.rate.setRange(1, 4)
        self.rate.setValue(config.exchanges_per_minute)
        self.generation = combo(
            [(False, "Grounded event calls"), (True, "Audited generative booth")], config.generative
        )
        self.muted = QCheckBox("Mute commentary only")
        self.muted.setChecked(config.muted)
        self.private = QCheckBox("Allow my hand and deck in commentary/model context")
        self.private.setChecked(config.private_information)
        self.owner = combo(
            [
                ("coach", "Coach owns commentary audio"),
                ("standalone", "Standalone Arena On Air owns commentary audio"),
            ],
            config.output_owner,
        )
        self.provider = combo(
            [
                ("kokoro", "Kokoro"),
                ("chatterbox_remote", "Chatterbox server"),
                ("chatterbox_local", "Chatterbox on this computer"),
            ],
            settings.get("tts_provider", "kokoro"),
        )
        self.voice = combo(
            [(v, v.title()) for v in ("atlas", "mira", "default")], settings.get("chatterbox_voice", "atlas")
        )
        self.analyst = combo(
            [(v, v.title()) for v in ("atlas", "mira", "default")],
            settings.get("commentary_chatterbox_analyst", "mira"),
        )
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.5, 2)
        self.speed.setSingleStep(0.1)
        self.speed.setValue(float(settings.get("chatterbox_speed", 1)))
        for label, widget in (
            ("Autoplay speech", self.mode),
            ("Style", self.persona),
            ("Cast", self.cast),
            ("Balance", self.focus),
            ("Exchanges per minute", self.rate),
            ("Commentary", self.generation),
            ("", self.muted),
            ("", self.private),
            ("Audio ownership", self.owner),
            ("Voice engine", self.provider),
            ("Chatterbox lead voice", self.voice),
            ("Chatterbox analyst voice", self.analyst),
            ("Chatterbox speed", self.speed),
        ):
            form.addRow(label, widget)
        self.mode_note = QLabel()
        self.mode_note.setWordWrap(True)
        form.addRow(self.mode_note)
        self.provider_note = QLabel()
        self.provider_note.setWordWrap(True)
        form.addRow(self.provider_note)
        note = QLabel(
            "Kokoro keeps its existing voice and speed settings. Choose Coach ownership only when standalone Arena On Air playback is off. This control never stops another application."
        )
        note.setWordWrap(True)
        form.addRow(note)
        self.fields = {}
        auto_source = Path(__file__).resolve().parents[4] / "arenaonair"
        definitions = [
            ("commentary_python", "Commentary Python (blank: Coach runtime)", ""),
            (
                "commentary_aoa_source",
                "Arena On Air source folder (or installed package)",
                str(auto_source) if auto_source.is_dir() else "",
            ),
            ("chatterbox_server_url", "Speech server URL", ""),
            ("chatterbox_key_file", "Speech-only key file", ""),
            ("chatterbox_local_python", "Local voice Python (separate environment)", ""),
            ("chatterbox_model_dir", "Turbo model snapshot folder", ""),
            (
                "chatterbox_model_revision",
                "Pinned model revision",
                "749d1c1a46eb10492095d68fbcf55691ccf137cd",
            ),
            ("commentary_kokoro_analyst", "Kokoro analyst voice ID", "am_onyx"),
        ]
        for key, label, default in definitions:
            field = QLineEdit(str(settings.get(key) or default))
            self.fields[key] = field
            runtime.addRow(label, field)
        self.device = combo(
            [(v, v.upper()) for v in ("cpu", "mps", "cuda", "auto")], settings.get("chatterbox_device", "cpu")
        )
        runtime.addRow("Local inference device", self.device)
        limits = QLabel(
            "Generative commentary uses Coach's backend: 1 exchange at a time, 2 calls, 6 seconds, 6,000 input tokens per call, 512/256 output tokens, and 12 request-seconds per minute. Strategy and gameplay preempt it. Local Turbo requires the separate locked runtime and a downloaded pinned snapshot; nothing is downloaded here. Speculative synthesis stays off until hardware qualification."
        )
        limits.setWordWrap(True)
        runtime.addRow(limits)
        preview = QPushButton("Apply and preview voice")
        preview.clicked.connect(self._preview)
        root.addWidget(preview)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        for control in (self.mode, self.generation, self.provider):
            control.currentIndexChanged.connect(self._update_controls)
        self._update_controls()

    def _update_controls(self):
        commentary = self.mode.currentData() == "commentary"
        generative = self.generation.currentData() is True
        self.generation.setEnabled(commentary)
        for control in (self.persona, self.cast, self.focus):
            control.setEnabled(commentary and generative)
        self.mode_note.setText(
            "Action narration is selected. Choose Arena On Air commentary above to enable the booth."
            if self.mode.currentData() == "actions"
            else "Essential messages only is selected. The commentary booth is off."
            if not commentary
            else "Grounded event calls use one voice. Styles and two-commentator exchanges require Audited generative booth."
            if not generative
            else "Audited generative booth uses Arena On Air and Coach's model backend. Unsupported or late lines are dropped."
        )
        provider = self.provider.currentData()
        remote, local = provider == "chatterbox_remote", provider == "chatterbox_local"
        self.provider_note.setText(
            "Kokoro uses your existing voice setup; no Chatterbox server or key is needed."
            if provider == "kokoro"
            else "Chatterbox server requires a speech server URL and a separate speech-key file under Runtime & connections."
            if remote
            else "Local Chatterbox requires a separate Python environment and pinned model snapshot under Runtime & connections."
        )
        for control in (self.voice, self.analyst, self.speed):
            control.setEnabled(remote or local)
        for key in ("chatterbox_server_url", "chatterbox_key_file"):
            self.fields[key].setEnabled(remote)
        for key in ("chatterbox_local_python", "chatterbox_model_dir", "chatterbox_model_revision"):
            self.fields[key].setEnabled(local)
        self.device.setEnabled(local)
        self.fields["commentary_kokoro_analyst"].setEnabled(provider == "kokoro")

    @staticmethod
    def _is_executable(value):
        path = Path(value).expanduser()
        return bool(value) and path.is_file() and os.access(path, os.X_OK)

    def _apply(self):
        provider = self.provider.currentData()
        values = {key: field.text().strip() for key, field in self.fields.items()}
        focus_key = None
        try:
            if self.mode.currentData() == "commentary":
                focus_key = "commentary_python"
                if values[focus_key] and not self._is_executable(values[focus_key]):
                    raise ValueError(
                        "Choose an executable Commentary Python, or leave it blank to use Coach's runtime."
                    )
                focus_key = "commentary_aoa_source"
                if self.generation.currentData() and values[focus_key]:
                    source = Path(values[focus_key]).expanduser()
                    if not any(
                        (root / "arenaonair" / "llm_booth.py").is_file() for root in (source, source / "src")
                    ):
                        raise ValueError(
                            "Choose the Arena On Air source folder containing arenaonair/llm_booth.py, or leave it blank to use an installed package."
                        )
            if provider == "chatterbox_remote":
                focus_key = "chatterbox_server_url"
                if not values[focus_key]:
                    raise ChatterboxError(
                        "Chatterbox server is selected, but its speech server URL is empty. "
                        "Configure the server URL and separate speech-key file under Runtime & connections, "
                        "or choose Kokoro under Commentary & voice to use your existing voice without a server."
                    )
                values["chatterbox_server_url"] = speech_endpoint(values["chatterbox_server_url"])
                focus_key = "chatterbox_key_file"
                if not values[focus_key]:
                    raise ChatterboxError(
                        "Chatterbox server requires a separate speech-key file. "
                        "Choose that file under Runtime & connections, or select Kokoro to use your existing voice."
                    )
                read_speech_key(values[focus_key])
            if provider == "chatterbox_local":
                focus_key = "chatterbox_local_python"
                if not self._is_executable(values["chatterbox_local_python"]):
                    raise ValueError("Choose the separate Chatterbox Python executable.")
                focus_key = "chatterbox_model_dir"
                validate_model(values["chatterbox_model_dir"], values["chatterbox_model_revision"])
        except (ChatterboxError, ValueError) as error:
            if focus_key:
                self.tabs.setCurrentWidget(self.runtime_tab)
                self.fields[focus_key].setFocus()
            QMessageBox.warning(self, "Voice configuration", str(error))
            return False
        config = CommentaryConfig(
            mode=self.mode.currentData(),
            persona=self.persona.currentData(),
            speakers=self.cast.currentData(),
            focus=self.focus.currentData(),
            exchanges_per_minute=self.rate.value(),
            generative=self.generation.currentData(),
            private_information=self.private.isChecked(),
            muted=self.muted.isChecked(),
            output_owner=self.owner.currentData(),
            python=values["commentary_python"],
            aoa_source=values["commentary_aoa_source"],
        )
        values.update(
            tts_provider=provider,
            chatterbox_voice=self.voice.currentData(),
            commentary_chatterbox_analyst=self.analyst.currentData(),
            chatterbox_speed=self.speed.value(),
            chatterbox_device=self.device.currentData(),
            autoplay_speech=config.mode,
        )
        values.update(
            {"commentary_" + key: value for key, value in config.to_wire().items() if key != "mode"}
        )
        for key, value in values.items():
            self.settings.set(key, value)
        self.session.configure_commentary(config.to_wire())
        return True

    def _save(self):
        if self._apply():
            self.accept()

    def _preview(self):
        if self._apply():
            self.session.preview_voice()
