"""Wait until the desktop has finished speaking one utterance.

The desktop owns audio, so the engine cannot block on playback itself. An
utterance sent with a ``speech_id`` is acknowledged over the pipe
(``speech_status`` commands): ``accepted`` → ``started`` → ``finished``, or it
ends ``stopped`` / ``superseded`` / ``failed`` / ``muted``. A desktop that
predates the protocol never acknowledges; callers then hold for a
conservative estimate of the speaking time instead of submitting early.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable

logger = logging.getLogger(__name__)

TERMINAL_STATES = frozenset({"finished", "stopped", "superseded", "failed", "muted"})
# Kokoro speaks ~2.7 words/s at 1.0x; estimate slower so a hold never cuts speech short.
WORDS_PER_SECOND = 2.3
# Time for the desktop to render speech before playback starts.
RENDER_ALLOWANCE_S = 6.0


def speaking_seconds(text: str, speed: float = 1.0) -> float:
    return max(3.0, len(text.split()) / (WORDS_PER_SECOND * max(0.5, float(speed or 1.0))))


class SpeechCompletion:
    """Desktop acknowledgments for utterances the engine is waiting on."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._cond = threading.Condition()
        self._states: dict[str, list[tuple[str, float]]] = {}

    def begin(self) -> str:
        speech_id = uuid.uuid4().hex
        with self._cond:
            self._states[speech_id] = []
        return speech_id

    def update(self, speech_id: str, state: str) -> bool:
        """Record an acknowledgment; stale ids and post-terminal states are ignored."""
        with self._cond:
            states = self._states.get(speech_id)
            if states is None or not state or (states and states[-1][0] in TERMINAL_STATES):
                return False
            states.append((state, self._clock()))
            self._cond.notify_all()
            return True

    def wait(
        self,
        speech_id: str,
        *,
        seconds_to_speak: float,
        cancelled: Callable[[], bool] = lambda: False,
        accept_timeout: float = 5.0,
        render_timeout: float = 45.0,
        poll: float = 0.25,
    ) -> str:
        """Block until the utterance ends and return its final state.

        Besides the terminal states this returns ``unacknowledged`` (no desktop
        reply: an older desktop), ``timeout`` (acknowledged but never finished
        within a generous bound) or ``cancelled``.
        """
        start = self._clock()
        try:
            with self._cond:
                while True:
                    states = self._states.get(speech_id, [])
                    if states and states[-1][0] in TERMINAL_STATES:
                        return states[-1][0]
                    if cancelled():
                        return "cancelled"
                    now = self._clock()
                    started = next((at for state, at in states if state == "started"), None)
                    if not states:
                        if now - start >= accept_timeout:
                            return "unacknowledged"
                    elif started is not None:
                        if now - started >= seconds_to_speak * 1.5 + 10.0:
                            return "timeout"
                    elif now - states[0][1] >= render_timeout:
                        return "timeout"
                    self._cond.wait(poll)
        finally:
            with self._cond:
                self._states.pop(speech_id, None)


def _hold(seconds: float, cancelled: Callable[[], bool], sleep: Callable[[float], None]) -> bool:
    remaining = seconds
    while remaining > 0 and not cancelled():
        step = min(0.25, remaining)
        sleep(step)
        remaining -= step
    return not cancelled()


def narrate_and_wait(
    text: str,
    *,
    send: Callable[[str, str], None] | None,
    completion: SpeechCompletion | None,
    cancelled: Callable[[], bool],
    speed: float = 1.0,
    muted: bool = False,
    attempts: int = 2,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wait_options: dict | None = None,
) -> bool:
    """True once the narration has been heard; False if it was stopped or cancelled.

    Speech that cannot be voiced (muted, failed, no acknowledgment, stuck
    renderer) still appears as text, so the caller continues only after the
    estimated speaking time has passed — never sooner.
    """
    expected = speaking_seconds(text, speed)
    if muted or send is None or completion is None:
        logger.info("Narration not voiced (muted=%s); holding %.0fs for reading", muted, expected)
        return _hold(expected, cancelled, sleep)
    for attempt in range(1, attempts + 1):
        speech_id = completion.begin()
        began = clock()
        send(text, speech_id)
        outcome = completion.wait(
            speech_id, seconds_to_speak=expected, cancelled=cancelled, **(wait_options or {})
        )
        logger.info("Narration %s attempt %d ended: %s", speech_id[:8], attempt, outcome)
        if outcome == "finished":
            return not cancelled()
        if outcome in ("stopped", "cancelled"):
            return False
        if outcome == "superseded":
            continue  # other speech replaced it; say it again
        allowance = RENDER_ALLOWANCE_S if outcome == "unacknowledged" else 0.0
        return _hold(expected + allowance - (clock() - began), cancelled, sleep)
    return False
