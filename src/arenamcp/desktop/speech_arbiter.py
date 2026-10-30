"""Single-threaded speech arbitration, independent of Qt and audio devices.

The desktop integration supplies start/stop/status callbacks and calls tick()
regularly. start() renders through its sole TtsManager; only playback callbacks
may report started/finished. CoachSession routes all desktop speech through this scheduler.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from arenamcp.commentary.contracts import TERMINAL_STATES, Epoch, SpeechCategory, Utterance

_PRIORITY = {category: rank for rank, category in enumerate(SpeechCategory)}
_OPTIONAL = {SpeechCategory.COMMENTARY, SpeechCategory.AMBIENT}


class SpeechArbiter:
    def __init__(
        self,
        *,
        start: Callable[[Utterance], None],
        stop: Callable[[], None],
        status: Callable[[str, str], None],
        clock: Callable[[], float] = time.monotonic,
        valid: Callable[[Utterance], bool] = lambda utterance: True,
    ) -> None:
        self._start, self._stop, self._status = start, stop, status
        self._clock, self._valid = clock, valid
        self.epoch: Epoch | None = None
        self.active: Utterance | None = None
        self.pending: Utterance | None = None
        self.muted = False
        self._started = False
        # Bounded by an explicit per-epoch admission cap, never evict duplicate IDs.
        self._states: dict[str, str] = {}
        self.reasons: dict[str, str] = {}

    def _emit(self, utterance: Utterance, state: str, reason: str = "") -> None:
        if self._states.get(utterance.utterance_id) in TERMINAL_STATES:
            return
        self._states[utterance.utterance_id] = state
        self.reasons[utterance.utterance_id] = reason or state
        self._status(utterance.utterance_id, state)

    def _eligible(self, utterance: Utterance) -> bool:
        return (
            utterance.epoch == self.epoch and self._clock() < utterance.expires_at and self._valid(utterance)
        )

    def submit(self, utterance: Utterance, *, allow_muted: bool = False) -> bool:
        if utterance.utterance_id in self._states:
            return False
        if len(self._states) >= 4096:
            return False  # Caller must treat False as not admitted.
        self._emit(utterance, "accepted")
        if (self.muted and not allow_muted) or not self._eligible(utterance):
            self._emit(
                utterance,
                "muted" if self.muted else "superseded",
                "muted" if self.muted else "expired_or_changed",
            )
            return True
        if utterance.anchor_id and self._states.get(utterance.anchor_id) in TERMINAL_STATES - {"finished"}:
            self._emit(utterance, "superseded", "anchor_not_delivered")
            return True
        active = self.active
        if (
            active
            and _PRIORITY[utterance.category] < _PRIORITY[active.category]
            and (active.interruptible or utterance.category == SpeechCategory.ESSENTIAL)
        ):
            self._cancel_active("superseded", "higher_priority_speech")
        if self.pending:
            if _PRIORITY[utterance.category] > _PRIORITY[self.pending.category]:
                self._emit(utterance, "superseded")
                return True
            self._emit(self.pending, "superseded", "newer_speech")
        self.pending = utterance
        self.tick()
        return True

    def _cancel_active(self, state: str, reason: str = "") -> None:
        old, self.active = self.active, None
        self._started = False
        if old:
            # Clear before stop: synchronous stop acknowledgments cannot finish a newer ID.
            self._stop()
            self._emit(old, state, reason)

    def tick(self) -> None:
        if self.active and (
            not self._valid(self.active) or (not self._started and self._clock() >= self.active.expires_at)
        ):
            self._cancel_active("superseded", "expired_or_changed")
        item = self.pending
        if not item:
            return
        anchor_state = self._states.get(item.anchor_id) if item.anchor_id else None
        if not self._eligible(item) or (item.anchor_id and anchor_state in TERMINAL_STATES - {"finished"}):
            self.pending = None
            self._emit(item, "superseded", "expired_or_changed_or_anchor")
            return
        if self.active or (item.anchor_id and anchor_state != "finished"):
            return
        self.pending, self.active = None, item
        self._started = False
        try:
            self._start(item)
        except Exception:
            self.playback_status(item.utterance_id, "failed", epoch=item.epoch)

    def playback_status(self, utterance_id: str, state: str, *, epoch: Epoch) -> None:
        item = self.active
        if not item or item.utterance_id != utterance_id or item.epoch != epoch:
            return
        if state == "started" and not self._started:
            self._started = True
            self._emit(item, state)
        elif state in TERMINAL_STATES:
            # A render/download completion must never be called finished.
            if state == "finished" and not self._started:
                state = "failed"
            self.active = None
            self._started = False
            self._emit(item, state)
            self.tick()

    def reset(self, epoch: Epoch | None) -> None:
        self.clear("epoch_changed")
        self.epoch = epoch
        self._states.clear()
        self.reasons.clear()

    def clear(self, reason: str = "user_stop") -> None:
        pending, self.pending = self.pending, None
        if pending:
            self._emit(pending, "stopped", reason)
        self._cancel_active("stopped", reason)

    def set_muted(self, muted: bool) -> None:
        self.muted = muted
        if muted:
            pending, self.pending = self.pending, None
            if pending:
                self._emit(pending, "muted")
            self._cancel_active("muted")

    def disable_routine(self) -> None:
        if self.pending and self.pending.category == SpeechCategory.ROUTINE:
            pending, self.pending = self.pending, None
            self._emit(pending, "stopped")
        if self.active and self.active.category == SpeechCategory.ROUTINE:
            self._cancel_active("stopped")
        self.tick()

    def disable_commentary(self, reason: str = "mode_changed") -> None:
        if self.pending and self.pending.category in _OPTIONAL:
            pending, self.pending = self.pending, None
            self._emit(pending, "stopped", reason)
        if self.active and self.active.category in _OPTIONAL:
            self._cancel_active("stopped", reason)
        self.tick()
