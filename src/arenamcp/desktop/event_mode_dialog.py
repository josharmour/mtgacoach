"""Desktop dialogs for Event / Draft auto-queue mode selection and re-entry."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from arenamcp.event_queue import (
    describe_event_rules,
    eligible_decks_for_event,
    event_can_build_deck,
    event_eligibility,
    event_needs_deck,
    format_entry_fee,
    near_decks_for_event,
)

_CATEGORY_GROUPS = (
    ("in_progress", "In Progress"),
    ("free", "Free Events"),
    ("limited", "Draft & Sealed"),
    ("constructed", "Constructed & Special Events"),
)

_OUTCOME_HEADINGS = {
    "won_main_prize": "Main prize won!",
    "completed": "Event run finished",
    "approval_required": "Paid entry confirmation required",
    "loss_limit": "Free event paused on loss limit",
}


def _wallet_summary(wallet: dict[str, Any]) -> str:
    if not wallet:
        return "Wallet balances unavailable"
    parts = [
        f"Gold: {int(wallet.get('gold') or 0):,}",
        f"Gems: {int(wallet.get('gems') or 0):,}",
    ]
    draft_tok = int(wallet.get("draft_tokens") or 0)
    sealed_tok = int(wallet.get("sealed_tokens") or 0)
    if draft_tok:
        parts.append(f"Draft Tokens: {draft_tok}")
    if sealed_tok:
        parts.append(f"Sealed Tokens: {sealed_tok}")
    return "  ·  ".join(parts)


def _event_group_key(event: dict[str, Any]) -> str:
    if event.get("in_progress"):
        return "in_progress"
    if event.get("is_free"):
        return "free"
    if event.get("category") in {"human_draft", "bot_draft", "sealed"}:
        return "limited"
    return "constructed"


class EventModeDialog(QDialog):
    """Picker showing live Arena events and asking the user what to play."""

    eventConfigured = Signal(dict)
    refreshRequested = Signal()
    buildDeckRequested = Signal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Draft / Event Auto-queue")
        self.setMinimumWidth(520)
        self.setMinimumHeight(460)
        self._events: list[dict[str, Any]] = []
        self._wallet: dict[str, Any] = {}
        self._decks: list[dict[str, Any]] = []
        self._on_home: bool | None = None
        self._displayed_event: dict[str, Any] | None = None
        self._preferred_event_name: str = ""
        self._preferred_built_deck_id: str = ""
        self._sticky_status_message: str = ""
        self._fee_buttons: list[tuple[QRadioButton, dict[str, Any]]] = []
        self._fee_group = QButtonGroup(self)

        root = QVBoxLayout(self)
        root.setSpacing(10)

        top_row = QHBoxLayout()
        self.wallet_label = QLabel("Loading Arena events…")
        self.wallet_label.setWordWrap(True)
        top_row.addWidget(self.wallet_label, 1)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._on_refresh_clicked)
        top_row.addWidget(self.refresh_btn)
        root.addLayout(top_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        root.addWidget(self.status_label)

        self.event_list = QListWidget()
        self.event_list.currentItemChanged.connect(self._on_selection_changed)
        root.addWidget(self.event_list, 1)

        self.detail_box = QGroupBox("Event options")
        detail_layout = QVBoxLayout(self.detail_box)
        self.detail_title = QLabel("Select an event above.")
        self.detail_title.setWordWrap(True)
        detail_layout.addWidget(self.detail_title)

        self.fee_container = QWidget()
        self.fee_layout = QVBoxLayout(self.fee_container)
        self.fee_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addWidget(self.fee_container)

        self.deck_row = QWidget()
        deck_row_layout = QHBoxLayout(self.deck_row)
        deck_row_layout.setContentsMargins(0, 0, 0, 0)
        deck_row_layout.addWidget(QLabel("Deck:"))
        self.deck_combo = QComboBox()
        deck_row_layout.addWidget(self.deck_combo, 1)
        self.deck_row.hide()
        detail_layout.addWidget(self.deck_row)

        self.adapt_box = QWidget()
        adapt_layout = QVBoxLayout(self.adapt_box)
        adapt_layout.setContentsMargins(0, 4, 0, 0)
        self.adapt_hint = QLabel(
            "No qualifying deck found, but an existing deck can be cloned and adapted with owned cards (0 wildcards):"
        )
        self.adapt_hint.setWordWrap(True)
        adapt_layout.addWidget(self.adapt_hint)
        adapt_ctrl = QHBoxLayout()
        adapt_ctrl.setContentsMargins(0, 0, 0, 0)
        self.adapt_source_combo = QComboBox()
        adapt_ctrl.addWidget(self.adapt_source_combo, 1)
        self.adapt_btn = QPushButton("Adapt Deck (Owned Cards Only)")
        self.adapt_btn.clicked.connect(self._on_adapt_clicked)
        adapt_ctrl.addWidget(self.adapt_btn)
        adapt_layout.addLayout(adapt_ctrl)
        self.adapt_box.hide()
        detail_layout.addWidget(self.adapt_box)

        self.reenter_free_cb = QCheckBox("Auto-reenter and grind when this free event finishes")
        self.reenter_free_cb.hide()
        detail_layout.addWidget(self.reenter_free_cb)

        root.addWidget(self.detail_box)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_btn)
        self.start_btn = QPushButton("Start Event")
        self.start_btn.setProperty("variant", "primary")
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._on_start_clicked)
        btn_row.addWidget(self.start_btn)
        root.addLayout(btn_row)

    def _on_refresh_clicked(self) -> None:
        self._sticky_status_message = ""
        self.status_label.setText("Refreshing available events from MTG Arena…")
        self.status_label.show()
        self.refreshRequested.emit()

    def set_decks(self, decks: list[dict[str, Any]]) -> None:
        self._decks = list(decks or [])
        if self._events:
            self._render_event_list()
        else:
            current = self._selected_event()
            if current is not None:
                self._populate_deck_combo(current)

    def update_events(self, payload: dict[str, Any]) -> None:
        self._wallet = dict(payload.get("wallet") or {})
        self.wallet_label.setText(_wallet_summary(self._wallet))
        self._on_home = payload["on_home"] if isinstance(payload.get("on_home"), bool) else None
        if not payload.get("ok"):
            self._events = []
            self.event_list.clear()
            self.status_label.setText(str(payload.get("error") or "Could not load Arena events."))
            self.status_label.show()
            self._render_selected_event(None)
            return

        self._events = list(payload.get("events") or [])
        if not self._events:
            self.event_list.clear()
            self.status_label.setText("No playable events were reported on the Arena Home screen.")
            self.status_label.show()
            self._render_selected_event(None)
            return
        self._render_event_list()

    def _render_event_list(self) -> None:
        prev = self._selected_event()
        prev_name = str(prev.get("event_name") or "") if prev else ""
        self.event_list.clear()

        grouped: dict[str, list[dict[str, Any]]] = {k: [] for k, _ in _CATEGORY_GROUPS}
        for ev in self._events:
            grouped[_event_group_key(ev)].append(ev)

        first_selectable: QListWidgetItem | None = None
        first_buildable: QListWidgetItem | None = None
        preferred_item: QListWidgetItem | None = None
        preserved_item: QListWidgetItem | None = None
        for group_key, heading in _CATEGORY_GROUPS:
            rows = grouped[group_key]
            if not rows:
                continue
            header = QListWidgetItem(f"— {heading} —")
            header.setFlags(Qt.NoItemFlags)
            self.event_list.addItem(header)
            for ev in rows:
                eligible, reason = event_eligibility(ev, self._decks)
                can_build = (not eligible) and event_can_build_deck(ev, self._decks)
                title = str(ev.get("display_name") or ev.get("event_name") or "Event")
                cost_str = (
                    f"In progress ({ev.get('current_wins', 0)}-{ev.get('current_losses', 0)})"
                    if ev.get("in_progress")
                    else (
                        " / ".join(format_entry_fee(f) for f in ev.get("entry_fees") or []) or "Unknown fee"
                    )
                )
                rules = describe_event_rules(ev)
                line = f"{title}  [{cost_str}]"
                if rules:
                    line += f"  ·  {rules}"
                if can_build:
                    line += f"  —  {reason} (adapt deck)"
                elif not eligible:
                    line += f"  —  {reason}"
                item = QListWidgetItem(line)
                item.setData(Qt.UserRole, ev)
                if not eligible and not can_build:
                    item.setFlags(Qt.NoItemFlags)
                self.event_list.addItem(item)
                ev_name = str(ev.get("event_name") or "")
                if eligible:
                    if first_selectable is None:
                        first_selectable = item
                    if self._preferred_event_name and ev_name == self._preferred_event_name:
                        preferred_item = item
                    elif prev_name and ev_name == prev_name:
                        preserved_item = item
                elif can_build:
                    if first_buildable is None:
                        first_buildable = item
                    if prev_name and ev_name == prev_name:
                        preserved_item = item

        if self._on_home is False and (first_selectable is not None or first_buildable is not None):
            self.status_label.setText(
                "Arena is not on the Home screen — click Home in Arena so Coach can open your selected event."
            )
            self.status_label.show()
        elif self._events and first_selectable is None and first_buildable is None:
            self.status_label.setText(
                "No eligible events right now — check your balances and qualifying decks below."
            )
            self.status_label.show()
        elif self._sticky_status_message:
            self.status_label.setText(self._sticky_status_message)
            self.status_label.show()
            self._sticky_status_message = ""
        else:
            self.status_label.hide()

        self._preferred_event_name = ""
        target = preferred_item or preserved_item or first_selectable or first_buildable
        if target is not None:
            self.event_list.setCurrentItem(target)
        else:
            self._render_selected_event(None)

    def _selected_event(self) -> dict[str, Any] | None:
        item = self.event_list.currentItem()
        if item is None:
            return None
        data = item.data(Qt.UserRole)
        return dict(data) if isinstance(data, dict) else None

    def _on_selection_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        data = current.data(Qt.UserRole) if current is not None else None
        self._render_selected_event(dict(data) if isinstance(data, dict) else None)

    def _clear_fee_buttons(self) -> None:
        for btn, _ in self._fee_buttons:
            self._fee_group.removeButton(btn)
            btn.deleteLater()
        self._fee_buttons.clear()

    def _populate_deck_combo(self, ev: dict[str, Any]) -> None:
        self.deck_combo.clear()
        fmt = str(ev.get("deck_select_format") or "")
        qual = eligible_decks_for_event(ev, self._decks)
        if not qual:
            self.deck_combo.addItem("No qualifying deck found", "")
            self.deck_combo.setEnabled(False)
            return
        self.deck_combo.setEnabled(True)
        self.deck_combo.addItem("Auto: Best legal deck (by win rate)", "")
        selected_idx = 0
        for deck in qual:
            did = str(deck.get("deck_id") or "")
            label = f"{deck.get('name') or 'Deck'} ({deck.get('format') or fmt})"
            self.deck_combo.addItem(label, did)
            if self._preferred_built_deck_id and did == self._preferred_built_deck_id:
                selected_idx = self.deck_combo.count() - 1
        if selected_idx > 0:
            self.deck_combo.setCurrentIndex(selected_idx)
            self._preferred_built_deck_id = ""

    def _populate_adapt_box(self, ev: dict[str, Any]) -> None:
        self.adapt_source_combo.clear()
        can_build = event_can_build_deck(ev, self._decks)
        self.adapt_box.setVisible(can_build)
        if not can_build:
            return
        fmt = str(ev.get("deck_select_format") or "this event")
        self.adapt_hint.setText(
            f"No qualifying {fmt} deck yet. Clone and adapt a near-qualifying deck using owned cards only (0 wildcards):"
        )
        for deck in near_decks_for_event(ev):
            summary = str(deck.get("summary") or "not legal")
            label = f"{deck.get('name') or 'Deck'} — {summary}"
            self.adapt_source_combo.addItem(label, str(deck.get("deck_id") or ""))
        self.adapt_btn.setEnabled(self.adapt_source_combo.count() > 0)

    def _on_adapt_clicked(self) -> None:
        ev = self._displayed_event if self._displayed_event is not None else self._selected_event()
        if ev is None:
            return
        source_id = str(self.adapt_source_combo.currentData() or "").strip()
        if not source_id:
            return
        fmt = str(ev.get("deck_select_format") or "").strip()
        self.adapt_btn.setEnabled(False)
        self.status_label.setText(
            f"Cloning and adapting deck for {fmt} using owned cards only, then validating in Arena…"
        )
        self.status_label.show()
        self.buildDeckRequested.emit(
            {
                "event_name": str(ev.get("event_name") or ""),
                "display_name": str(ev.get("display_name") or ev.get("event_name") or ""),
                "event_format": fmt,
                "source_deck_id": source_id,
            }
        )

    def on_deck_built(self, payload: dict[str, Any]) -> None:
        self.adapt_btn.setEnabled(self.adapt_source_combo.count() > 0)
        if payload.get("ok"):
            self._preferred_event_name = str(payload.get("event_name") or "")
            self._preferred_built_deck_id = str(payload.get("deck_id") or "")
            msg = str(payload.get("summary") or f"Built '{payload.get('deck_name') or 'deck'}'.")
            self._sticky_status_message = msg
            self.status_label.setText(msg)
            self.status_label.show()
            self.refreshRequested.emit()
            return
        self._sticky_status_message = ""
        self.status_label.setText(str(payload.get("error") or "Could not adapt deck for this event."))
        self.status_label.show()

    def _render_selected_event(self, ev: dict[str, Any] | None) -> None:
        self._displayed_event = ev
        self._clear_fee_buttons()
        if ev is None:
            self.detail_title.setText("Select an event above.")
            self.deck_row.hide()
            self.adapt_box.hide()
            self.reenter_free_cb.hide()
            self.start_btn.setEnabled(False)
            self.start_btn.setText("Start Event")
            return

        title = str(ev.get("display_name") or ev.get("event_name") or "Event")
        rules = describe_event_rules(ev)
        self.detail_title.setText(f"{title}" + (f"  —  {rules}" if rules else ""))

        in_progress = bool(ev.get("in_progress"))
        fees = list(ev.get("entry_fees") or [])
        if not in_progress:
            for fee in fees:
                label = format_entry_fee(fee)
                if not fee.get("affordable", False):
                    label += " (insufficient)"
                rb = QRadioButton(label)
                rb.setEnabled(bool(fee.get("affordable", False)))
                rb.toggled.connect(self._update_start_button)
                self._fee_group.addButton(rb)
                self.fee_layout.addWidget(rb)
                self._fee_buttons.append((rb, fee))
            first_ok = next((rb for rb, f in self._fee_buttons if f.get("affordable")), None)
            if first_ok is not None:
                first_ok.setChecked(True)

        needs_deck = event_needs_deck(ev)
        self.deck_row.setVisible(needs_deck)
        if needs_deck:
            self._populate_deck_combo(ev)
            self._populate_adapt_box(ev)
        else:
            self.adapt_box.hide()

        can_auto_reenter = (
            bool(ev.get("is_free")) and str(ev.get("loss_details_type") or "") != "PlayUntilEventEnds"
        )
        self.reenter_free_cb.setVisible(can_auto_reenter)
        if not can_auto_reenter:
            self.reenter_free_cb.setChecked(False)

        self._update_start_button()

    def _selected_fee(self) -> dict[str, Any] | None:
        for rb, fee in self._fee_buttons:
            if rb.isChecked():
                return fee
        return None

    def _update_start_button(self) -> None:
        ev = self._displayed_event if self._displayed_event is not None else self._selected_event()
        if ev is None:
            self.start_btn.setEnabled(False)
            return
        eligible, reason = event_eligibility(ev, self._decks)
        if not eligible:
            self.start_btn.setEnabled(False)
            self.start_btn.setText(f"Cannot Enter ({reason})")
            return
        if ev.get("in_progress"):
            self.start_btn.setEnabled(True)
            self.start_btn.setText("Resume Event")
            return
        fee = self._selected_fee()
        if fee is None:
            if ev.get("is_free"):
                self.start_btn.setEnabled(True)
                self.start_btn.setText("Start Free Event")
            else:
                self.start_btn.setEnabled(False)
                self.start_btn.setText("Cannot Enter (Insufficient Balance)")
            return
        if fee.get("is_free"):
            self.start_btn.setEnabled(True)
            self.start_btn.setText("Start Free Event")
        else:
            self.start_btn.setEnabled(True)
            self.start_btn.setText(f"Pay {format_entry_fee(fee)} & Start")

    def _on_start_clicked(self) -> None:
        ev = self._selected_event()
        if ev is None:
            return
        eligible, _reason = event_eligibility(ev, self._decks)
        if not eligible:
            return
        fee = self._selected_fee()
        is_free = bool(ev.get("is_free")) if fee is None else bool(fee.get("is_free"))
        currency = "Free" if fee is None else str(fee.get("currency_type") or "Free")
        quantity = 0 if fee is None else int(fee.get("quantity") or 0)
        deck_id = str(self.deck_combo.currentData() or "") if self.deck_row.isVisible() else ""
        deck_name = self.deck_combo.currentText() if (self.deck_row.isVisible() and deck_id) else ""
        payload = {
            "event_name": ev["event_name"],
            "display_name": ev.get("display_name") or ev["event_name"],
            "category": ev.get("category") or "",
            "is_free": is_free,
            "entry_currency": currency,
            "entry_quantity": quantity,
            "deck_id": deck_id,
            "deck_name": deck_name,
            "reenter_free": bool(self.reenter_free_cb.isVisible() and self.reenter_free_cb.isChecked()),
            "stop_at_main_prize": True,
            "confirm_paid": bool((not ev.get("in_progress")) and (not is_free)),
        }
        self.eventConfigured.emit(payload)
        self.accept()


class EventReentryDialog(QDialog):
    """Prompt shown after an event run ends (or when paid entry approval is needed)."""

    responded = Signal(str, str, str, int)  # request_id, action, currency, quantity

    def __init__(self, payload: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._request_id = str(payload.get("request_id") or "")
        name = str(payload.get("display_name") or payload.get("event_name") or "Event")
        outcome = str(payload.get("outcome") or "completed")
        wins = int(payload.get("wins") or 0)
        losses = int(payload.get("losses") or 0)
        heading = _OUTCOME_HEADINGS.get(outcome, "Event run finished")

        self.setWindowTitle(f"{name} — {heading}")
        self.setMinimumWidth(400)
        self._responded = False
        self.rejected.connect(self._on_rejected)
        root = QVBoxLayout(self)
        root.setSpacing(10)

        title_lbl = QLabel(f"<b>{heading}</b>")
        title_lbl.setTextFormat(Qt.RichText)
        root.addWidget(title_lbl)

        record_lbl = QLabel(f"Event: {name}\nRecord: {wins}W - {losses}L")
        record_lbl.setWordWrap(True)
        root.addWidget(record_lbl)

        fees = list(payload.get("entry_fees") or [])
        if not fees and payload.get("is_free"):
            fees = [{"currency_type": "Free", "quantity": 0, "affordable": True, "is_free": True}]

        for fee in fees:
            if not fee.get("affordable", False):
                continue
            label = f"Re-enter ({format_entry_fee(fee)})"
            btn = QPushButton(label)
            btn.setProperty("variant", "primary")
            curr = str(fee.get("currency_type") or "Free")
            qty = int(fee.get("quantity") or 0)
            btn.clicked.connect(lambda _checked=False, c=curr, q=qty: self._finish("reenter", c, q))
            root.addWidget(btn)

        switch_btn = QPushButton("Choose another event…")
        switch_btn.clicked.connect(lambda: self._finish("switch_event", "", 0))
        root.addWidget(switch_btn)

        pause_btn = QPushButton("Pause / Done")
        pause_btn.clicked.connect(lambda: self._finish("pause", "", 0))
        root.addWidget(pause_btn)

    def _on_rejected(self) -> None:
        if not self._responded:
            self._responded = True
            self.responded.emit(self._request_id, "pause", "", 0)

    def _finish(self, action: str, currency: str, quantity: int) -> None:
        self._responded = True
        self.responded.emit(self._request_id, action, currency, quantity)
        self.accept()
