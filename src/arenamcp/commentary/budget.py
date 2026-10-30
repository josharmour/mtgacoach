"""Non-waiting commentary admission; all attempts count, including cancellation."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


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
        "first_token_timeout",
        "request_timeout",
        "invalid_json",
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
    request_seconds: float = 0.0
    call_started: float | None = None
    closed: bool = False
    usable: bool = True


class CommentaryBudget:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.history = deque()
        self.active = None

    def begin(self, rate=2):
        now = self.clock()
        while self.history and now - self.history[0].started >= 60:
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

    def before_call(self, exchange, system, user, *, audit, schema=""):
        now = self.clock()
        if exchange is not self.active or exchange.closed or exchange.call_started is not None:
            raise BudgetExceeded("obsolete_exchange")
        if exchange.calls != int(audit) or now >= exchange.deadline - 0.1:
            raise BudgetExceeded("call_count_or_deadline")
        # UTF-8 bytes plus message framing are a conservative token upper bound;
        # no gateway-specific tokenizer guess can undercount a dense Unicode prompt.
        tokens = len(system.encode("utf-8")) + len(user.encode("utf-8")) + len(schema.encode("utf-8")) + 256
        output = 256 if audit else 512
        if tokens > 6000:
            raise BudgetExceeded("context_budget")
        if sum(item.input_tokens for item in self.history) + tokens > 48000:
            raise BudgetExceeded("input_budget")
        if sum(item.output_tokens for item in self.history) + output > 3072:
            raise BudgetExceeded("output_budget")
        exchange.calls += 1
        exchange.input_tokens += tokens
        exchange.output_tokens += output  # reserve even when provider usage is missing
        exchange.call_started = now
        return output, max(0.1, exchange.deadline - now)

    def after_call(self, exchange):
        if exchange.call_started is not None:
            exchange.request_seconds += max(0, self.clock() - exchange.call_started)
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
