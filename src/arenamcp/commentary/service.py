"""Engine-side coordinator: one optional worker, no waits on gameplay threads."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import replace

from .budget import BudgetExceeded, CommentaryBudget
from .client import CommentaryClient
from .config import CommentaryConfig
from .contracts import TERMINAL_STATES, Utterance
from .events import ObservationAdapter
from .headless import deterministic
from .timeline import event_lifetime, position_fingerprint, select_events, valid_for

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
        self.analysis_diagnostics = {}
        self.next_analysis_at = 0.0
        self.last_grounded = (None, -float("inf"))
        self.pending_events = {}
        self.pending_epoch = None
        self.fallback_events = set()
        self.proposals = {}
        self.heard = deque(maxlen=4)
        self.openings = {}
        self.grounded_ids = set()
        self.suppressed_opening_epoch = None
        self._lock = threading.Lock()

    def configure(self):
        config = CommentaryConfig.from_settings(self.settings())
        if config != self.config:
            self.config = config
            self.cancelled.set()
            self.pending_events = {}
            self.fallback_events.clear()
            with self._lock:
                self.inflight.clear()
                self.proposals.clear()
                self.heard.clear()
                self.grounded_ids.clear()
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
            self.suppress_opening("muted")
        if self.config.enabled and not self.muted() and self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True, name="coach-commentary")
            self.thread.start()
        self.wake.set()

    def suppress_opening(self, reason):
        """Explicit Stop/mute never causes an interrupted introduction to retry."""
        snapshot = self.latest or {}
        with self._lock:
            epoch = (snapshot.get("match_id"), snapshot.get("game_number"))
            changed = self.suppressed_opening_epoch != epoch
            self.suppressed_opening_epoch = epoch
            for opening in self.openings.values():
                opening["blocked"] = True
        if changed:
            logger.info("Commentary opening suppressed reason=%s", reason)

    def delivery(self, uid, state, reason=""):
        if state == "started":
            with self._lock:
                item = self.proposals.get(uid)
                if item and uid in self.grounded_ids:
                    self.last_grounded = (item.epoch, time.monotonic())
                if item:
                    logger.info("Commentary delivery uid=%s role=%s state=started", uid, item.role)
        if state in TERMINAL_STATES:
            with self._lock:
                self.inflight.pop(uid, None)
                item = self.proposals.pop(uid, None)
                if item and state == "finished":
                    self.heard.append(item.text)
                    if uid in self.grounded_ids:
                        self.last_grounded = (item.epoch, time.monotonic())
                self.grounded_ids.discard(uid)
                if item:
                    for eid in item.evidence_ids:
                        opening = self.openings.get(eid)
                        if opening:
                            if state == "finished":
                                opening["finished"].add(item.role)
                            elif state == "muted" or reason in {
                                "user_stop",
                                "mode_changed",
                                "shutdown",
                                "epoch_changed",
                            }:
                                opening["blocked"] = True
                            opening["retry_at"] = time.monotonic() + 2
                    logger.info(
                        "Commentary delivery uid=%s role=%s state=%s reason=%s evidence=%s",
                        uid,
                        item.role,
                        state,
                        reason or "unspecified",
                        list(item.evidence_ids),
                    )
            try:
                self.deliveries.put_nowait((uid, state))
            except queue.Full:
                self.cancelled.set()
            self.wake.set()

    def _opening_available(self, event, epoch, now):
        if event.kind == "game_start" and (
            self.openings
            or (self.latest or {}).get("commentary_deck_overview")
            or self.suppressed_opening_epoch == (epoch.match_id, epoch.game_number)
        ):
            return False  # Don't follow an introduction (or Stop) with boilerplate.
        if event.kind != "deck_overview":
            return True
        if self.suppressed_opening_epoch == (epoch.match_id, epoch.game_number):
            return False
        item = self.openings.get(event.event_id)
        return not item or (
            not item["blocked"]
            and item["attempts"] < 3
            and not item["roles"].issubset(item["finished"])
            and now >= item["retry_at"]
        )

    def status(self, state, **details):
        signature = (state, details.get("reason"))
        if state in {"analysis_empty", "analysis_deferred", "analysis_backoff"}:
            key = (state, details.get("reason"), details.get("worker_state"))
            now = time.monotonic()
            if now - self.analysis_diagnostics.get(key, -float("inf")) < 10:
                return
            self.analysis_diagnostics = {k: t for k, t in self.analysis_diagnostics.items() if now - t < 60}
            self.analysis_diagnostics[key] = now
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
            if fresh.epoch != expected.epoch:
                raise BudgetExceeded("facts_changed")
        user = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        from .context import generation_format

        response_format = {"type": "json_object"} if audit else generation_format()
        output, timeout = self.budget.before_call(
            exchange,
            system,
            user,
            audit=audit,
            schema=json.dumps(response_format, separators=(",", ":")),
        )
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
                response_format=response_format,
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
        opening = any(e.kind == "deck_overview" for e in observation.events)
        if not opening and self.last_grounded[0] == observation.epoch and now - self.last_grounded[1] < 10:
            self.status("waiting_capacity", reason="grounded_call_spacing")
            return
        available = tuple(
            e
            for e in observation.events
            if e.event_id not in self.fallback_events and self._opening_available(e, observation.epoch, now)
        )
        # A stale low-value candidate must not poison every other event in the burst.
        lines = []
        while available:
            candidates = deterministic(replace(observation, events=available), config)
            if not candidates:
                break
            if now < candidates[0].expires_at and valid_for(candidates[0], fresh):
                lines = [u for u in candidates if now < u.expires_at and valid_for(u, fresh)]
                break
            rejected = {eid for u in candidates for eid in u.evidence_ids}
            self.status("dropped", reason="facts_changed")
            available = tuple(e for e in available if e.event_id not in rejected)
        if not lines:
            self.status("waiting_events", reason="no_eligible_public_event")
            return
        with self._lock:
            overview = next((e for e in available if e.kind == "deck_overview"), None)
            if overview and overview.event_id in lines[0].evidence_ids:
                opening = self.openings.setdefault(
                    overview.event_id,
                    {
                        "attempts": 0,
                        "finished": set(),
                        "blocked": False,
                        "roles": {u.role for u in lines},
                        "retry_at": 0,
                    },
                )
                opening["attempts"] += 1
                suffix = f":retry{opening['attempts']}" if opening["attempts"] > 1 else ""
                lines = [u for u in lines if u.role not in opening["finished"]]
                ids = {u.utterance_id: u.utterance_id + suffix for u in lines}
                lines = [
                    replace(u, utterance_id=ids[u.utterance_id], anchor_id=ids.get(u.anchor_id))
                    for u in lines
                ]
            for item in lines:
                self.inflight[item.utterance_id] = item.expires_at + 30
                self.proposals[item.utterance_id] = item
                self.grounded_ids.add(item.utterance_id)
        for item in lines:
            self.emit({"type": "commentary_proposal", "utterance": item.to_wire()})
        self.status("grounded", reason=reason)
        return {eid for item in lines for eid in item.evidence_ids}

    def _fallback(self, observation, config, reason):
        consumed = self._grounded(observation, config, reason)
        if consumed:
            # An opening is consumed only by successful delivery of every role.
            consumed -= {e.event_id for e in observation.events if e.kind == "deck_overview"}
            self.fallback_events.update(consumed)
            if not config.generative or any(e.kind == "deck_overview" for e in observation.events):
                self.pending_events = {
                    key: event for key, event in self.pending_events.items() if key not in consumed
                }

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
                # Keep a bounded fresh candidate while speech or gameplay owns
                # capacity; observing the same snapshot must not erase it.
                now = time.monotonic()
                if self.pending_epoch != observation.epoch:
                    self.pending_events = {}
                    self.pending_epoch = observation.epoch
                    self.fallback_events = set()
                    self.heard.clear()
                    self.proposals.clear()
                    self.openings.clear()
                    self.grounded_ids.clear()
                for event in observation.events:
                    if event.kind == "hand_changed":
                        self.pending_events = {
                            key: old for key, old in self.pending_events.items() if old.kind != "hand_changed"
                        }
                    self.pending_events[event.event_id] = event
                self.pending_events = {
                    key: event
                    for key, event in (
                        (e.event_id, e)
                        for e in select_events(self.pending_events.values(), observation, limit=32)
                    )
                    if now - event.received_at < event_lifetime(event.kind)
                    and (
                        event.kind != "hand_changed"
                        or (config.private_information and observation.private_state.get("hand"))
                    )
                }
                self.fallback_events.intersection_update(self.pending_events)
                from .rationale import attach_rationales

                candidates = attach_rationales(
                    self.pending_events.values(), snapshot, config.private_information, now
                )
                observation = replace(
                    observation,
                    events=select_events(
                        (e for e in candidates if self._opening_available(e, observation.epoch, now)),
                        observation,
                    ),
                    recent_commentary=tuple(self.heard),
                )
                # Expire missing delivery acknowledgments; no unbounded speech backlog.
                with self._lock:
                    self.inflight = {uid: expiry for uid, expiry in self.inflight.items() if expiry > now}
                    self.proposals = {
                        uid: item for uid, item in self.proposals.items() if uid in self.inflight
                    }
                    speaking = bool(self.inflight)
                if not observation.events or speaking:
                    self.status(
                        "waiting_delivery" if speaking else "waiting_events",
                        reason="speech_inflight" if speaking else "no_fresh_public_event",
                    )
                    continue
                self.cancelled.clear()
                exchange = None
                usable = False
                try:
                    if any(e.kind == "deck_overview" for e in observation.events):
                        self._fallback(observation, config, "opening_deck_overview")
                        continue
                    if any(
                        e.payload.get("coach_intent")
                        for e in observation.events
                        if e.event_id not in self.fallback_events
                    ):
                        self._fallback(observation, config, "confirmed_action_explanation")
                        continue
                    # Worker setup is not inference occupancy; no model loads here.
                    from arenamcp.backends.proxy import _BACKGROUND_LANE

                    if config.generative and not _BACKGROUND_LANE.commentary_available():
                        self._fallback(observation, config, "analysis_waits_for_gameplay")
                        continue
                    if config.generative and now < self.next_analysis_at:
                        self._fallback(observation, config, "analysis_retry_spacing")
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
                    ):
                        self.status("dropped", reason="facts_or_mode_changed")
                        self.client.close()
                        self._fallback(observation, config, "analysis_facts_changed")
                        continue
                    utterances = [Utterance.from_wire(value) for value in result.get("utterances", [])[:2]]
                    if any(
                        u.epoch != observation.epoch
                        or u.snapshot_id != observation.snapshot_id
                        or (
                            u.position_id
                            and u.position_id != position_fingerprint(observation, u.position_scope)
                        )
                        for u in utterances
                    ):
                        raise ValueError("invalid worker provenance")
                    if not utterances:
                        self.status(
                            "analysis_empty",
                            worker_state=result.get("status", {}).get("state"),
                            reason=result.get("status", {}).get("reason")
                            or result.get("status", {}).get("state", "empty"),
                        )
                        self._fallback(
                            observation,
                            config,
                            result.get("status", {}).get("reason")
                            or result.get("status", {}).get("state", "analysis_unavailable"),
                        )
                        continue
                    self.pending_events = {
                        key: event
                        for key, event in self.pending_events.items()
                        if key not in {e.event_id for e in observation.events}
                    }
                    prefix_valid = True
                    for item in utterances:
                        prefix_valid = (
                            prefix_valid and time.monotonic() < item.expires_at and valid_for(item, fresh)
                        )
                        if not prefix_valid:
                            self.status(
                                "not_delivered", reason="expired_or_changed", utterance_id=item.utterance_id
                            )
                            self.client.delivered(item.utterance_id, "superseded")
                            continue
                        with self._lock:
                            self.inflight[item.utterance_id] = item.expires_at + 30
                            self.proposals[item.utterance_id] = item
                        self.emit({"type": "commentary_proposal", "utterance": item.to_wire()})
                        usable = True
                    self.status(
                        result.get("status", {}).get("state", "ready"),
                        observation_age=round(time.monotonic() - observation.arrived_at, 3),
                        calls=exchange.calls,
                        input_tokens_bound=exchange.input_tokens,
                        output_tokens_limit=exchange.output_tokens,
                        request_seconds=round(exchange.request_seconds, 3),
                    )
                except BudgetExceeded as error:
                    self.status("analysis_deferred", reason=str(error))
                    self._fallback(observation, config, str(error))
                except Exception as error:
                    self.status("unavailable", reason=type(error).__name__)
                    self.client.close()
                    self._fallback(observation, config, "analysis_unavailable")
                finally:
                    if exchange:
                        self.budget.finish(exchange, usable=usable)
                        if not usable:
                            self.next_analysis_at = time.monotonic() + 5
        finally:
            self.client.close()

    def close(self):
        self.closed.set()
        self.cancelled.set()
        self.wake.set()
        # Only the background owner terminates its worker; gameplay never joins it.
