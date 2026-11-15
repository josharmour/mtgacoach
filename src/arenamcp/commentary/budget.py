"""Non-waiting commentary admission; all attempts count, including cancellation."""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    pass


# Transport only bounded reason codes, never exception messages with URLs or data.
BROKER_REASONS = frozenset(
    {
        "cancelled",
        "superseded",
        "dropped",
        "facts_changed",
        "cancelled_or_late",
        "backend_has_no_commentary_admission",
        "obsolete_exchange",
        "call_count_or_deadline",
        "context_budget",
        "input_budget",
        "output_budget",
        "audit_shape_changed",
        "first_token_timeout",
        "request_timeout",
        "invalid_json",
        "incomplete_final_response",
        "backend_unavailable",
        "admission_or_request_failed",
    }
)


def broker_failure_code(error):
    candidate = str(error) if isinstance(error, BudgetExceeded) else getattr(error, "reason", None)
    if candidate in BROKER_REASONS:
        return candidate
    if type(error).__name__ == "FirstTokenTimeout":
        return "first_token_timeout"
    if isinstance(error, TimeoutError):
        return "request_timeout"
    if type(error).__name__ == "JSONDecodeError":
        return "invalid_json"
    if type(error).__name__ == "BackendUnavailable":
        return "backend_unavailable"
    return "admission_or_request_failed"


@dataclass
class Exchange:
    started: float
    deadline: float
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reserved_output_tokens: int = 0
    request_seconds: float = 0.0
    call_output_limit: int = 0
    call_started: float | None = None
    last_call_finished: float | None = None
    closed: bool = False
    usable: bool = True
    single_reply_audit: bool = False
    attempt: str = field(default_factory=lambda: uuid.uuid4().hex)


class CommentaryBudget:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.history = deque()
        self.active = None

    def begin(self, rate=2):
        now = self.clock()
        while self.history:
            oldest = self.history[0]
            # Keep both reservations until even the later audit has aged out.
            # Include the full occupancy of a cancelled or interrupted call.
            end = oldest.last_call_finished if oldest.last_call_finished is not None else oldest.started
            if not oldest.closed or now - end < 60:
                break
            self.history.popleft()
        if (
            self.active is not None
            or sum(item.usable for item in self.history) >= min(4, max(1, rate))
            or len(self.history) >= 4
        ):
            raise BudgetExceeded("exchange_rate_or_busy")
        # Reserve the full deadline so concurrent/unfinished calls cannot oversubscribe occupancy.
        if sum(item.request_seconds for item in self.history) + 6 > 12:
            raise BudgetExceeded("request_time_budget")
        exchange = Exchange(now, now + 6)
        self.history.append(exchange)
        self.active = exchange
        return exchange

    def before_call(self, exchange, system, user, *, audit, schema="", single_reply=False):
        now = self.clock()
        if exchange is not self.active or exchange.closed or exchange.call_started is not None:
            raise BudgetExceeded("obsolete_exchange")
        if exchange.calls != int(audit) or now >= exchange.deadline - 0.1:
            raise BudgetExceeded("call_count_or_deadline")
        # UTF-8 bytes plus message framing are a conservative token upper bound;
        # no gateway-specific tokenizer guess can undercount a dense Unicode prompt.
        tokens = len(system.encode("utf-8")) + len(user.encode("utf-8")) + len(schema.encode("utf-8")) + 256
        # An already-heard lead needs exactly one analyst verdict. An unheard
        # lead can still be prepended locally and needs the full audit reserve.
        if audit and exchange.single_reply_audit and not single_reply:
            raise BudgetExceeded("audit_shape_changed")
        single_reply_audit = exchange.single_reply_audit if audit else single_reply
        generation_limit = 512
        audit_limit = 512 if single_reply_audit else 1024
        output = audit_limit if audit else generation_limit
        # An accepted generation must still pass its audit before it can air.
        # Check room for both, but charge only the calls actually attempted.
        required = audit_limit if audit else generation_limit + audit_limit
        if tokens > 6000:
            raise BudgetExceeded("context_budget")
        if sum(item.input_tokens for item in self.history) + tokens > 48000:
            raise BudgetExceeded("input_budget")
        if sum(item.output_tokens for item in self.history) + required > 3072:
            raise BudgetExceeded("output_budget")
        exchange.single_reply_audit = single_reply_audit
        exchange.calls += 1
        exchange.input_tokens += tokens
        exchange.reserved_output_tokens += output
        exchange.call_output_limit = output
        exchange.output_tokens += output  # reserve even when provider usage is missing
        exchange.call_started = now
        return output, max(0.1, exchange.deadline - now)

    def after_call(self, exchange, *, actual_output=None):
        if exchange.call_started is not None:
            if type(actual_output) is int and 0 < actual_output <= exchange.call_output_limit:
                exchange.output_tokens -= exchange.call_output_limit - actual_output
            exchange.call_output_limit = 0
            exchange.last_call_finished = self.clock()
            exchange.request_seconds += max(0, exchange.last_call_finished - exchange.call_started)
            exchange.call_started = None

    def finish(self, exchange, *, usable=True):
        self.after_call(exchange)
        exchange.closed = True
        exchange.usable = usable
        # Native no-call/empty polls cost no inference capacity. The service
        # separately backs them off, so they cannot spin or exhaust airtime.
        if not usable and exchange.calls == 0 and exchange in self.history:
            self.history.remove(exchange)
        if self.active is exchange:
            self.active = None
