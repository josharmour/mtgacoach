from __future__ import annotations

from typing import Any

from PySide6.QtGui import QTextBlockUserData, QTextCursor
from PySide6.QtWidgets import QTextEdit

from . import theme

MAX_BLOCKS = 500

# role → (tone, size) from the theme's token set.
_ROLE_STYLE = {
    "user": ("you", "body"),
    "coach": ("convo", "body"),
    "system": ("muted", "caption"),
}


class ConversationTranscript(QTextEdit):
    """Scrolling chat transcript for Conversation Mode (newest at the bottom).

    Roles: "user" (typed/PTT questions), "coach" (conversation replies), and
    "system" (status notices such as fallback / error tags).
    """

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self._entries: list[tuple[str, str]] = []
        self._pending = False
        theme.on_theme_changed(self.restyle)

    def _entry_html(self, role: str, text: str) -> str:
        tone, size = _ROLE_STYLE.get(role, _ROLE_STYLE["system"])
        prefix = {"user": "You", "coach": "Coach"}.get(role, "")
        label = theme.span(f"{prefix}: ", tone, size=size, weight=700) if prefix else ""
        return theme.block(label + theme.span(text, tone, size=size), gap=4)

    def add_entry(self, role: str, text: str) -> None:
        """Append an entry and autoscroll to the newest line (bottom)."""
        if not text:
            return
        self._entries.append((role, text))
        if len(self._entries) > MAX_BLOCKS:
            del self._entries[: len(self._entries) - MAX_BLOCKS]

        doc = self.document()
        sb = self.verticalScrollBar()
        stick_to_bottom = sb.value() >= sb.maximum() - 10

        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not doc.isEmpty():
            cursor.insertBlock()
        cursor.insertHtml(self._entry_html(role, text))

        # Trim oldest entries from the top when exceeding 500 blocks
        while doc.blockCount() > MAX_BLOCKS:
            first_block = doc.firstBlock()
            next_block = first_block.next()
            del_cursor = QTextCursor(doc)
            del_cursor.setPosition(0)
            if next_block.isValid():
                del_cursor.setPosition(next_block.position(), QTextCursor.MoveMode.KeepAnchor)
            else:
                del_cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
            del_cursor.removeSelectedText()

        if stick_to_bottom:
            sb.setValue(sb.maximum())

    def set_pending(self, pending: bool) -> None:
        """Show/clear the thinking placeholder line."""
        self._pending = pending
        doc = self.document()
        for block in _iter_blocks(doc):
            if block.userData() is not None and getattr(block.userData(), "pending", False):
                cursor = QTextCursor(block)
                cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
                cursor.removeSelectedText()
                cursor.removeSelectedText()
                break
        if pending:
            cursor = QTextCursor(doc)
            cursor.movePosition(QTextCursor.MoveOperation.End)
            if not doc.isEmpty():
                cursor.insertBlock()
            cursor.insertHtml(theme.block(theme.span("…thinking", "muted", size="caption", italic=True)))
            cursor.block().setUserData(_PendingBlockData())
            sb = self.verticalScrollBar()
            sb.setValue(sb.maximum())

    def restyle(self) -> None:
        """Re-render every entry with the active theme's colours."""
        sb = self.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 10
        entries = list(self._entries)
        pending = self._pending
        self.clear()
        self._entries = []
        for role, text in entries:
            self.add_entry(role, text)
        if pending:
            self.set_pending(True)
        if at_bottom:
            sb.setValue(sb.maximum())


class _PendingBlockData(QTextBlockUserData):
    """Marks a document block as the transient 'thinking' placeholder."""

    pending = True


def _iter_blocks(doc: Any) -> list[Any]:
    blocks = []
    block = doc.begin()
    while block.isValid():
        blocks.append(block)
        block = block.next()
    return blocks
