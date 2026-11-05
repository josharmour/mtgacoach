"""Bounded text and paged detail for the resizable, scroll-free companion."""

from __future__ import annotations

import math
from html.parser import HTMLParser

from PySide6.QtCore import QEvent, QSignalBlocker, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QTextLayout, QTextOption
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from . import theme


class _PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "p", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("p", "div"):
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(text: str) -> str:
    # Status snippets only need text extraction, not a native rich-text document.
    parser = _PlainHTML()
    parser.feed(text)
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


class SummaryLabel(QLabel):
    """Stable line budget, with full text available to accessibility and tooltips."""

    def __init__(self, text="", *, lines=1, role="small", parent=None):
        super().__init__(text, parent)
        self.lines = lines
        self.setProperty("role", role)
        self.setTextFormat(Qt.PlainText)
        self.setWordWrap(True)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._measure()

    def _measure(self):
        self.setFixedHeight(self.fontMetrics().lineSpacing() * self.lines + 4)

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._measure()

    def sizeHint(self):  # noqa: N802
        return QSize(80, self.height())

    def setText(self, text):  # noqa: N802
        super().setText(text)
        self.setToolTip(text)

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        tone = self.property("tone") or ("muted" if self.property("role") == "small" else "text")
        painter.setPen(theme.qcolor(tone))
        rect = self.contentsRect()
        text = self.text().replace("\n", " ")
        layout = QTextLayout(text, self.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        layout.beginLayout()
        y = rect.top()
        for index in range(self.lines):
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(rect.width())
            start = line.textStart()
            remaining = text[start:]
            drawn = (
                self.fontMetrics().elidedText(remaining, Qt.ElideRight, rect.width())
                if index == self.lines - 1
                else text[start : start + line.textLength()]
            )
            painter.drawText(rect.left(), y + self.fontMetrics().ascent(), drawn)
            y += self.fontMetrics().lineSpacing()
        layout.endLayout()


class SummaryButton(QPushButton):
    """A compact clickable summary which never dictates the window width."""

    leading = False

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)

    def paintEvent(self, event):  # noqa: N802
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = "\n".join(
            self.fontMetrics().elidedText(line, Qt.ElideRight, max(0, self.width() - 28))
            for line in self.text().splitlines()
        )
        painter = QPainter(self)
        self.style().drawControl(QStyle.CE_PushButton, option, painter, self)
        if self.leading:
            from PySide6.QtGui import QPen

            if theme.tokens().name != theme.THEME_HIGH_CONTRAST:
                for width, alpha in ((9, 20), (6, 35)):
                    color = theme.qcolor("good")
                    color.setAlpha(alpha)
                    painter.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap))
                    painter.drawLine(7, 10, 7, self.height() - 10)
            painter.setPen(QPen(theme.qcolor("good"), 3, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(7, 10, 7, self.height() - 10)


class _PageBrowser(QTextBrowser):
    pageRequested = Signal(int)

    def wheelEvent(self, event):  # noqa: N802
        event.ignore()  # Details are paged; wheel events never shift the main window.

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (Qt.Key_PageDown, Qt.Key_PageUp):
            self.pageRequested.emit(1 if event.key() == Qt.Key_PageDown else -1)
        else:
            super().keyPressEvent(event)


class DetailPager(QWidget):
    """A fixed workspace: long details use explicit pages, never taller layouts."""

    closed = Signal()
    linkActivated = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.page = 0
        self._html = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        head = QHBoxLayout()
        self.title = SummaryLabel(role="body")
        self.close_button = QPushButton("Back")
        self.close_button.clicked.connect(self.closed)
        head.addWidget(self.title, 1)
        head.addWidget(self.close_button)
        layout.addLayout(head)
        self.browser = _PageBrowser()
        self.browser.setReadOnly(True)
        self.browser.setOpenLinks(False)
        self.browser.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.browser.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.browser.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.browser.setMinimumSize(0, 0)
        self.browser.anchorClicked.connect(lambda url: self.linkActivated.emit(url.toString()))
        self.browser.pageRequested.connect(self.advance)
        self.browser.verticalScrollBar().rangeChanged.connect(self._update_page)
        layout.addWidget(self.browser, 1)
        row = QHBoxLayout()
        self.previous = QPushButton("← Previous")
        self.next = QPushButton("Next →")
        self.page_label = QLabel()
        self.page_label.setAlignment(Qt.AlignCenter)
        self.previous.clicked.connect(lambda: self.advance(-1))
        self.next.clicked.connect(lambda: self.advance(1))
        row.addWidget(self.previous)
        row.addWidget(self.page_label, 1)
        row.addWidget(self.next)
        layout.addLayout(row)
        theme.on_theme_changed(self._restyle)

    def _restyle(self):
        self.browser.document().setDefaultStyleSheet(theme.rich_text_css())
        self._update_page()

    def set_content(self, title, text, *, reset=False):
        self.title.setText(title)
        if reset:
            self.page = 0
        if text != self._html:
            self._html = text
            self.browser.document().setDefaultStyleSheet(theme.rich_text_css())
            with QSignalBlocker(self.browser.verticalScrollBar()):
                self.browser.setHtml(text)
        self._update_page()
        QTimer.singleShot(0, self._update_page)

    def _stride(self):
        # One line of overlap keeps a line crossing a page boundary readable.
        line = self.browser.fontMetrics().lineSpacing()
        return max(line, self.browser.viewport().height() - line)

    def _update_page(self, *_):
        maximum = self.browser.verticalScrollBar().maximum()
        pages = max(1, math.ceil(maximum / self._stride()) + 1)
        self.page = min(self.page, pages - 1)
        self.browser.verticalScrollBar().setValue(min(maximum, self.page * self._stride()))
        self.previous.setEnabled(self.page > 0)
        self.next.setEnabled(self.page < pages - 1)
        self.page_label.setText(f"{self.page + 1} / {pages}")

    def advance(self, direction):
        self.page = max(0, self.page + direction)
        self._update_page()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        QTimer.singleShot(0, self._update_page)
