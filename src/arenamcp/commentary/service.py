"""Engine-side coordinator: one optional worker, no waits on gameplay threads."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time

from .budget import BudgetExceeded, CommentaryBudget
from .client import CommentaryClient
from .config import CommentaryConfig
from .contracts import TERMINAL_STATES, Utterance
from .events import ObservationAdapter
from .headless import deterministic

logger = logging.getLogger(__name__)


class CommentaryService:
    def __init__(self, *, settings, backend, emit, client_factory=CommentaryClient, muted=lambda: False):
        self.settings, self.backend, self.emit = settings, backend, emit
        self.muted = muted
        self.config = CommentaryConfig.from_settings(settings())
        self.client = client_factory()
        self.adapter = ObservationAdapter()
        self.budget = CommentaryBudget()
        self.cancelled = threading.Event()
        self.closed = threading.Event()
        self.wake = threading.Event()
        self.latest = None
        self.thread = None
        self.deliveries = queue.Queue(maxsize=64)
        self.inflight = {}
        self.last_state = None
        self.last_grounded = (None, -float("inf"))
        self._lock = threading.Lock()

    def configure(self):
        config = CommentaryConfig.from_settings(self.settings())
        if config != self.config:
            self.config = config
            self.cancelled.set()
            with self._lock:
                self.inflight.clear()
            self.emit({"type": "commentary_clear"})
        self.wake.set()

    def offer(self, snapshot):
        # The producer hands over an already-published detached snapshot. No
        # projection, model work, or worker I/O occurs on the gameplay thread.
        previous = self.latest
        self.latest = snapshot
        if previous and (previous.get("match_id"), previous.get("game_number")) != (
            snapshot.get("match_id"),
            snapshot.get("game_number"),
        ):
            self.cancelled.set()
        if self.muted():
            self.cancelled.set()
        if self.config.enabled and not self.muted() and self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True, name="coach-commentary")
            self.thread.start()
        self.wake.set()

    def delivery(self, uid, state):
        if state in TERMINAL_STATES:
            with self._lock:
                self.inflight.pop(uid, None)
            try:
                self.deliveries.put_nowait((uid, state))
            except queue.Full:
                self.cancelled.set()
            self.wake.set()

    def status(self, state, **details):
        signature = (state, details.get("reason"))
        if (
            state in {"waiting_events", "waiting_delivery", "waiting_capacity"}
            and signature == self.last_state
        ):
            return
        self.last_state = signature
        self.emit({"type": "commentary_status", "data": {"state": state, **details}})
        logger.info("Commentary %s %s", state, details)

    def _backend_request(self, exchange, system, context, audit, expected=None):
        if self.cancelled.is_set() or self.muted():
            raise BudgetExceeded("cancelled")
        if expected is not None:
            fresh = self.adapter.project(self.latest, self.config.private_information)
            if fresh.epoch != expected.epoch or fresh.snapshot_id != expected.snapshot_id:
                raise BudgetExceeded("facts_changed")
        user = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        output, timeout = self.budget.before_call(exchange, system, user, audit=audit)
        try:
            backend = self.backend()
            # An older/custom backend must not silently ignore admission/cancellation.
            from arenamcp.backends.proxy import ProxyBackend

            if not isinstance(backend, ProxyBackend):
                raise BudgetExceeded("backend_has_no_commentary_admission")
            response = backend.complete(
                system,
                user,
                output,
                temperature=0 if audit else 0.3,
                enable_thinking=False,
                request_timeout_s=timeout,
                raise_on_error=True,
                response_format={"type": "json_object"},
                background=True,
                call_class="background.commentary",
                priority=30,
                cancel_event=self.cancelled,
                lane_wait_s=0,
                first_token_timeout_s=min(2, timeout),
            )
            if self.cancelled.is_set() or time.monotonic() >= exchange.deadline:
                raise BudgetExceeded("cancelled_or_late")
            return json.loads(response)
        finally:
            self.budget.after_call(exchange)

    def _grounded(self, observation, config, reason):
        """Narrate observed public facts when optional analysis cannot run."""
        now = time.monotonic()
        if self.cancelled.is_set() or self.muted() or config != self.config or not config.enabled:
            return
        fresh = self.adapter.project(self.latest, config.private_information)
        if fresh.epoch != observation.epoch:
            return
        if self.last_grounded[0] == observation.epoch and now - self.last_grounded[1] < 10:
            self.status("waiting_capacity", reason="grounded_call_spacing")
            return
        lines = deterministic(observation, config)
        if not lines:
            self.status("waiting_events", reason="no_eligible_public_event")
            return
        item = lines[0]
        if now >= item.expires_at or (not item.event_bound and fresh.snapshot_id != item.snapshot_id):
            self.status("dropped", reason="facts_changed")
            return
        with self._lock:
            self.inflight[item.utterance_id] = item.expires_at + 30
        self.last_grounded = (observation.epoch, now)
        self.emit({"type": "commentary_proposal", "utterance": item.to_wire()})
        self.status("grounded", reason=reason)

    def _run(self):
        try:
            while not self.closed.is_set():
                self.wake.wait(0.1)
                self.wake.clear()
                while not self.deliveries.empty():
                    uid, state = self.deliveries.get_nowait()
                    try:
                        self.client.delivered(uid, state)
                    except Exception:
                        self.client.close()
                config, snapshot = self.config, self.latest
                if not config.enabled or self.muted() or snapshot is None:
                    self.client.close()
                    continue
                try:
                    observation = self.adapter.observe(snapshot, config.private_information)
                except (TypeError, ValueError, KeyError):
                    continue
                # Expire missing delivery acknowledgments; no unbounded speech backlog.
                now = time.monotonic()
                with self._lock:
                    self.inflight = {uid: expiry for uid, expiry in self.inflight.items() if expiry > now}
                    speaking = bool(self.inflight)
                if not observation.events or speaking:
                    self.status(
                        "waiting_delivery" if speaking else "waiting_events",
                        reason="speech_inflight" if speaking else "no_fresh_public_event",
                    )
                    continue
                self.cancelled.clear()
                exchange = None
                try:
                    # Worker setup is not inference occupancy; no model loads here.
                    from arenamcp.backends.proxy import _BACKGROUND_LANE

                    if config.generative and not _BACKGROUND_LANE.commentary_available():
                        self._grounded(observation, config, "analysis_waits_for_gameplay")
                        continue
                    self.client.start(config)
                    exchange = self.budget.begin(config.exchanges_per_minute)
                    result = self.client.generate(
                        observation,
                        config,
                        lambda system, context, audit, exchange=exchange, observation=observation: (
                            self._backend_request(exchange, system, context, audit, observation)
                        ),
                        self.cancelled,
                    )
                    fresh = self.adapter.project(self.latest, config.private_information)
                    if (
                        self.cancelled.is_set()
                        or self.muted()
                        or config != self.config
                        or fresh.epoch != observation.epoch
                        or fresh.snapshot_id != observation.snapshot_id
                    ):
                        self.status("dropped", reason="facts_or_mode_changed")
                        self.client.close()
                        self._grounded(observation, config, "analysis_facts_changed")
                        continue
                    utterances = [Utterance.from_wire(value) for value in result.get("utterances", [])[:2]]
                    if any(
                        u.epoch != observation.epoch
                        or u.snapshot_id != observation.snapshot_id
                        or (config.generative and u.event_bound)
                        for u in utterances
                    ):
                        raise ValueError("invalid worker provenance")
                    if not utterances:
                        self._grounded(
                            observation, config, result.get("status", {}).get("state", "analysis_unavailable")
                        )
                        continue
                    for item in utterances:
                        if time.monotonic() >= item.expires_at:
                            self.client.delivered(item.utterance_id, "superseded")
                            continue
                        with self._lock:
                            self.inflight[item.utterance_id] = item.expires_at + 30
                        self.emit({"type": "commentary_proposal", "utterance": item.to_wire()})
                    self.status(
                        result.get("status", {}).get("state", "ready"),
                        observation_age=round(time.monotonic() - observation.arrived_at, 3),
                        calls=exchange.calls,
                        input_tokens_bound=exchange.input_tokens,
                        output_tokens_limit=exchange.output_tokens,
                        request_seconds=round(exchange.request_seconds, 3),
                    )
                except BudgetExceeded as error:
                    self._grounded(observation, config, str(error))
                except Exception as error:
                    self.status("unavailable", reason=type(error).__name__)
                    self.client.close()
                    self._grounded(observation, config, "analysis_unavailable")
                finally:
                    if exchange:
                        self.budget.finish(exchange)
        finally:
            self.client.close()

    def close(self):
        self.closed.set()
        self.cancelled.set()
        self.wake.set()
        # Only the background owner terminates its worker; gameplay never joins it.
