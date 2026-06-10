"""Per-request submission state machine (fable-improvements.md items 2+3).

Tracks each logical GRE request through:

    PENDING → SUBMITTED → ADVANCED | REJECTED | ROLLED_BACK

Identity is **content-addressed**: a fingerprint of the decision's request
type + option set. The GRE re-issues "the same logical decision" with fresh
msgId/gameStateId values (see issue #231), so raw ids churn while the
content stays identical — the fingerprint is what actually says "this is
the window we already answered".

Rules enforced:
- One in-flight submission per request — a second submit for the same
  fingerprint cannot fire until the first settles. Machine-gunning becomes
  structurally impossible.
- Settlement is observation-driven: seeing a *different* decision settles
  the in-flight one as ADVANCED; seeing the *same* fingerprint again after
  a grace period settles it as REJECTED (the GRE re-presented our window).
- A hard per-request submission cap; reaching it means the request needs
  a human (callers surface MANUAL REQUIRED once and stand down).
- Escapes (AutoRespond) are allowed only for requests with enough REJECTED
  outcomes — never for a request nobody has attempted.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

Fingerprint = tuple


def decision_fingerprint(decision: Any) -> Fingerprint:
    """Content-addressed identity for a PendingDecision."""
    return (
        decision.request_type,
        tuple(sorted(o.option_id for o in decision.options)),
        int(decision.min_select or 0),
        int(decision.max_select or 0),
    )


@dataclass
class _Record:
    fingerprint: Fingerprint
    submissions: int = 0
    rejected: int = 0
    rolled_back: int = 0
    submitted_at: float = 0.0
    in_flight: bool = False
    first_seen_at: float = field(default_factory=time.monotonic)


class RequestTracker:
    """Submission lifecycle per logical request (content fingerprint)."""

    # A request answered this many times without advancing needs a human.
    MAX_SUBMISSIONS_PER_REQUEST = 3
    # Re-seeing the same fingerprint within this window after a submit is
    # normal processing lag, not a rejection.
    REJECT_GRACE_S = 2.0
    # AutoRespond escapes require at least this many REJECTED outcomes.
    ESCAPE_AFTER_REJECTED = 2

    def __init__(self) -> None:
        self._records: dict[Fingerprint, _Record] = {}
        self._in_flight: Optional[Fingerprint] = None

    def _record(self, fp: Fingerprint) -> _Record:
        rec = self._records.get(fp)
        if rec is None:
            rec = _Record(fingerprint=fp)
            self._records[fp] = rec
        return rec

    # -- lifecycle ----------------------------------------------------

    def observe(self, fp: Optional[Fingerprint]) -> None:
        """Feed the currently-pending decision fingerprint (None = nothing).

        Settles any in-flight submission:
          - different/absent fingerprint → ADVANCED
          - same fingerprint after the grace period → REJECTED
        """
        flight = self._in_flight
        if flight is None:
            return
        rec = self._record(flight)
        if fp != flight:
            rec.in_flight = False
            self._in_flight = None
            logger.debug(f"request {flight[0]}: ADVANCED after submit")
            try:
                from arenamcp.match_packets import get_current_packet
                packet = get_current_packet()
                if packet:
                    packet.update_outcome(flight, "ADVANCED")
            except Exception as e:
                logger.warning(f"MatchPacket: failed to update ADVANCED outcome: {e}")
            return
        age = time.monotonic() - rec.submitted_at
        if age >= self.REJECT_GRACE_S:
            rec.in_flight = False
            rec.rejected += 1
            self._in_flight = None
            logger.warning(
                f"request {flight[0]}: REJECTED (re-presented {age:.1f}s "
                f"after submit; rejection #{rec.rejected})"
            )
            try:
                from arenamcp.match_packets import get_current_packet
                packet = get_current_packet()
                if packet:
                    packet.update_outcome(flight, "REJECTED")
            except Exception as e:
                logger.warning(f"MatchPacket: failed to update REJECTED outcome: {e}")

    def may_submit(self, fp: Fingerprint) -> bool:
        rec = self._record(fp)
        if rec.in_flight:
            return False  # one in-flight submission per request
        return rec.submissions < self.MAX_SUBMISSIONS_PER_REQUEST

    def note_submitted(self, fp: Fingerprint) -> None:
        rec = self._record(fp)
        rec.submissions += 1
        rec.submitted_at = time.monotonic()
        rec.in_flight = True
        self._in_flight = fp

    def note_rolled_back(self, fp: Fingerprint) -> None:
        rec = self._record(fp)
        rec.rolled_back += 1
        if self._in_flight == fp:
            rec.in_flight = False
            self._in_flight = None
            try:
                from arenamcp.match_packets import get_current_packet
                packet = get_current_packet()
                if packet:
                    packet.update_outcome(fp, "ROLLED_BACK")
            except Exception as e:
                logger.warning(f"MatchPacket: failed to update ROLLED_BACK outcome: {e}")

    # -- queries --------------------------------------------------------

    def exhausted(self, fp: Fingerprint) -> bool:
        """True when the request hit the submission cap without advancing."""
        rec = self._record(fp)
        return (
            not rec.in_flight
            and rec.submissions >= self.MAX_SUBMISSIONS_PER_REQUEST
        )

    def may_escape(self, fp: Fingerprint) -> bool:
        """AutoRespond is allowed only after enough real rejections."""
        return self._record(fp).rejected >= self.ESCAPE_AFTER_REJECTED

    def rejections(self, fp: Fingerprint) -> int:
        return self._record(fp).rejected

    def reset(self) -> None:
        """New match — drop all request history."""
        self._records.clear()
        self._in_flight = None
