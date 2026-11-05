"""Native glanceable forecasts: an animated balance and inspectable searched paths."""

from __future__ import annotations

import math

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QFrame,
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
        self.setMinimumHeight(100)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(100)
        self.value: float | None = None
        self.angle = 0.0
        self.motion_enabled = True
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(650)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)
        self.animation.valueChanged.connect(self._animate)
        theme.on_theme_changed(self._theme_changed)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(180, 100)

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
        width = self.width()
        cx, cy = width / 2, 30.0
        arm = min(105.0, width * 0.32)
        radians = math.radians(self.angle)
        tone = (
            "muted" if self.value is None or 45 <= self.value < 55 else "good" if self.value >= 55 else "warn"
        )
        painter.setPen(QPen(theme.qcolor("border"), 2))
        painter.setBrush(theme.qcolor("surface_2"))
        support = QPainterPath(QPointF(cx, cy))
        support.lineTo(cx - 16, 78)
        support.lineTo(cx + 16, 78)
        support.closeSubpath()
        painter.drawPath(support)
        ends = [
            QPointF(cx + side * arm * math.cos(radians), cy + side * arm * math.sin(radians))
            for side in (-1, 1)
        ]
        painter.setPen(QPen(theme.qcolor(tone), 3, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(*ends)
        for point in ends:
            x, y = point.x(), point.y()
            painter.setPen(QPen(theme.qcolor("muted"), 1))
            painter.drawLine(QPointF(x, y), QPointF(x - 17, y + 21))
            painter.drawLine(QPointF(x, y), QPointF(x + 17, y + 21))
            pan = QPainterPath(QPointF(x - 17, y + 21))
            pan.quadTo(x, y + 38, x + 17, y + 21)
            pan.closeSubpath()
            painter.setBrush(theme.qcolor("surface_2"))
            painter.drawPath(pan)
        painter.setBrush(theme.qcolor("surface"))
        painter.setPen(QPen(theme.qcolor("muted"), 2))
        painter.drawEllipse(QPointF(cx, cy), 4, 4)
        painter.setFont(theme.ui_font("caption"))
        painter.setPen(theme.qcolor("muted"))
        painter.drawText(QRectF(0, 82, width / 2, 17), Qt.AlignLeft, "Opponent ahead")
        painter.drawText(QRectF(width / 2, 82, width / 2, 17), Qt.AlignRight, "You ahead")


class RouteRail(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.steps: list[dict] = []
        self.leading = False
        self.complete = True
        self.setFixedHeight(56)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(180, 56)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setFont(theme.ui_font("caption"))
        count = min(3, len(self.steps))
        if not count:
            return
        width = self.width() / count
        points = [QPointF(width * (i + 0.5), 9) for i in range(count)]
        tone = "good" if self.leading else "border"
        if self.leading and theme.tokens().name != theme.THEME_HIGH_CONTRAST:
            for stroke, alpha in ((12, 15), (8, 30), (4, 50)):
                glow = theme.qcolor(tone)
                glow.setAlpha(alpha)
                painter.setPen(QPen(glow, stroke, Qt.SolidLine, Qt.RoundCap))
                for a, b in zip(points, points[1:], strict=False):
                    painter.drawLine(a, b)
        for i, (a, b) in enumerate(zip(points, points[1:], strict=False)):
            style = Qt.DashLine if i >= 1 or not self.complete else Qt.SolidLine
            painter.setPen(QPen(theme.qcolor(tone), 2, style))
            painter.drawLine(a, b)
        for i, point in enumerate(points):
            painter.setBrush(theme.qcolor(tone))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(point, 3.5, 3.5)
            painter.setPen(theme.qcolor("muted"))
            painter.drawText(
                QRectF(i * width, 21, width, 17), Qt.AlignCenter, f"T{self.steps[i].get('turn', '?')}"
            )
            short = painter.fontMetrics().elidedText(step_text(self.steps[i]), Qt.ElideRight, int(width - 6))
            painter.drawText(QRectF(i * width, 37, width, 17), Qt.AlignCenter, short)


class PathCard(QFrame):
    def __init__(self, index: int, inspect, parent: QWidget | None = None):
        super().__init__(parent)
        self.setProperty("card", True)
        self.setObjectName("strategyPath")
        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(4)
        self.button = QPushButton()
        self.button.setProperty("variant", "flat")
        self.button.setCheckable(True)
        self.button.clicked.connect(lambda: inspect(index))
        layout.addWidget(self.button, alignment=Qt.AlignLeft)
        self.summary = text_label(role="body")
        layout.addWidget(self.summary)
        self.rail = RouteRail()
        layout.addWidget(self.rail)
        self.outcome = text_label()
        layout.addWidget(self.outcome)
        self.detail = text_label()
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.detail)
        self.hide()


class StrategyView(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        layout.setAlignment(Qt.AlignTop)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.plan: dict = {}
        self.state: dict = {}
        self.lines: list[dict] = []
        self.selected = -1
        self.current = False
        self.leading = False
        outlook = QFrame()
        outlook.setProperty("card", True)
        box = QVBoxLayout(outlook)
        box.setSizeConstraint(QLayout.SetMinimumSize)
        box.setContentsMargins(10, 7, 10, 7)
        box.setSpacing(3)
        self.title = text_label("Outlook", "caption")
        self.verdict = text_label("Waiting for a position", "title")
        self.scale = OutlookScale()
        self.reason = text_label()
        self.safety = text_label()
        for widget in (self.title, self.verdict, self.scale, self.reason, self.safety):
            box.addWidget(widget)
        box.addWidget(text_label("Board assessment · not a win probability"))
        layout.addWidget(outlook)
        head = QHBoxLayout()
        head.addWidget(text_label("Paths from here", "emphasis"))
        self.freshness = text_label()
        head.addWidget(self.freshness)
        layout.addLayout(head)
        self.cards = [PathCard(i, self.inspect) for i in range(3)]
        for card in self.cards:
            layout.addWidget(card)
        self.empty = text_label("Searched paths will appear when available.")
        layout.addWidget(self.empty)
        self.assumptions = Disclosure("Forecast assumptions")
        self.assumptions.content.addWidget(
            text_label(
                "Solid: searched continuation. Dashed: approximate continuation. "
                "The third turn is greedy; future draws and unknown opponent spells are not modeled. "
                "A green path is the leading supported line, not a promise of winning. "
                "Inspecting a path never changes autoplay."
            )
        )
        layout.addWidget(self.assumptions)
        theme.on_theme_changed(self.refresh)

    def inspect(self, index: int) -> None:
        self.selected = index if self.selected != index else -1
        self.refresh()

    def set_data(self, plan: dict, state: dict) -> None:
        previous = self.lines[self.selected] if 0 <= self.selected < len(self.lines) else None
        self.plan, self.state = plan, state
        self.lines = visible_lines(plan, state)
        self.selected = self.lines.index(previous) if previous in self.lines else -1
        self.refresh()

    def refresh(self) -> None:
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
        outlook = self.plan.get("outlook") or {}
        value = number(outlook.get("pct")) if self.current else None
        self.scale.set_value(value)
        verdict = str(outlook.get("label") or "Outlook unavailable").capitalize()
        self.verdict.setText(
            verdict if self.current else "Reassessing" if self.plan else "Waiting for a position"
        )
        tone = str(outlook.get("tone") or "muted") if self.current else "muted"
        self.verdict.setProperty("tone", tone if tone in theme.TONES else "muted")
        theme.repolish(self.verdict)
        self.reason.setText(
            " · ".join(map(str, (outlook.get("why") or [])[:2]))
            if self.current
            else "Waiting for an assessment of this board."
        )
        dead_in = number(facts.get("dead_in"))
        safety = (
            f"Danger: projected loss in {dead_in} attack(s)"
            if dead_in
            else "No immediate modeled lethal"
            if self.lines and self.lines[0].get("outcome") == "alive"
            else "Survival not established"
        )
        pressure = (
            "Lethal available on this board"
            if facts.get("lethal_now")
            else "Winning line conditional"
            if self.lines and self.lines[0].get("outcome") == "win"
            else "Win not established"
        )
        if search.get("truncated") or (
            self.lines and (self.lines[0].get("unsupported") or self.lines[0].get("outcome") == "unknown")
        ):
            safety, pressure = "Survival uncertain · limited model", "Win not established"
        self.safety.setText(f"{safety}\n{pressure}" if self.current else "")
        self.freshness.setText(
            "Limited search"
            if self.current and search.get("truncated")
            else "Best found · bounded"
            if self.current and search.get("bounded")
            else "Current board"
            if self.current
            else "Needs update"
        )
        self.empty.setVisible(not self.lines)
        for i, card in enumerate(self.cards):
            card.setVisible(i < len(self.lines))
            if i >= len(self.lines):
                continue
            line = self.lines[i]
            leading = i == 0 and self.leading
            card.setProperty("leading", leading)
            card.setProperty("selected", i == self.selected)
            theme.repolish(card)
            heading = (
                "Leading line"
                if leading
                else "Outdated path"
                if not self.current
                else "Uncertain path"
                if line.get("outcome") == "unknown"
                else f"Alternative {i}"
                if i
                else "Limited forecast"
            )
            card.button.setText(heading + (" ▾" if i == self.selected else " ▸"))
            card.button.setChecked(i == self.selected)
            steps = [s for s in line.get("steps") or [] if isinstance(s, dict)][:3]
            summary = step_text(steps[0]).split(" · ")[0] if steps else "Resolve the current attack"
            card.summary.setText(summary)
            card.button.setAccessibleName(f"Inspect {heading}: {summary}")
            card.rail.steps, card.rail.leading = steps, leading
            card.rail.complete = line.get("complete", True)
            card.rail.update()
            card.rail.setVisible(i == self.selected or leading)
            card.rail.setToolTip("\n".join(step_text(step) for step in steps))
            card.outcome.setText(
                line_outcome(line) if self.current else "Previous board · not current advice"
            )
            details = []
            for step in steps:
                life = number(step.get("life_after"))
                opp = number(step.get("opp_life_after"))
                details.append(f"T{step.get('turn', '?')}: {step_text(step)}")
                if life is not None or opp is not None:
                    details.append(
                        f"  Projected life: you {life if life is not None else '?'} · opponent {opp if opp is not None else '?'}"
                    )
            if line.get("unsupported"):
                details.append("Not modeled: " + "; ".join(map(str, line["unsupported"])))
            card.detail.setText("\n".join(details))
            card.detail.setVisible(i == self.selected)
