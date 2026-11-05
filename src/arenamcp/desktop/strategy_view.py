"""Native glanceable forecasts: an animated balance and inspectable searched paths."""

from __future__ import annotations

import html
import math

from PySide6.QtCore import QEasingCurve, QPointF, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .companion_widgets import SummaryButton, SummaryLabel
from .strategy_model import is_current, line_outcome, number, step_text, visible_lines


def text_label(text: str = "", role: str = "small") -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    label.setProperty("role", role)
    label.setMinimumWidth(0)
    label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    policy = label.sizePolicy()
    policy.setHeightForWidth(True)
    label.setSizePolicy(policy)
    return label


class Disclosure(QWidget):
    """Keeps secondary information available without competing with play."""

    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.title = title
        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        layout.setAlignment(Qt.AlignTop)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.toggle = QPushButton("▸ " + title.replace("&", "&&"))
        self.toggle.setProperty("variant", "flat")
        self.toggle.setCheckable(True)
        self.toggle.setAccessibleName(title)
        layout.addWidget(self.toggle, alignment=Qt.AlignLeft)
        self.body = QWidget()
        self.content = QVBoxLayout(self.body)
        self.content.setContentsMargins(0, 0, 0, 0)
        self.content.setSpacing(5)
        self.body.hide()
        layout.addWidget(self.body)
        self.toggle.toggled.connect(self.set_expanded)

    def set_expanded(self, expanded: bool) -> None:
        self.toggle.setChecked(expanded)
        self.toggle.setText(("▾ " if expanded else "▸ ") + self.title.replace("&", "&&"))
        self.body.setVisible(expanded)


class OutlookScale(QWidget):
    """A bounded, one-shot transition between heuristic assessments, never odds."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumHeight(36)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedSize(84, 36)
        self.value: float | None = None
        self.angle = 0.0
        self.motion_enabled = True
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(650)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)
        self.animation.valueChanged.connect(self._animate)
        theme.on_theme_changed(self._theme_changed)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(84, 36)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def _animate(self, value) -> None:
        self.angle = float(value)
        self.update()

    def _theme_changed(self) -> None:
        self.set_value(self.value, animate=False)

    def set_motion(self, enabled: bool) -> None:
        self.motion_enabled = enabled
        self.set_value(self.value, animate=False)

    def set_value(self, value: int | float | None, *, animate: bool = True) -> None:
        previous = self.value
        self.value = max(0, min(100, value)) if value is not None else None
        target = (self.value - 50) * 0.38 if self.value is not None else 0.0
        if (
            animate
            and self.motion_enabled
            and self.value is not None
            and self.animation.state() == QVariantAnimation.Running
            and self.animation.endValue() == target
        ):
            return
        self.animation.stop()
        system_motion = self.style().styleHint(QStyle.SH_Widget_Animate, None, self)
        if (
            animate
            and self.motion_enabled
            and system_motion
            and self.isVisible()
            and previous is not None
            and self.value is not None
            and theme.tokens().name != theme.THEME_HIGH_CONTRAST
            and self.angle != target
        ):
            self.animation.setStartValue(self.angle)
            self.animation.setEndValue(target)
            self.animation.start()
        else:
            self.angle = target
            self.update()
        self.setAccessibleName(
            "Outlook unavailable"
            if self.value is None
            else "Board outlook: "
            + ("you ahead" if self.value >= 55 else "opponent ahead" if self.value < 45 else "even")
        )

    def hideEvent(self, event) -> None:  # noqa: N802
        self.set_value(self.value, animate=False)
        super().hideEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        cx, cy, arm = self.width() / 2, 9.0, 28.0
        radians = math.radians(self.angle)
        tone = (
            "muted" if self.value is None or 45 <= self.value < 55 else "good" if self.value >= 55 else "warn"
        )
        painter.setPen(QPen(theme.qcolor("border"), 1))
        painter.setBrush(theme.qcolor("surface_2"))
        support = QPainterPath(QPointF(cx, cy))
        support.lineTo(cx - 6, 33)
        support.lineTo(cx + 6, 33)
        support.closeSubpath()
        painter.drawPath(support)
        ends = [
            QPointF(cx + side * arm * math.cos(radians), cy + side * arm * math.sin(radians))
            for side in (-1, 1)
        ]
        painter.setPen(QPen(theme.qcolor(tone), 2, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(*ends)
        for point in ends:
            x, y = point.x(), point.y()
            pan = QPainterPath(QPointF(x, y))
            pan.lineTo(x - 6, y + 9)
            pan.quadTo(x, y + 16, x + 6, y + 9)
            pan.closeSubpath()
            painter.setPen(QPen(theme.qcolor("muted"), 1))
            painter.drawPath(pan)
        painter.setBrush(theme.qcolor("surface"))
        painter.drawEllipse(QPointF(cx, cy), 2, 2)


class StrategyView(QWidget):
    """One compact scale and one discoverable path; detail replaces the workspace."""

    detailRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.plan, self.state, self.lines = {}, {}, []
        self.current = self.leading = False
        self.selected = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        row = QHBoxLayout()
        text = QVBoxLayout()
        text.setSpacing(0)
        self.verdict = SummaryLabel("Outlook · Waiting", role="body")
        self.reason = SummaryLabel("Board assessment · not win odds")
        text.addWidget(self.verdict)
        text.addWidget(self.reason)
        self.scale = OutlookScale()
        row.addLayout(text, 1)
        row.addWidget(self.scale)
        layout.addLayout(row)
        self.path_button = SummaryButton("Paths · Waiting for a position  ›")
        self.path_button.setFixedHeight(36)
        self.path_button.clicked.connect(lambda: self.inspect(0))
        layout.addWidget(self.path_button)
        theme.on_theme_changed(self.refresh)

    def inspect(self, index):
        self.selected = max(0, min(index, len(self.lines) - 1))
        self.detailRequested.emit()

    def set_data(self, plan, state):
        self.plan, self.state = plan, state
        self.lines = visible_lines(plan, state)
        self.selected = max(0, min(self.selected, len(self.lines) - 1))
        self.refresh()

    def refresh(self):
        facts = self.plan.get("facts") or {}
        search = facts.get("search") or {}
        self.current = is_current(self.plan, self.state)
        self.leading = bool(
            self.current
            and self.lines
            and not search.get("truncated")
            and not self.lines[0].get("unsupported")
            and self.lines[0].get("outcome") in {"win", "alive", "dead"}
        )
        finished = bool(self.state.get("game_over") or self.state.get("result"))
        in_game = bool(self.state.get("match_id") and self.state.get("turn"))
        outlook = self.plan.get("outlook") or {}
        self.scale.set_value(number(outlook.get("pct")) if self.current else None)
        verdict = (
            str(outlook.get("label") or "Unavailable").capitalize()
            if self.current
            else "Match complete"
            if finished
            else "Reassessing"
            if in_game
            else "Waiting for a game"
        )
        self.verdict.setText("Outlook · " + verdict)
        self.reason.setText("Board assessment · not win odds")
        why = " · ".join(map(str, outlook.get("why") or [])) if self.current else "No current assessment."
        self.verdict.setToolTip(why)
        self.scale.setToolTip("Opponent ahead ← balance → You ahead. " + why)
        tone = str(outlook.get("tone") or "muted") if self.current else "muted"
        self.verdict.setProperty("tone", tone if tone in theme.TONES else "muted")
        theme.repolish(self.verdict)
        if self.current and self.lines:
            first = self.lines[0]
            steps = first.get("steps") or []
            action = step_text(steps[0]).split(" · ")[0] if steps else "Resolve the current attack"
            prefix = "Leading path" if self.leading else "Limited forecast"
            title = f"{prefix} · {action}  ›"
            tip = line_outcome(first) + ". Click to explore paths and assumptions."
        else:
            title = "Paths · No active plan  ›" if finished or not in_game else "Paths · Rechecking  ›"
            tip = "No current recommendation. Previous winning claims have been withdrawn."
        self.path_button.setText(title)
        self.path_button.setToolTip(tip)
        self.path_button.leading = self.leading
        self.path_button.update()

    def detail_html(self):
        if not self.current:
            return "No current recommendation. " + (
                "This match is complete."
                if self.state.get("game_over") or self.state.get("result")
                else "Waiting for an assessment of the current board."
            )
        facts = self.plan.get("facts") or {}
        search = facts.get("search") or {}
        why = " · ".join(map(str, (self.plan.get("outlook") or {}).get("why") or []))
        parts = [theme.block(theme.span(why, "muted"))] if why else []
        if self.lines:
            line = self.lines[self.selected]
            heading = "Leading path" if self.selected == 0 and self.leading else f"Path {self.selected + 1}"
            parts.append(theme.block(theme.span(heading, weight=600)))
            parts.append(theme.block(theme.span(line_outcome(line))))
            if search.get("truncated"):
                parts.append(
                    theme.block(theme.span("Search truncated · survival and wins uncertain", "warn"))
                )
            for step in line.get("steps") or []:
                text = f"T{step.get('turn', '?')}: {step_text(step)}"
                parts.append(theme.block(theme.span(text)))
                life, opp = number(step.get("life_after")), number(step.get("opp_life_after"))
                if life is not None or opp is not None:
                    parts.append(
                        theme.block(
                            theme.span(
                                f"Projected life: you {life if life is not None else '?'} · opponent {opp if opp is not None else '?'}",
                                "muted",
                            )
                        )
                    )
            if line.get("unsupported"):
                parts.append(
                    theme.block(
                        theme.span("Not modeled: " + "; ".join(map(str, line["unsupported"])), "warn")
                    )
                )
            links = [
                f'<a href="path:{i}">{html.escape("Leading path" if i == 0 and self.leading else f"Path {i + 1}")}</a>'
                for i in range(len(self.lines))
            ]
            parts.append(theme.block(" · ".join(links)))
        else:
            parts.append("No supported searched paths are available.")
        parts.append(
            theme.block(
                theme.span(
                    "Forecast assumptions: green means the leading supported candidate found, not a promise of winning. "
                    "Future draws and unknown opponent spells are not modeled. The third turn is approximate. "
                    "Inspecting paths never changes autoplay.",
                    "muted",
                )
            )
        )
        return "".join(parts)
