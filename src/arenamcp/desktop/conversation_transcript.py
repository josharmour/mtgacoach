from __future__ import annotations

import html
from typing import Any

from PySide6.QtGui import QTextBlockUserData, QTextCursor
from PySide6.QtWidgets import QTextEdit

MAX_BLOCKS = 500


class ConversationTranscript(QTextEdit):
    """Scrolling chat transcript for Conversation Mode (newest at the bottom).

    Roles: "user" (typed/PTT questions), "coach" (conversation replies), and
    "system" (status notices such as fallback / error tags).
    """

    _ROLE_COLORS_DARK = {
        "user": "#a6e3a1",
        "coach": "#89b4fa",
        "system": "#9399b2",
    }

    _ROLE_COLORS_LIGHT = {
        "user": "#1b7e2c",
        "coach": "#1e66f5",
        "system": "#6c6f85",
    }

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)

    def _colors(self) -> dict[str, str]:
        from .theme import get_theme_tokens

        tokens = get_theme_tokens(self)
        return self._ROLE_COLORS_DARK if tokens["is_dark"] else self._ROLE_COLORS_LIGHT

    def _entry_html(self, role: str, text: str) -> str:
        color = self._colors().get(role, self._colors()["system"])
        prefix = {"user": "You", "coach": "Coach", "system": ""}.get(role, "")
        escaped = html.escape(text).replace("\n", "<br>")
        label = f"<b>{prefix}:</b> " if prefix else ""
        return (
            f"<div style='color:{color}; font-size:12px; margin-bottom:4px;'>{label}{escaped}</div>"
        )

    def add_entry(self, role: str, text: str) -> None:
        """Append an entry and autoscroll to the newest line (bottom)."""
        if not text:
            return
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
        doc = self.document()
        for block in _iter_blocks(doc):
            if block.userData() is not None and getattr(block.userData(), "pending", False):
                cursor = QTextCursor(block)
                cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
                cursor.removeSelectedText()
                cursor.removeSelectedText()
                break
        if pending:
            color = self._colors()["system"]
            cursor = QTextCursor(doc)
            cursor.movePosition(QTextCursor.MoveOperation.End)
            if not doc.isEmpty():
                cursor.insertBlock()
            cursor.insertHtml(
                f"<div style='color:{color}; font-style:italic; font-size:11px;'>…thinking</div>"
            )
            cursor.block().setUserData(_PendingBlockData())
            sb = self.verticalScrollBar()
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
