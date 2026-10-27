"""Bounded semantic Arena input history for reports made after manual recovery.

Only game protocol fields are retained. This does not intercept input, retain
authentication envelopes, or infer that an unrecognised client action was human.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import Any

_FIELDS = frozenset(
    [
        "type",
        "idType",
        "listType",
        "staticList",
        "context",
        "optionContext",
        "validationType",
        "minSel",
        "maxSel",
        "sourceId",
        "ids",
        "unfilteredIds",
        "options",
        "optionIds",
        "selectedIds",
        "targets",
        "targetIds",
        "targetInstanceId",
        "instanceId",
        "selectedAttackers",
        "selectedBlockers",
        "attackerInstanceId",
        "blockerInstanceId",
        "selectedDamageRecipient",
        "damageRecipient",
        "playerSystemSeatId",
        "planeswalkerInstanceId",
        "actions",
        "actionType",
        "abilityGrpId",
        "alternativeGrpId",
        "prompt",
        "promptId",
        "parameters",
        "parameterName",
        "numberValue",
        "optionIndex",
        "choice",
        "mulliganCount",
        "selectedOption",
        "result",
        "xValue",
        "value",
        "amount",
        "cancel",
        "done",
        "autoPass",
        "action_index",
        "auto_pass",
        "expected_instance_id",
        "expected_grp_id",
        "expected_game_state_id",
        "expected_msg_id",
        "attackers",
        "blockers",
        "target_ids",
        "instance_id",
        "selected_ids",
        "option_index",
        "finalize_only",
        "selection_id_type",
    ]
)
_ENUM = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,95}$")
_ENUM_FIELDS = frozenset(
    [
        "type",
        "idType",
        "listType",
        "staticList",
        "context",
        "optionContext",
        "validationType",
        "parameterName",
        "actionType",
        "selection_id_type",
        "result",
        "choice",
    ]
)


def _safe(value: Any, depth: int = 0, budget: list[int] | None = None, field: str = "") -> Any:
    """Allowlisted, depth/length bounded protocol data; never arbitrary strings."""
    if budget is None:
        budget = [256]
    budget[0] -= 1
    if depth > 6 or budget[0] < 0:
        return None
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, str):
        return value if field in _ENUM_FIELDS and _ENUM.fullmatch(value) else None
    if isinstance(value, list):
        return [_safe(item, depth + 1, budget, field) for item in value[:40] if budget[0] > 0]
    if isinstance(value, dict):
        return {
            key: _safe(item, depth + 1, budget, key)
            for key, item in value.items()
            if key in _FIELDS and budget[0] > 0
        }
    return None


def _number(value: Any) -> int | None:
    return value if type(value) is int else None


class RecoveryTrace:
    """Keep recent requests/responses and a few rejected-input incidents.

    Arena IllegalRequest messages identify rejected inputs. Repeated requests
    alone are not classified as failures. Request matching uses both
    msgId/respId and gameStateId, never merely the currently displayed prompt.
    """

    def __init__(
        self,
        *,
        max_events: int = 80,
        max_bytes: int = 96 * 1024,
        max_age_s: float = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_events, self.max_bytes, self.max_age_s = max_events, max_bytes, max_age_s
        self._clock = clock
        self._lock = threading.RLock()
        self._events: deque = deque()
        self._requests: OrderedDict = OrderedDict()
        self._incidents: deque = deque(maxlen=6)
        self._seq = 0
        self._match_id: str | None = None
        self._game_number: int | None = None

    def _prune(self) -> None:
        cutoff = self._clock() - self.max_age_s
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        for key, (at, _row) in list(self._requests.items()):
            if at < cutoff:
                del self._requests[key]
        while self._incidents and self._incidents[0][0] < self._clock() - 1800:
            self._incidents.popleft()

    def _append(self, kind: str, **details: Any) -> dict:
        self._prune()
        self._seq += 1
        row = {
            "sequence": self._seq,
            "at": round(time.time(), 3),
            "kind": kind,
            "match_id": self._match_id,
            "game_number": self._game_number,
            **details,
        }
        self._events.append((self._clock(), row))
        while len(self._events) > self.max_events:
            self._events.popleft()
        # Individual protocol arrays are capped too; no one request can grow
        # the in-memory history or the saved diagnostic without limit.
        while self._events and len(json.dumps([row for _, row in self._events])) > self.max_bytes // 2:
            self._events.popleft()
        return row

    @staticmethod
    def _identity(request: dict) -> tuple:
        details = request.get("details") or {}
        prompt = request.get("prompt") or {}
        return (
            request.get("match_id"),
            request.get("game_number"),
            request.get("type"),
            details.get("sourceId"),
            details.get("staticList"),
            prompt.get("promptId"),
        )

    def _request_for(self, response_to: int | None, game_state_id: int | None) -> tuple[dict | None, str]:
        entry = self._requests.get(response_to)
        if not entry:
            return None, "missing_request"
        request = entry[1]
        if game_state_id is None or request.get("game_state_id") is None:
            return None, "missing_state_id"
        if request.get("game_state_id") != game_state_id:
            return None, "state_mismatch"
        return copy.deepcopy(request), "exact"

    def _nearby_bridge(self, details: dict) -> list[dict]:
        ids = details.get("ids")
        return [
            row
            for at, row in self._events
            if self._clock() - at <= 10
            and row.get("kind") == "bridge_attempt"
            and (not ids or (row.get("arguments") or {}).get("ids") == ids)
        ][-3:]

    def _observe_rejection(self, message: dict) -> None:
        rejection = message.get("illegalRequestMessage") or {}
        invalid = rejection.get("invalidMessage") or {}
        kind = str(invalid.get("type") or "").removeprefix("ClientMessageType_")
        if not _ENUM.fullmatch(kind):
            return
        response_to, gsid = _number(invalid.get("respId")), _number(invalid.get("gameStateId"))
        request, match = self._request_for(response_to, gsid)
        details = _safe(invalid.get(kind[:1].lower() + kind[1:]) or {})
        reason = str(rejection.get("reason") or "")
        row = self._append(
            "illegal_request",
            failure_reason=reason if _ENUM.fullmatch(reason) else "unknown",
            response_type=kind,
            response_to=response_to,
            game_state_id=gsid,
            details=details,
            request=request,
            request_match=match,
        )
        identity = self._identity(request or {})
        for _, incident_identity, incident in self._incidents:
            if incident_identity == identity and response_to in incident.get("request_chain", []):
                incident["awaiting_reissue_after"] = response_to
        self._incidents.append(
            (
                self._clock(),
                identity,
                {
                    "reason": "arena_rejected_response",
                    "match_id": self._match_id,
                    "game_number": self._game_number,
                    "request": request,
                    "rejection": row,
                    "nearby_bridge_attempts": self._nearby_bridge(details),
                    "follow_up_responses": [],
                    "request_chain": [response_to] if request else [],
                    "awaiting_reissue_after": response_to if request else None,
                },
            )
        )

    def observe_gre(self, payload: dict, state: dict | None = None) -> None:
        if not isinstance(payload, dict):
            return
        state = state or {}
        with self._lock:
            match_id = state.get("match_id")
            game_number = _number(state.get("game_number"))
            if match_id and match_id != self._match_id:
                self._game_number = None
            changed_game = (
                game_number is not None and self._game_number is not None and game_number != self._game_number
            )
            if match_id and (match_id != self._match_id or changed_game):
                if self._match_id is not None:
                    self._events.clear()
                    self._requests.clear()
                self._match_id = str(match_id)[:100]
            if game_number is not None:
                self._game_number = game_number
            self._prune()
            messages = (payload.get("greToClientEvent") or {}).get("greToClientMessages") or []
            for message in messages[:100]:
                if not isinstance(message, dict):
                    continue
                kind = str(message.get("type") or "").removeprefix("GREMessageType_")
                if kind == "IllegalRequest":
                    self._observe_rejection(message)
                    continue
                if not kind.endswith("Req") or not _ENUM.fullmatch(kind):
                    continue
                request_id = _number(message.get("msgId"))
                if request_id is None:
                    continue
                field = kind[:1].lower() + kind[1:]
                details = _safe(message.get(field) or {})
                request = {
                    "type": kind,
                    "request_id": request_id,
                    "game_state_id": _number(message.get("gameStateId")),
                    "match_id": self._match_id,
                    "game_number": self._game_number,
                    "details": details,
                    "prompt": _safe(message.get("prompt") or {}),
                }
                if request_id in self._requests and self._requests[request_id][1] == request:
                    continue
                identity = self._identity(request)
                for at, incident_identity, incident in self._incidents:
                    if (
                        incident_identity == identity
                        and self._clock() - at <= 60
                        and incident.get("awaiting_reissue_after") is not None
                        and request_id != incident["awaiting_reissue_after"]
                    ):
                        incident["request_chain"] = (incident.get("request_chain", []) + [request_id])[-8:]
                        incident["awaiting_reissue_after"] = None
                self._requests[request_id] = (self._clock(), request)
                self._requests.move_to_end(request_id)
                while len(self._requests) > 24:
                    self._requests.popitem(last=False)
                self._append("request", **request)

    def observe_client(self, payload: dict) -> None:
        if (
            not isinstance(payload, dict)
            or payload.get("clientToMatchServiceMessageType")
            != "ClientToMatchServiceMessageType_ClientToGREMessage"
        ):
            return
        message = payload.get("payload")
        if isinstance(message, str):
            if len(message) > 256 * 1024:
                return
            try:
                message = json.loads(message)
            except ValueError:
                return
        if not isinstance(message, dict):
            return
        kind = str(message.get("type") or "").removeprefix("ClientMessageType_")
        if not _ENUM.fullmatch(kind) or not kind.endswith(("Resp", "Req")):
            return
        with self._lock:
            self._prune()
            response_to = _number(message.get("respId"))
            game_state_id = _number(message.get("gameStateId"))
            request, match = self._request_for(response_to, game_state_id)
            field = kind[:1].lower() + kind[1:]
            details = _safe(message.get(field) or {})
            domain = ((request or {}).get("details") or {}).get("idType")
            if ((request or {}).get("details") or {}).get("staticList") == "StaticList_CardNames":
                domain = "TitleId"
            selections = []
            if kind == "SelectNResp":
                for value in details.get("ids") or []:
                    selection = {"id": value, "expected_id_domain": domain or "unknown"}
                    if domain == "TitleId" and type(value) is int:
                        try:
                            from arenamcp.card_name_choice import card_title_name

                            name = card_title_name(value)
                            if name:
                                selection["card_name"] = name[:120]
                        except (ImportError, AttributeError):
                            pass
                    selections.append(selection)
            row = self._append(
                "client_response",
                type=kind,
                response_to=response_to,
                game_state_id=game_state_id,
                details=details,
                selections=selections,
                request_match=match,
                request=request,
                origin="unknown_client_input",
            )
            if request:
                identity = self._identity(request)
                # Preserve a small incident even when ordinary pass/action
                # traffic has pushed its failed attempt out of the recent tail.
                for _, incident_identity, incident in self._incidents:
                    if incident_identity == identity and response_to in incident.get("request_chain", []):
                        incident["follow_up_responses"] = (incident["follow_up_responses"] + [row])[-4:]

    def observe_bridge(self, command: dict, outcome: dict | None, error: str | None = None) -> None:
        action = str(command.get("action") or "")
        if not _ENUM.fullmatch(action):
            return
        with self._lock:
            # No arbitrary exception/error strings: they may contain secrets.
            row = self._append(
                "bridge_attempt",
                action=action,
                arguments=_safe(command),
                ok=outcome.get("ok")
                if isinstance(outcome, dict) and type(outcome.get("ok")) is bool
                else None,
                error_present=bool(error or (isinstance(outcome, dict) and outcome.get("error"))),
            )
            if (row["error_present"] or row["ok"] is False) and action.startswith(
                ("submit", "select", "choose", "cancel", "confirm", "click", "pass", "pay")
            ):
                request_id = command.get("expected_msg_id")
                candidate = self._requests.get(request_id)
                request_match = "expected_id_only" if candidate else "latest_observed_only"
                if candidate is None and self._requests:
                    candidate = next(reversed(self._requests.values()))
                request = copy.deepcopy(candidate[1]) if candidate else None
                self._incidents.append(
                    (
                        self._clock(),
                        self._identity(request or {}),
                        {
                            "reason": "bridge_command_failed",
                            "match_id": self._match_id,
                            "game_number": self._game_number,
                            "request": request,
                            "request_match": request_match,
                            "bridge_attempt": row,
                            "follow_up_responses": [],
                            "request_chain": [request["request_id"]] if request else [],
                        },
                    )
                )

    def snapshot(self) -> dict:
        with self._lock:
            self._prune()
            result = {
                "source": "Arena requests, outgoing ClientToGRE responses, and bridge attempts",
                "origin_note": "Client input origin is unknown; bridge attempts are recorded separately. No mouse or keyboard capture.",
                "window_seconds": self.max_age_s,
                "match_id": self._match_id,
                "incident_window_seconds": 1800,
                "game_number": self._game_number,
                "events": [row for _, row in self._events],
                "incidents": [incident for _, _, incident in self._incidents],
            }
            result = copy.deepcopy(result)
            while len(json.dumps(result)) > self.max_bytes:
                if result["events"]:
                    result["events"].pop(0)
                elif result["incidents"]:
                    result["incidents"].pop(0)
                else:
                    break
            return result


recovery_trace = RecoveryTrace()


def install_recovery_trace(
    parser: Any, state: Callable[[], dict], *, trace: RecoveryTrace | None = None
) -> None:
    """Use the same existing log stream for incoming and outgoing game messages."""
    target = trace if trace is not None else recovery_trace
    parser.register_handler("GreToClientEvent", lambda payload: target.observe_gre(payload, state()))
    parser.register_handler("ClientToMatchServiceMessage", target.observe_client)
