"""Nonmodal, read-only comparison of the engine's limited decks."""

from html import escape

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QTextBrowser, QVBoxLayout, QWidget


class LimitedDeckPreview(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Draft deck recommendation")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.resize(1100, 720)
        layout = QVBoxLayout(self)
        self.heading = QLabel("No draft build received yet.")
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        self.columns = QHBoxLayout()
        layout.addLayout(self.columns)
        note = QLabel(
            "Read-only preview • Autoplay continues with the recommended build. "
            "Alternatives are suggestions, not proven win-rate improvements."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

    def set_preview(self, data: dict) -> None:
        while self.columns.count():
            item = self.columns.takeAt(0)
            item.widget().deleteLater()
        self.heading.setText(f"{data.get('event', '')} — proposed deck and alternatives")
        for i, option in enumerate(data.get("options", [])):
            column = QWidget()
            layout = QVBoxLayout(column)
            title = "Recommended" if option.get("recommended") else f"Alternative {i}"
            layout.addWidget(QLabel(f"{title} · {option['total']} cards · {option['lands']} lands"))
            browser = QTextBrowser()
            browser.setOpenExternalLinks(False)
            curve = option.get("curve", {})
            text = f"<p>{escape(option.get('plan', '') or 'No plan explanation supplied.')}</p>"
            text += (
                "<p><b>Printed mana curve (spells)</b><br>"
                + " · ".join(f"{n if n < 7 else '7+'}: {curve.get(str(n), 0)}" for n in range(8))
                + "</p><h3>Main deck</h3><table width='100%'>"
            )
            for row in option["cards"]:
                text += (
                    f"<tr><td>{row['count']} × {escape(row['name'])}</td>"
                    f"<td>{escape(row.get('cost', ''))}</td></tr>"
                )
            text += "</table><h3>Left out of this build</h3>"
            for cut in option["cuts"]:
                reason = f" — {escape(cut['reason'])}" if cut.get("reason") else ""
                text += f"<p>{cut['count']} × {escape(cut['name'])}{reason}</p>"
            browser.setHtml(text)
            layout.addWidget(browser)
            self.columns.addWidget(column)
        self.show()
