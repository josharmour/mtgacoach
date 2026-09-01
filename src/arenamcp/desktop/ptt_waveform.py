"""Passive voice-activity indicator widget for the desktop UI."""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget


class PTTWaveformWidget(QFrame):
    """Passive voice-activity indicator.

    Push-to-talk audio capture lives in the coach process (hold F4 while it runs);
    this widget only mirrors the backend's speech events — it does not start or
    stop capture itself.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setToolTip(
            "Voice activity indicator — lights up while advice is being "
            "spoken. Push-to-talk capture is handled by the coach process "
            "(hold F4)."
        )
        self.setStyleSheet(
            "QFrame { background: rgba(30, 30, 46, 0.9); border: 1px solid rgba(137, 180, 250, 0.4); border-radius: 6px; padding: 4px 10px; }"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(8)

        self._icon_lbl = QLabel("🎙")
        self._icon_lbl.setStyleSheet("font-size: 16px;")
        layout.addWidget(self._icon_lbl)

        self._status_lbl = QLabel("PTT: F4 (Ready)")
        self._status_lbl.setStyleSheet("font-weight: 600; color: #89b4fa; font-size: 12px;")
        layout.addWidget(self._status_lbl)

        self._bars_lbl = QLabel(" ▂▃▅▇ ")
        self._bars_lbl.setStyleSheet("color: #a6e3a1; font-weight: bold; font-family: monospace;")
        layout.addWidget(self._bars_lbl)

    def set_status(self, text: str, active: bool = False, speaking: bool = False) -> None:
        if speaking:
            self._icon_lbl.setText("🔊")
            self._status_lbl.setText("SPEAKING...")
            self._status_lbl.setStyleSheet("font-weight: 700; color: #a6e3a1; font-size: 12px;")
            self._bars_lbl.setText("▃▅▇▅▃")
        elif active:
            self._icon_lbl.setText("🎙")
            self._status_lbl.setText("LISTENING...")
            self._status_lbl.setStyleSheet("font-weight: 700; color: #f9e2af; font-size: 12px;")
            self._bars_lbl.setText("▇▅▃▅▇")
        else:
            self._icon_lbl.setText("🎙")
            self._status_lbl.setText(text or "PTT: F4 (Ready)")
            self._status_lbl.setStyleSheet("font-weight: 600; color: #89b4fa; font-size: 12px;")
            self._bars_lbl.setText(" ▂▃▅▇ ")
