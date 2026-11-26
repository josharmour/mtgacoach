"""Clickable Arena standings (Mock B: Live Event Banner + Compact Ladders) on the coach sidebar."""

from __future__ import annotations

import os

from PySide6.QtCore import QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QStyle, QStyleOptionButton, QVBoxLayout, QWidget

from arenamcp.android_link import MIRROR_PATH, game_device
from arenamcp.ladder import (
    LADDERS,
    RANK_HISTORY_LIMIT,
    RankLog,
    normalize_event_key,
    pip_progress,
    rank_short,
    rank_text,
    ranked_ladder,
)
from arenamcp.match_history import MatchHistory
from arenamcp.watcher import DEFAULT_LOG_PATH, _normalize_log_path

from . import theme
from .companion_widgets import SummaryButton

_RESULT_MARKS = {"win": "W", "loss": "L", "draw": "D"}
_RESULT_TONES = {"W": "good", "L": "bad", "D": "warn"}


def _paint_wl_pills(
    painter: QPainter,
    rect: QRect,
    marks: list[str],
    *,
    align_right: bool = False,
    empty_text: str = "",
) -> None:
    if rect.width() <= 4 or rect.height() <= 4:
        return
    if not marks:
        if empty_text:
            painter.save()
            painter.setFont(theme.ui_font("caption"))
            painter.setPen(theme.qcolor("muted"))
            fm = painter.fontMetrics()
            text = fm.elidedText(empty_text, Qt.ElideRight, rect.width())
            flags = (Qt.AlignRight if align_right else Qt.AlignLeft) | Qt.AlignVCenter
            painter.drawText(rect, flags, text)
            painter.restore()
        return

    painter.save()
    font = theme.ui_font("caption")
    font.setBold(True)
    painter.setFont(font)
    fm = QFontMetrics(font)
    size = max(14, min(rect.height(), fm.height() + 2))
    gap = 3
    max_count = max(1, (rect.width() + gap) // (size + gap))
    shown = marks[:max_count]
    total_w = len(shown) * size + max(0, len(shown) - 1) * gap
    x = rect.right() - total_w + 1 if align_right else rect.left()
    y = rect.top() + (rect.height() - size) // 2

    for mark in shown:
        tone = _RESULT_TONES.get(mark, "muted")
        box = QRectF(x, y, size, size)
        fill = theme.qcolor(tone)
        painter.save()
        painter.setOpacity(0.16)
        painter.setPen(Qt.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(box, 4.0, 4.0)
        painter.restore()

        painter.save()
        painter.setOpacity(0.45)
        painter.setPen(QPen(fill, 1.0))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), 4.0, 4.0)
        painter.restore()

        painter.setPen(theme.qcolor(tone))
        painter.drawText(box, Qt.AlignCenter, mark)
        x += size + gap
    painter.restore()


def _paint_pip_dots(painter: QPainter, right_x: int, center_y: int, filled: int, total: int) -> int:
    """Paint rank pip dots right-aligned at *right_x*, returning the leftmost x used."""
    if total <= 0:
        return right_x
    diameter = 5
    gap = 3
    total_w = total * diameter + max(0, total - 1) * gap
    start_x = right_x - total_w
    y = center_y - diameter // 2
    painter.save()
    painter.setPen(Qt.NoPen)
    for i in range(total):
        painter.setBrush(theme.qcolor("accent" if i < filled else "border"))
        painter.drawEllipse(start_x + i * (diameter + gap), y, diameter, diameter)
    painter.restore()
    return start_x


class _EventBannerButton(SummaryButton):
    """Live event course banner shown above the season ladders when in an event."""

    def __init__(self, parent=None):
        super().__init__("", parent)
        self.setProperty("eventBanner", True)
        self.setCursor(Qt.PointingHandCursor)
        self.event_data: dict | None = None
        self.marks: list[str] = []
        self._measure()

    def _measure(self) -> None:
        fm = self.fontMetrics()
        self.setFixedHeight(2 * fm.lineSpacing() + 18)

    def paintEvent(self, event):  # noqa: N802
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        self.style().drawControl(QStyle.CE_PushButtonBevel, option, painter, self)

        data = self.event_data
        if not data:
            return
        rect = self.rect().adjusted(10, 6, -10, -6)
        half_h = rect.height() // 2
        top_rect = QRect(rect.left(), rect.top(), rect.width(), half_h)
        bot_rect = QRect(rect.left(), rect.top() + half_h, rect.width(), rect.height() - half_h)

        # Top row: Event name + Ranked/Unranked badge on left; W-L record on right.
        title_font = theme.ui_font("body")
        title_font.setBold(True)
        badge_font = theme.ui_font("caption")
        record = f"{data['wins']}W – {data['losses']}L"
        painter.setFont(title_font)
        rec_fm = painter.fontMetrics()
        rec_w = rec_fm.horizontalAdvance(record) + 6

        tag = "Ranked" if data.get("ranked") else "Unranked"
        painter.setFont(badge_font)
        tag_fm = painter.fontMetrics()
        tag_w = tag_fm.horizontalAdvance(tag) + 12

        title_avail = max(20, top_rect.width() - rec_w - tag_w - 8)
        painter.setFont(title_font)
        painter.setPen(theme.qcolor("text"))
        title = painter.fontMetrics().elidedText(
            str(data.get("display_name") or ""), Qt.ElideRight, title_avail
        )
        title_w = painter.fontMetrics().horizontalAdvance(title)
        painter.drawText(
            QRect(top_rect.left(), top_rect.top(), title_avail, top_rect.height()),
            Qt.AlignLeft | Qt.AlignVCenter,
            title,
        )

        # Tag pill
        tag_x = top_rect.left() + title_w + 6
        tag_h = max(14, tag_fm.height() + 2)
        tag_box = QRectF(tag_x, top_rect.top() + (top_rect.height() - tag_h) / 2, tag_w, tag_h)
        painter.save()
        painter.setPen(QPen(theme.qcolor("border"), 1.0))
        painter.setBrush(theme.qcolor("surface"))
        painter.drawRoundedRect(tag_box, 4.0, 4.0)
        painter.setFont(badge_font)
        painter.setPen(theme.qcolor("muted"))
        painter.drawText(tag_box, Qt.AlignCenter, tag)
        painter.restore()

        # Record on right
        painter.setFont(title_font)
        painter.setPen(theme.qcolor("text"))
        painter.drawText(top_rect, Qt.AlignRight | Qt.AlignVCenter, record)

        # Bottom row: stage/rule on left, W/L pills on right
        rule = str(data.get("rule") or "")
        pills_w = min(bot_rect.width() // 2 + 24, max(60, len(self.marks) * 18))
        rule_w = max(20, bot_rect.width() - pills_w - 6)
        painter.setFont(badge_font)
        painter.setPen(theme.qcolor("muted"))
        rule_text = painter.fontMetrics().elidedText(rule, Qt.ElideRight, rule_w)
        painter.drawText(
            QRect(bot_rect.left(), bot_rect.top(), rule_w, bot_rect.height()),
            Qt.AlignLeft | Qt.AlignVCenter,
            rule_text,
        )
        pills_rect = QRect(bot_rect.right() - pills_w + 1, bot_rect.top(), pills_w, bot_rect.height())
        _paint_wl_pills(painter, pills_rect, self.marks, align_right=True)


class _RankCardButton(SummaryButton):
    """Compact Mock B ladder card: ladder + short rank + pip dots on row 1, W/L pills on row 2."""

    def __init__(self, ladder: str, parent=None):
        super().__init__(f"{ladder}\nUnavailable", parent)
        self.ladder = ladder
        self.short_rank = "Unavailable"
        self.pips: tuple[int, int] | None = None
        self.marks: list[str] = []
        self.setProperty("rankCard", True)
        self.setCursor(Qt.PointingHandCursor)
        self._measure()

    def _measure(self) -> None:
        fm = self.fontMetrics()
        self.setFixedHeight(2 * fm.lineSpacing() + 18)

    def paintEvent(self, event):  # noqa: N802
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        self.style().drawControl(QStyle.CE_PushButtonBevel, option, painter, self)

        rect = self.rect().adjusted(10, 6, -10, -6)
        half_h = rect.height() // 2
        top_rect = QRect(rect.left(), rect.top(), rect.width(), half_h)
        bot_rect = QRect(rect.left(), rect.top() + half_h, rect.width(), rect.height() - half_h)

        # Row 1: muted ladder name (left), bold rank + optional pip dots (right)
        right_edge = top_rect.right() + 1
        if self.pips is not None:
            filled, total = self.pips
            pips_left = _paint_pip_dots(painter, right_edge, top_rect.center().y(), filled, total)
            rank_right = pips_left - 6
        else:
            rank_right = right_edge

        rank_font = theme.ui_font("body")
        rank_font.setBold(True)
        painter.setFont(rank_font)
        rank_fm = painter.fontMetrics()
        rank_w = min(rank_fm.horizontalAdvance(self.short_rank) + 2, max(20, top_rect.width() * 2 // 3))
        rank_left = max(top_rect.left() + 24, rank_right - rank_w)
        rank_str = rank_fm.elidedText(self.short_rank, Qt.ElideRight, max(10, rank_right - rank_left))
        painter.setPen(theme.qcolor("text"))
        painter.drawText(
            QRect(rank_left, top_rect.top(), max(10, rank_right - rank_left), top_rect.height()),
            Qt.AlignRight | Qt.AlignVCenter,
            rank_str,
        )

        label_font = theme.ui_font("caption")
        painter.setFont(label_font)
        painter.setPen(theme.qcolor("muted"))
        ladder_avail = max(10, rank_left - top_rect.left() - 6)
        ladder_str = painter.fontMetrics().elidedText(self.ladder, Qt.ElideRight, ladder_avail)
        painter.drawText(
            QRect(top_rect.left(), top_rect.top(), ladder_avail, top_rect.height()),
            Qt.AlignLeft | Qt.AlignVCenter,
            ladder_str,
        )

        # Row 2: color-coded W/L pills (or "No recent matches")
        _paint_wl_pills(painter, bot_rect, self.marks, align_right=False, empty_text="No recent matches")


class _UnrankedRowButton(SummaryButton):
    """Compact footer row showing Unranked label on the left and W/L pills on the right."""

    def __init__(self, parent=None):
        super().__init__("Unranked · No recent matches", parent)
        self.marks: list[str] = []
        self.setProperty("unrankedRow", True)
        self.setCursor(Qt.PointingHandCursor)
        self._measure()

    def _measure(self) -> None:
        fm = self.fontMetrics()
        self.setFixedHeight(fm.lineSpacing() + 10)

    def paintEvent(self, event):  # noqa: N802
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        self.style().drawControl(QStyle.CE_PushButtonBevel, option, painter, self)

        rect = self.rect().adjusted(6, 2, -6, -2)
        cap_font = theme.ui_font("caption")
        painter.setFont(cap_font)
        painter.setPen(theme.qcolor("muted"))
        left_label = "Unranked"
        left_w = painter.fontMetrics().horizontalAdvance(left_label) + 8
        painter.drawText(
            QRect(rect.left(), rect.top(), left_w, rect.height()),
            Qt.AlignLeft | Qt.AlignVCenter,
            left_label,
        )
        pills_rect = QRect(rect.left() + left_w, rect.top(), max(10, rect.width() - left_w), rect.height())
        _paint_wl_pills(painter, pills_rect, self.marks, align_right=True, empty_text="No recent matches")


class RankStrip(QWidget):
    history_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(2, 0, 2, 0)
        header.setSpacing(6)
        self.account_label = QLabel("Arena account · Not detected yet")
        self.account_label.setObjectName("arenaAccountLabel")
        self.account_label.setProperty("role", "small")
        self.account_label.setTextFormat(Qt.PlainText)
        self.account_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header.addWidget(self.account_label, 1)

        self.direction_label = QLabel("Newest → oldest")
        self.direction_label.setProperty("role", "small")
        self.direction_label.setTextFormat(Qt.PlainText)
        self.direction_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header.addWidget(self.direction_label)
        root.addLayout(header)

        self._reader = RankLog()
        self.buttons: dict[str, _RankCardButton] = {}
        self.recent: dict[str, list] = {ladder: [] for ladder in LADDERS}
        self.recent_unranked: list = []
        self.recent_event: list = []
        self.active_event: dict | None = None
        self._history_stamp = None

        self.event_banner = _EventBannerButton(self)
        self.event_banner.clicked.connect(lambda checked=False: self.history_requested.emit("Event"))
        self.event_banner.hide()
        root.addWidget(self.event_banner)

        layout = QHBoxLayout()
        layout.setSpacing(6)
        root.addLayout(layout)
        for ladder in LADDERS:
            button = _RankCardButton(ladder, self)
            button.clicked.connect(lambda checked=False, name=ladder: self.history_requested.emit(name))
            layout.addWidget(button, 1)
            self.buttons[ladder] = button

        self.unranked_button = _UnrankedRowButton(self)
        self.unranked_button.clicked.connect(lambda checked=False: self.history_requested.emit("Unranked"))
        root.addWidget(self.unranked_button)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.refresh)
        theme.on_theme_changed(self._on_theme_changed)

    def _on_theme_changed(self) -> None:
        try:
            self.event_banner._measure()
            for button in self.buttons.values():
                button._measure()
            self.unranked_button._measure()
            self._render()
        except RuntimeError:
            pass

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._timer.start(0)

    def hideEvent(self, event):  # noqa: N802
        super().hideEvent(event)
        self._timer.stop()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._render()

    def _render(self):
        name = " ".join(self._reader.account_name.split())
        text = f"Arena account · {name}" if name else "Arena account · Not detected yet"
        dir_w = max(0, self.direction_label.sizeHint().width() + 8)
        avail_acct = max(1, self.width() - dir_w)
        self.account_label.setText(
            self.account_label.fontMetrics().elidedText(text, Qt.ElideRight, avail_acct)
        )
        self.account_label.setToolTip(
            text + "\nDetected from the active Arena log. Recent matches are stored on this device."
        )

        # Event banner
        ev = self.active_event
        self.event_banner.event_data = ev
        ev_marks = [_RESULT_MARKS.get(r.result, "?") for r in self.recent_event]
        self.event_banner.marks = ev_marks
        if ev is not None:
            ev_label = f"{ev['display_name']} ({ev['wins']}W-{ev['losses']}L)"
            self.event_banner.setText(ev_label)
            self.event_banner.setToolTip(
                f"{ev['display_name']} · {ev['wins']}W – {ev['losses']}L ({ev['rule']}). Click for match history."
            )
            self.event_banner.show()
        else:
            self.event_banner.hide()
        self.event_banner.update()

        # Ladder cards (Constructed / Limited)
        for ladder, button in self.buttons.items():
            rank = rank_text(self._reader.snapshot, ladder)
            button.short_rank = rank_short(self._reader.snapshot, ladder)
            button.pips = pip_progress(self._reader.snapshot, ladder)
            recent = self.recent[ladder]
            marks = [_RESULT_MARKS.get(r.result, "?") for r in recent]
            button.marks = marks

            # Maintain accessible plain-text summary (also keeps test_performance_tab contract).
            metrics = button.fontMetrics()
            available = max(1, button.width() - 28)
            results = " ".join(marks)
            if metrics.horizontalAdvance(results) > available:
                results = "".join(marks)
            history = f"Newest → oldest\n{results}" if results else "No recent matches"
            if results and metrics.horizontalAdvance(results) > available:
                split = (len(marks) + 1) // 2
                first, last = results[:split], results[split:]
                newest = "Newest" if metrics.horizontalAdvance(f"Newest {first}") <= available else "New"
                oldest = "Oldest" if metrics.horizontalAdvance(f"{last} Oldest") <= available else "Old"
                history = f"{newest} {first}\n{last} {oldest}"
            button.setText(f"{ladder}\n{rank}\n{history}")
            button.setToolTip(
                f"{ladder}: {rank}. Show up to {RANK_HISTORY_LIMIT} recent ranked {ladder} matches, newest first."
            )
            button.update()

        # Unranked row
        unranked_marks = [_RESULT_MARKS.get(r.result, "?") for r in self.recent_unranked]
        self.unranked_button.marks = unranked_marks
        unranked_metrics = self.unranked_button.fontMetrics()
        unranked_avail = max(1, self.unranked_button.width() - 28)
        if unranked_marks:
            spaced = " ".join(unranked_marks)
            compact = "".join(unranked_marks)
            candidates = (
                f"Unranked · Newest → oldest · {spaced}",
                f"Unranked · {spaced}",
                f"Unranked · {compact}",
            )
            unranked_text = next(
                (c for c in candidates if unranked_metrics.horizontalAdvance(c) <= unranked_avail),
                candidates[-1],
            )
        else:
            unranked_text = "Unranked · No recent matches"
        self.unranked_button.setText(unranked_text)
        self.unranked_button.setToolTip(
            f"Show up to {RANK_HISTORY_LIMIT} recent unranked matches (Brawl Historic, Play, etc.), newest first."
        )
        self.unranked_button.update()

    def refresh(self):
        path = (
            MIRROR_PATH
            if game_device() == "android"
            else _normalize_log_path(os.environ.get("MTGA_LOG_PATH") or DEFAULT_LOG_PATH)
        )
        more = self._reader.read(path)
        if not more:
            from arenamcp import match_history

            try:
                stat = match_history.HISTORY_FILE.stat()
                stamp = (str(match_history.HISTORY_FILE), stat.st_mtime_ns, stat.st_size)
            except OSError:
                stamp = (str(match_history.HISTORY_FILE), None)
            extra = self._reader.observed_ranked_events
            records = getattr(self, "_cached_records", None)
            if stamp != self._history_stamp or records is None:
                self._history_stamp = stamp
                history = MatchHistory()
                records = list(reversed(history.get_recent(history.total_games)))
                self._cached_records = records
            self.recent = {
                name: [r for r in records if ranked_ladder(r.format_name, extra_ranked=extra) == name][
                    :RANK_HISTORY_LIMIT
                ]
                for name in LADDERS
            }
            self.recent_unranked = [
                r for r in records if ranked_ladder(r.format_name, extra_ranked=extra) is None
            ][:RANK_HISTORY_LIMIT]
            self.active_event = self._reader.active_event(records)
            if self.active_event is not None:
                ev_key = normalize_event_key(self.active_event["event_name"])
                self.recent_event = [r for r in records if normalize_event_key(r.format_name) == ev_key][
                    :RANK_HISTORY_LIMIT
                ]
            else:
                self.recent_event = []
            self._render()
        self._timer.start(10 if more else 3000)
