"""BepInEx-plugin protocol for the native macOS IL2CPP bridge.

The Windows client runs the C# plugin (bepinex-plugin/MtgaCoachBridge), which
answers the GRE bridge commands. The native Mac client is IL2CPP, so it gets a
small injected library (spikes/mac-il2cpp/probe.cpp) that only offers generic
main-thread reflection: ``reflect_batch`` runs a list of ops (pending, get, call,
new, set, expect, expect_pending) in one Unity frame. This adapter rebuilds the
plugin's commands on those ops and returns the plugin's response shapes, so
GREBridge and the autopilot run unchanged. Each handler mirrors its
counterpart in Plugin.Actions.cs; MTGA-specific logic stays in Python, where it
can be fixed without restarting the game.

Observation stays log-first (docs/DECISIONS.md, 2026-07-16 and 2026-09-23):
``get_game_state`` is unsupported here, so the coach keeps its Player.log view
of the board instead of a bridge overlay.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

SendFn = Callable[[dict[str, Any], "float | None"], dict[str, Any]]

MESSAGING = "Wotc.Mtgo.Gre.External.Messaging."
# Request fields never needed for the protocol and bulky or recursive to dump.
SNAPSHOT_SKIP = ["OriginalMessage", "ParentRequest", "_outboundMessage", "OnSubmit", "OnRequestSubmit"]
SNAPSHOT_DEPTH = 7
# Speculative getters read in the snapshot batch; absent members come back null.
SNAPSHOT_GETTERS = [
    "Type", "CanCancel", "AllowUndo", "CanPass", "CanSubmit",
    "IsInstanceIdSelection", "IsZoneSelection", "IsManaColorSelection", "IsCardColorSelection",
    "IsCounterSelection", "IsBasicLandSelection", "IsTriggeredAbilitySelection", "IsStackingDecision",
    "IsMultiZoneSearch", "IsOptional",
]
MAX_NUMERIC_INPUT_ENTRIES = 20
TARGET_COMMIT_TIMEOUT_S = 1.2
TARGET_GONE_GRACE_S = 0.6

# Commands answered by the injected library directly.
PASSTHROUGH = {"ping", "reflect_batch"}

DECISION_TYPES = {
    "SelectTargetsReq": "target_selection", "SearchReq": "search", "DistributionReq": "distribution",
    "NumericInputReq": "numeric_input", "SelectNReq": "select_n", "GroupReq": "group_selection",
    "GroupOptionReq": "modal_choice", "DeclareAttackersReq": "declare_attackers",
    "DeclareBlockersReq": "declare_blockers", "PayCostsReq": "pay_costs",
    "ChooseStartingPlayerReq": "choose_starting_player", "SelectReplacementReq": "select_replacement",
    "SelectNGroupReq": "select_n_group", "SelectFromGroupsReq": "select_from_groups",
    "SearchFromGroupsReq": "search_from_groups", "SelectCountersReq": "select_counters",
    "OrderReq": "order_triggers", "GatherReq": "gather",
}
INTERESTING_NAME_KEYWORDS = (
    "prompt", "help", "message", "label", "text", "context", "target", "option", "choice", "select",
    "group", "search", "source", "zone", "attack", "block", "counter", "id", "count", "min", "max",
    "amount", "total", "value",
)


class AdapterError(Exception):
    """A reflection batch failed; the message is reported as the command error."""


# ---------------------------------------------------------------------------
# Snapshot nodes: {"$c": class, "$h": handle, field: value, ...}; lists carry
# "$items"; enums are {"e": name, "v": number}.
# ---------------------------------------------------------------------------


def field(node: Any, name: str, default: Any = None) -> Any:
    """Read a member by its C# name, whatever backing-field form the dump used."""
    if not isinstance(node, dict):
        return default
    camel = name[:1].lower() + name[1:]
    for key in (name, camel + "_", "_" + camel, camel, "m_" + name):
        if key in node:
            return node[key]
    return default


def items(node: Any) -> list[Any]:
    value = node.get("$items") if isinstance(node, dict) else None
    return value if isinstance(value, list) else []


def enum_name(value: Any) -> str:
    if isinstance(value, dict) and "e" in value:
        return str(value["e"])
    return "" if value is None else str(value)


def num(value: Any, default: int = 0) -> int:
    if isinstance(value, dict) and "v" in value:
        value = value["v"]
    if isinstance(value, bool):
        return int(value)
    return int(value) if isinstance(value, (int, float)) else default


def handle(node: Any) -> int | None:
    return node.get("$h") if isinstance(node, dict) else None


def short_class(node: Any) -> str:
    full = node.get("$c", "") if isinstance(node, dict) else ""
    return full.split("<", 1)[0].rsplit(".", 1)[-1]


def camel_case(name: str) -> str:
    name = name.strip("_")
    return name[:1].lower() + name[1:] if name[:1].isupper() else name


# Argument encodings understood by the injected library.
def U(value: int) -> dict:
    return {"uint": int(value)}


def I(value: int) -> dict:  # noqa: E743 - mirrors the other one-letter encoders
    return {"int": int(value)}


def B(value: bool) -> dict:
    return {"bool": bool(value)}


def E(name: str) -> dict:
    return {"enum": name}


def H(handle_id: int) -> dict:
    return {"h": int(handle_id)}


def REF(index: int) -> dict:
    return {"ref": index}


def LIST(values: list[dict]) -> dict:
    return {"list": list(values)}


class _Ops:
    """Builds a reflect batch whose ops can refer to earlier results by index."""

    def __init__(self) -> None:
        self.ops: list[dict[str, Any]] = []

    def add(self, op: str, **fields: Any) -> dict:
        self.ops.append({"op": op, **fields})
        return REF(len(self.ops) - 1)

    def pending(self, depth: int = 0) -> dict:
        return self.add("pending", depth=depth, skip=SNAPSHOT_SKIP)

    def get(self, target: dict, member: str, depth: int = 0, optional: bool = False) -> dict:
        return self.add("get", target=target, member=member, depth=depth, optional=optional)

    def call(self, target: dict, method: str, *args: dict, depth: int = 0) -> dict:
        return self.add("call", target=target, method=method, args=list(args), depth=depth)

    def new(self, class_name: str, *args: dict) -> dict:
        return self.add("new", **{"class": class_name}, args=list(args), depth=0)

    def set(self, target: dict, member: str, value: dict) -> dict:
        return self.add("set", target=target, member=member, value=value)

    def expect_class(self, target: dict, class_name: str) -> dict:
        return self.add("expect", target=target, **{"class": class_name})

    def expect_member(self, target: dict, member: str, equals: Any) -> dict:
        return self.add("expect", target=target, member=member, equals=equals)

    def expect_pending(self, target: dict) -> dict:
        return self.add("expect_pending", target=target)

    def message(self, message_type: str, game_state_id: int, resp_id: int) -> dict:
        """A fresh ClientToGREMessage, as the plugin builds for direct OnSubmit calls."""
        message = self.new(MESSAGING + "ClientToGREMessage")
        self.set(message, "Type", E(message_type))
        self.set(message, "GameStateId", U(game_state_id))
        self.set(message, "RespId", U(resp_id))
        return message

    def on_submit(self, request: dict, message: dict) -> None:
        delegate = self.get(request, "OnSubmit")
        self.call(delegate, "Invoke", message)


class Snapshot:
    """The pending request as dumped in one frame, plus identity and getters."""

    def __init__(self, results: list[Any]) -> None:
        request = results[0]
        self.request: dict | None = request if isinstance(request, dict) and "$none" not in request else None
        self.reason = request.get("$none", "") if isinstance(request, dict) else ""
        original = results[1] if len(results) > 1 else None
        self.game_state_id = num(field(original, "GameStateId")) if isinstance(original, dict) else None
        self.msg_id = num(field(original, "MsgId")) if isinstance(original, dict) else None
        self.getters = dict(zip(SNAPSHOT_GETTERS, results[2:], strict=False))

    @property
    def request_class(self) -> str:
        return short_class(self.request) if self.request else ""

    @property
    def handle(self) -> int | None:
        return handle(self.request)


class MacBridgeAdapter:
    # Also drives the Android build of the same probe ("il2cpp-android"): the
    # reflect-1 protocol and the game's classes are identical on both.
    def __init__(self, send: SendFn, runtime: str = "il2cpp-macos") -> None:
        self._send = send
        self._runtime = runtime

    # -- transport ---------------------------------------------------------

    def _run(self, ops: _Ops, timeout: float | None = None) -> list[Any]:
        response = self._send({"action": "reflect_batch", "ops": ops.ops}, timeout)
        if not response.get("ok"):
            raise AdapterError(response.get("error") or "reflect_batch failed")
        return response.get("results") or []

    def snapshot(self, depth: int = SNAPSHOT_DEPTH, timeout: float | None = None) -> Snapshot:
        ops = _Ops()
        request = ops.pending(depth)
        ops.get(request, "OriginalMessage", depth=1, optional=True)
        for name in SNAPSHOT_GETTERS:
            ops.get(request, name, depth=1, optional=True)
        return Snapshot(self._run(ops, timeout))

    def _submit(self, snapshot: Snapshot, build: Callable[[_Ops, dict], None], timeout: float | None) -> list[Any]:
        """Run `build`'s ops only if the snapshot's request is still the pending one."""
        ops = _Ops()
        request = ops.pending(0)
        ops.expect_pending(H(snapshot.handle))
        build(ops, request)
        return self._run(ops, timeout)

    def handles(self, command: dict[str, Any]) -> bool:
        return command.get("action") not in PASSTHROUGH

    def handle(self, command: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        action = str(command.get("action") or "")
        handler = getattr(self, "_cmd_" + action, None)
        if handler is None:
            return {"ok": False, "unsupported": True, "error": f"not supported by the macOS IL2CPP bridge: {action}"}
        try:
            return handler(command, timeout)
        except AdapterError as exc:
            logger.info("mac bridge %s failed: %s", action, exc)
            return {"ok": False, "error": str(exc)}

    def _require(self, timeout: float | None, *classes: str) -> Snapshot:
        snapshot = self.snapshot(timeout=timeout)
        if snapshot.request is None:
            raise AdapterError("No pending interaction")
        if classes and snapshot.request_class not in classes:
            raise AdapterError(f"Pending is {snapshot.request_class}, not {'/'.join(classes)}")
        return snapshot

    # -- observation ---------------------------------------------------------

    def _cmd_get_game_state(self, command: dict, timeout: float | None) -> dict:
        return {"ok": False, "unsupported": True, "error": "macOS bridge is log-first; board state comes from Player.log"}

    def _cmd_get_pending_actions(self, command: dict, timeout: float | None) -> dict:
        snapshot = self.snapshot(timeout=timeout)
        if snapshot.request is None:
            return {"ok": True, "has_pending": False, "request_type": None}
        request, getters, name = snapshot.request, snapshot.getters, snapshot.request_class
        response: dict[str, Any] = {
            "ok": True,
            "has_pending": True,
            "request_type": enum_name(getters.get("Type")) or name,
            "request_class": name,
            "can_cancel": bool(getters.get("CanCancel")),
            "allow_undo": bool(getters.get("AllowUndo")),
            "bridge_runtime": self._runtime,
        }
        if snapshot.game_state_id is not None:
            response["game_state_id"] = snapshot.game_state_id
            response["msg_id"] = snapshot.msg_id
        shaper = getattr(self, "_shape_" + name, None)
        if shaper:
            shaper(request, getters, response)
        payload = build_request_payload(request, response["request_type"], name)
        if payload:
            response["request_payload"] = payload
        if "decision_context" not in response:
            context = build_decision_context(response["request_type"], name, payload)
            if context:
                response["decision_context"] = context
        return response

    def _shape_ActionsAvailableRequest(self, request: dict, getters: dict, response: dict) -> None:
        response["actions"] = [serialize_action(action) for action in items(field(request, "Actions"))]
        response["can_pass"] = bool(getters.get("CanPass"))

    def _shape_DeclareBlockersRequest(self, request: dict, getters: dict, response: dict) -> None:
        response["blockers"] = [
            {
                "blockerInstanceId": num(field(blocker, "BlockerInstanceId")),
                "mustBlock": bool(field(blocker, "MustBlock")),
                "minAttackers": num(field(blocker, "MinAttackers")),
                "maxAttackers": num(field(blocker, "MaxAttackers")),
                "attackerInstanceIds": [num(i) for i in items(field(blocker, "AttackerInstanceIds"))],
            }
            for blocker in items(field(request, "AllBlockers"))
        ]
        response["can_pass"] = False

    def _shape_DeclareAttackerRequest(self, request: dict, getters: dict, response: dict) -> None:
        attackers = []
        for attacker in items(field(request, "QualifiedAttackers")):
            recipients = []
            for recipient in items(field(attacker, "LegalDamageRecipients")):
                entry = {"type": enum_name(field(recipient, "Type"))}
                for key, member in (
                    ("playerSystemSeatId", "PlayerSystemSeatId"),
                    ("planeswalkerInstanceId", "PlaneswalkerInstanceId"),
                    ("teamId", "TeamId"),
                ):
                    value = recipient_id(recipient, member)
                    if value is not None:
                        entry[key] = value
                recipients.append(entry)
            attackers.append(
                {
                    "attackerInstanceId": num(field(attacker, "AttackerInstanceId")),
                    "mustAttack": bool(field(attacker, "MustAttack")),
                    "legalDamageRecipients": recipients,
                }
            )
        response["attackers"] = attackers
        response["can_submit"] = bool(getters.get("CanSubmit"))
        response["can_pass"] = False

    def _shape_CastingTimeOptionRequest(self, request: dict, getters: dict, response: dict) -> None:
        entries = casting_time_entries(request)
        response["actions"] = [entry["payload"] for entry in entries]
        response["decision_context"] = {
            "type": "casting_time_options",
            "num_options": len(entries),
            "options": [entry["payload"] for entry in entries],
        }
        response["can_pass"] = bool(getters.get("CanCancel"))
        for child in items(field(request, "ChildRequests")):
            if short_class(child) != "CastingTimeOption_NumericInputRequest":
                continue
            response["numeric_min"] = num(field(child, "Min"))
            response["numeric_max"] = num(field(child, "Max"))
            response["numeric_input_type"] = enum_name(field(child, "InputType"))
            if num(field(child, "StepSize")) > 0:
                response["numeric_step"] = num(field(child, "StepSize"))
            disallowed = [num(v) for v in items(field(child, "DisallowedValues"))]
            if disallowed:
                response["numeric_disallowed"] = disallowed
            suggested = [num(v) for v in items(field(child, "SuggestedValues"))]
            if suggested:
                response["numeric_suggested"] = suggested
            if field(child, "DisallowEven"):
                response["numeric_disallow_even"] = True
            if field(child, "DisallowOdd"):
                response["numeric_disallow_odd"] = True
            if num(field(child, "GrpId")):
                response["grp_id"] = num(field(child, "GrpId"))
            break

    def _shape_SelectTargetsRequest(self, request: dict, getters: dict, response: dict) -> None:
        selections, flat = [], []
        for selection in items(field(request, "TargetSelections")):
            index = num(field(selection, "TargetIdx"))
            slot = []
            for target in items(field(selection, "Targets")):
                # grpId stays 0: the plugin resolves it from the live board, which
                # the log-first coach already knows by instance id.
                entry = {"targetInstanceId": num(field(target, "TargetInstanceId")), "targetIdx": index, "grpId": 0}
                slot.append(dict(entry))
                flat.append(entry)
            selections.append(
                {
                    "targetIdx": index,
                    "minTargets": num(field(selection, "MinTargets")),
                    "maxTargets": num(field(selection, "MaxTargets")),
                    "selectedTargets": num(field(selection, "SelectedTargets")),
                    "targets": slot,
                }
            )
        response["target_selections"] = selections
        response["target_candidates"] = flat
        response["can_pass"] = False

    def _shape_AssignDamageRequest(self, request: dict, getters: dict, response: dict) -> None:
        response["assigners"] = [
            {
                "instanceId": num(field(assigner, "InstanceId")),
                "totalDamage": num(field(assigner, "TotalDamage")),
                "assignments": [
                    {
                        "instanceId": num(field(a, "InstanceId")),
                        "minDamage": num(field(a, "MinDamage")),
                        "maxDamage": num(field(a, "MaxDamage")),
                        "assignedDamage": num(field(a, "AssignedDamage")),
                    }
                    for a in items(field(assigner, "Assignments"))
                ],
            }
            for assigner in items(field(request, "Assigners"))
        ]
        response["can_pass"] = False

    def _shape_SelectReplacementRequest(self, request: dict, getters: dict, response: dict) -> None:
        response["replacements"] = [
            {"index": index, "display": replacement_display(replacement)}
            for index, replacement in enumerate(items(field(request, "Replacements")))
        ]
        response["is_optional"] = bool(getters.get("IsOptional") or field(request, "IsOptional"))
        response["can_pass"] = False

    def _shape_DistributionRequest(self, request: dict, getters: dict, response: dict) -> None:
        response["distribution_target_ids"] = [num(i) for i in items(field(request, "TargetIds"))]
        response["distribution_legal_ids"] = [num(i) for i in items(field(request, "LegalTargetIds"))]
        for key, member in (("min", "Min"), ("max", "Max"), ("min_per", "MinPer"), ("max_per", "MaxPer")):
            response["distribution_" + key] = num(field(request, member))
        response["can_pass"] = False

    def _shape_SearchRequest(self, request: dict, getters: dict, response: dict) -> None:
        response["search_candidates"] = [num(i) for i in items(field(request, "Options"))]
        response["search_zones"] = [num(i) for i in items(field(request, "ZonesToSearch"))]
        response["search_additional_zones"] = [num(i) for i in items(field(request, "AdditionalZones"))]
        response["search_context_options"] = [num(i) for i in items(field(request, "ContextOptions"))]
        response["search_is_multi_zone"] = bool(getters.get("IsMultiZoneSearch"))
        response["can_pass"] = False

    def _shape_SelectNRequest(self, request: dict, getters: dict, response: dict) -> None:
        response["select_n_ids"] = [num(i) for i in items(field(request, "Ids"))]
        response["select_n_zone_ids"] = [num(i) for i in items(field(request, "ZoneIds"))]
        response["select_n_id_type"] = enum_name(field(request, "IdType"))
        response["select_n_list_type"] = enum_name(field(request, "ListType"))
        response["select_n_context"] = enum_name(field(request, "Context"))
        response["select_n_option_context"] = enum_name(field(request, "OptionContext"))
        response["select_n_min"] = num(field(request, "MinSel"))
        response["select_n_max"] = num(field(request, "MaxSel"))
        should_cancel = bool(field(request, "ShouldCancel"))
        response["select_n_can_cancel"] = should_cancel
        for key, getter in (
            ("instance_id", "IsInstanceIdSelection"), ("zone", "IsZoneSelection"),
            ("mana_color", "IsManaColorSelection"), ("card_color", "IsCardColorSelection"),
            ("counter", "IsCounterSelection"), ("basic_land", "IsBasicLandSelection"),
            ("triggered_ability", "IsTriggeredAbilitySelection"), ("stacking_decision", "IsStackingDecision"),
        ):
            response["select_n_is_" + key] = bool(getters.get(getter))
        response["select_n_should_cancel"] = should_cancel
        response["can_pass"] = False

    def _shape_GroupRequest(self, request: dict, getters: dict, response: dict) -> None:
        # InstanceIds/GroupSpecs/Context are computed properties over the wrapped
        # protobuf GroupReq, so read that message's fields.
        inner = field(request, "GroupRequest") or request
        response["group_instance_ids"] = [num(i) for i in items(field(inner, "InstanceIds"))]
        response["group_specs"] = [plain_object(spec) for spec in items(field(inner, "GroupSpecs"))]
        response["group_context"] = enum_name(field(inner, "Context"))
        response["can_pass"] = False

    def _shape_NumericInputRequest(self, request: dict, getters: dict, response: dict) -> None:
        response["numeric_min"] = num(field(request, "Min"))
        response["numeric_max"] = num(field(request, "Max"))
        response["numeric_input_type"] = enum_name(field(request, "InputType"))
        response["numeric_disallowed"] = [num(v) for v in items(field(request, "DisallowedValues"))]
        response["numeric_suggested"] = [num(v) for v in items(field(request, "SuggestedValues"))]
        response["numeric_disallow_even"] = bool(field(request, "DisallowEven"))
        response["numeric_disallow_odd"] = bool(field(request, "DisallowOdd"))
        response["can_pass"] = False

    # -- submissions -----------------------------------------------------------

    def _cmd_submit_action(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout, "ActionsAvailableRequest", "CastingTimeOptionRequest")
        index = int(command.get("action_index") or 0)
        if snapshot.request_class == "CastingTimeOptionRequest":
            return self._submit_casting_time_option(snapshot, index, command, timeout)
        actions = items(field(snapshot.request, "Actions"))
        if not 0 <= index < len(actions):
            raise AdapterError(f"Action index {index} out of range (0-{len(actions) - 1})")
        action = actions[index]
        info = serialize_action(action)
        # Optional identity guards (ignored by the Windows plugin).
        for key, member in (("expected_instance_id", "instanceId"), ("expected_grp_id", "grpId")):
            if command.get(key) is not None and int(command[key]) != info[member]:
                raise AdapterError(f"identity mismatch: action {index} is {info}")
        if command.get("expected_action_type") and command["expected_action_type"] != info["actionType"]:
            raise AdapterError(f"identity mismatch: action {index} is {info}")
        if command.get("expected_game_state_id") not in (None, -1) and int(
            command["expected_game_state_id"]
        ) != snapshot.game_state_id:
            raise AdapterError(
                f"stale command: request game_state_id {snapshot.game_state_id} != {command['expected_game_state_id']}"
            )

        def build(ops: _Ops, request: dict) -> None:
            ops.call(request, "SubmitAction", H(handle(action)), B(bool(command.get("auto_pass"))))

        self._submit(snapshot, build, timeout)
        logger.info("mac bridge submit_action [%d]: %s", index, info)
        return {
            "ok": True,
            "submitted_type": info["actionType"],
            "submitted_grp_id": info["grpId"],
            "submitted_instance_id": info["instanceId"],
        }

    def _submit_casting_time_option(
        self, snapshot: Snapshot, index: int, command: dict, timeout: float | None
    ) -> dict:
        entries = casting_time_entries(snapshot.request)
        if not 0 <= index < len(entries):
            raise AdapterError(f"Casting-time option index {index} out of range (0-{len(entries) - 1})")
        entry = entries[index]
        payload = entry["payload"]
        if command.get("expected_choice_kind") and command["expected_choice_kind"] != payload["choiceKind"]:
            raise AdapterError(f"identity mismatch: option {index} is {payload}")
        if command.get("expected_option_index") is not None and command["expected_option_index"] != payload.get(
            "optionIndex"
        ):
            raise AdapterError(f"identity mismatch: option {index} is {payload}")

        def build(ops: _Ops, request: dict) -> None:
            ops.expect_class(H(entry["child_handle"]), entry["child_class"])
            ops.call(H(entry["child_handle"]), entry["method"], *entry["args"])

        self._submit(snapshot, build, timeout)
        response = {
            "ok": True,
            "submitted_type": "CastingTimeOption",
            "submitted_choice_kind": payload["choiceKind"],
            "submitted_grp_id": payload.get("grpId", 0),
        }
        if "optionIndex" in payload:
            response["submitted_option_index"] = payload["optionIndex"]
        return response

    def _call_on_request(
        self, command: dict, timeout: float | None, classes: tuple[str, ...], method: str, *args: dict
    ) -> Snapshot:
        snapshot = self._require(timeout, *classes)
        self._submit(snapshot, lambda ops, request: ops.call(request, method, *args), timeout)
        return snapshot

    def _cmd_submit_pass(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout, "ActionsAvailableRequest")
        if not snapshot.getters.get("CanPass"):
            raise AdapterError("Cannot pass on current interaction")
        self._submit(snapshot, lambda ops, request: ops.call(request, "SubmitPass"), timeout)
        return {"ok": True, "submitted_type": "Pass"}

    def _cmd_submit_mulligan(self, command: dict, timeout: float | None) -> dict:
        keep = bool(command.get("keep"))
        self._call_on_request(command, timeout, ("MulliganRequest",), "KeepHand" if keep else "MulliganHand")
        return {"ok": True, "submitted_type": "Keep" if keep else "Mulligan"}

    def _cmd_submit_choose_starting_player(self, command: dict, timeout: float | None) -> dict:
        seat = int(command.get("seat_id") or 0)
        self._call_on_request(command, timeout, ("ChooseStartingPlayerRequest",), "ChooseStartingPlayer", U(seat))
        return {"ok": True, "submitted_type": "ChooseStartingPlayer", "seat_id": seat}

    def _cmd_submit_optional(self, command: dict, timeout: float | None) -> dict:
        accept = bool(command.get("accept"))
        response = "AllowYes" if accept else "CancelNo"
        self._call_on_request(command, timeout, ("OptionalActionMessageRequest",), "SubmitResponse", E(response))
        return {"ok": True, "submitted_type": "Optional", "response": response}

    def _cmd_submit_numeric(self, command: dict, timeout: float | None) -> dict:
        value = int(command.get("value") or 0)
        self._call_on_request(command, timeout, ("NumericInputRequest",), "SubmitValue", U(value))
        return {"ok": True, "submitted_type": "NumericInput", "value": value}

    def _cmd_submit_x(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout, "CastingTimeOptionRequest", "NumericInputRequest")
        requested = int(command.get("value") or 0)
        if snapshot.request_class == "NumericInputRequest":
            target, method, kind = snapshot.request, "SubmitValue", "NumericInput"
        else:
            target = next(
                (c for c in items(field(snapshot.request, "ChildRequests"))
                 if short_class(c) == "CastingTimeOption_NumericInputRequest"),
                None,
            )
            if target is None:
                raise AdapterError("CastingTimeOptionRequest has no CastingTimeOption_NumericInputRequest child")
            method, kind = "SubmitX", "CastingTimeOption_X"
        # Clamp before the uint conversion (issue #390: out-of-range X wedged the slider).
        value = max(num(field(target, "Min")), min(num(field(target, "Max")), requested))
        self._submit(snapshot, lambda ops, request: ops.call(H(handle(target)), method, U(value)), timeout)
        return {"ok": True, "submitted_type": kind, "value": value, "clamped": value != requested}

    def _cmd_submit_selection(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout, "SelectNRequest", "SearchRequest")
        ids = [int(i) for i in command.get("ids") or []]
        if snapshot.request_class == "SelectNRequest" and not ids:
            self._submit(snapshot, lambda ops, request: ops.call(request, "SubmitArbitrary"), timeout)
            return {"ok": True, "submitted_type": "SelectN"}
        selection = LIST([U(i) for i in ids])
        self._submit(snapshot, lambda ops, request: ops.call(request, "SubmitSelection", selection), timeout)
        return {"ok": True, "submitted_type": "SelectN" if snapshot.request_class == "SelectNRequest" else "Search"}

    def _cmd_submit_group(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout, "GroupRequest")
        specs = command.get("groups") or []

        def build(ops: _Ops, request: dict) -> None:
            groups = [self._new_group(ops, spec, zones=True) for spec in specs]
            ops.call(request, "SubmitGroups", LIST(groups))

        self._submit(snapshot, build, timeout)
        return {"ok": True, "submitted_type": "Group"}

    @staticmethod
    def _new_group(ops: _Ops, spec: dict, zones: bool = False) -> dict:
        group = ops.new(MESSAGING + "Group")
        ids = ops.get(group, "Ids")
        for value in spec.get("ids") or []:
            ops.call(ids, "Add", U(int(value)))
        if zones:
            for key, prefix, member in (("zone", "ZoneType_", "ZoneType"), ("sub_zone", "SubZoneType_", "SubZoneType")):
                raw = str(spec.get(key) or "")
                if raw:
                    ops.set(group, member, E(raw.removeprefix(prefix)))
        elif int(spec.get("groupId") or 0) > 0:
            ops.set(group, "GroupId", I(int(spec["groupId"])))
        return group

    def _cmd_submit_blockers(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout, "DeclareBlockersRequest")
        assignments = command.get("assignments") or []
        if not assignments:
            self._submit(snapshot, lambda ops, request: ops.call(request, "SubmitBlockers"), timeout)
            return {"ok": True, "submitted_type": "DeclareBlockers"}
        by_id = {num(field(b, "BlockerInstanceId")): b for b in items(field(snapshot.request, "AllBlockers"))}
        selected = []
        for assignment in assignments:
            blocker = by_id.get(int(assignment.get("blockerInstanceId") or 0))
            if blocker is None:
                logger.warning("mac bridge: blocker %s not in AllBlockers", assignment.get("blockerInstanceId"))
                continue
            selected.append((blocker, [int(a) for a in assignment.get("attackerInstanceIds") or []]))

        def build(ops: _Ops, request: dict) -> None:
            # Fresh messages through OnSubmit, as the plugin does: UpdateBlockers
            # and SubmitBlockers share one outbound message and the second
            # overwrites the first before it is serialized.
            if selected:
                declaration = ops.message("DeclareBlockersResp", snapshot.game_state_id, snapshot.msg_id)
                ops.set(declaration, "DeclareBlockersResp", ops.new(MESSAGING + "DeclareBlockersResp"))
                chosen = ops.get(ops.get(declaration, "DeclareBlockersResp"), "SelectedBlockers")
                for blocker, attacker_ids in selected:
                    attackers = ops.get(H(handle(blocker)), "SelectedAttackerInstanceIds")
                    ops.call(attackers, "Clear")
                    for attacker_id in attacker_ids:
                        ops.call(attackers, "Add", U(attacker_id))
                    ops.call(chosen, "Add", H(handle(blocker)))
                ops.on_submit(request, declaration)
            ops.on_submit(request, ops.message("SubmitBlockersReq", snapshot.game_state_id, snapshot.msg_id))

        self._submit(snapshot, build, timeout)
        return {"ok": True, "submitted_type": "DeclareBlockers"}

    def _cmd_submit_attackers(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout, "DeclareAttackerRequest")
        wanted = {int(a.get("attackerInstanceId") or 0) for a in command.get("attackers") or []}
        matched = []
        for attacker in items(field(snapshot.request, "Attackers")):
            legal = items(field(attacker, "LegalDamageRecipients"))
            if (
                num(field(attacker, "AttackerInstanceId")) in wanted
                and field(attacker, "SelectedDamageRecipient") is None
                and legal
            ):
                matched.append((attacker, legal[0]))
        if not wanted or not matched:
            # Finalize: the second step of the two-step flow, or "attack with nobody".
            self._submit(snapshot, lambda ops, request: ops.call(request, "SubmitAttackers"), timeout)
            return {"ok": True, "submitted_type": "DeclareAttackersSubmit"}

        def build(ops: _Ops, request: dict) -> None:
            for attacker, recipient in matched:
                ops.set(H(handle(attacker)), "SelectedDamageRecipient", H(handle(recipient)))
            ops.call(request, "UpdateAttacker", LIST([H(handle(a)) for a, _ in matched]))

        self._submit(snapshot, build, timeout)
        return {
            "ok": True,
            "submitted_type": "DeclareAttackersUpdate",
            "needs_finalize": True,
            "declared_count": len(matched),
        }

    def _cmd_submit_targets(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout, "SelectTargetsRequest")
        preferred: list[int] = []
        for value in list(command.get("target_instance_ids") or []) + [command.get("target_instance_id")]:
            if value and int(value) not in preferred:
                preferred.append(int(value))
        caller_target = preferred[0] if preferred else 0
        finalize = command.get("finalize", True) is not False
        selections = items(field(snapshot.request, "TargetSelections"))

        def unsatisfied(selection: dict) -> bool:
            return num(field(selection, "MinTargets")) > 0 and num(field(selection, "SelectedTargets")) < num(
                field(selection, "MinTargets")
            )

        chosen: list[tuple[dict, dict]] = []
        used: set[int] = set()
        required = required_filled = 0
        for selection in selections:
            required_slot = unsatisfied(selection)
            required += required_slot
            targets = items(field(selection, "Targets"))
            pick = next(
                (t for pid in preferred if pid not in used for t in targets if num(field(t, "TargetInstanceId")) == pid),
                None,
            ) or next((t for t in targets if num(field(t, "TargetInstanceId")) not in used), None)
            if pick is None:
                continue  # no free legal target; SubmitTargets decides if that is acceptable
            used.add(num(field(pick, "TargetInstanceId")))
            required_filled += required_slot
            chosen.append((selection, pick))

        base = {"submitted_type": "SelectTargets", "target_instance_id": caller_target}
        if finalize and not chosen and not any(unsatisfied(s) for s in selections):
            self._commit_targets(snapshot, timeout)
            return {"ok": True, **base, "already_selected": True, "finalized": True}
        if not chosen:
            raise AdapterError("No legal targets to fill")

        def build(ops: _Ops, request: dict) -> None:
            # One fresh SelectTargetsResp per slot through OnSubmit: UpdateTarget
            # reuses the request's outbound message, which the commit would
            # overwrite before it is serialized.
            for selection, target in chosen:
                sent = ops.new(MESSAGING + "Target")
                ops.set(sent, "TargetInstanceId", U(num(field(target, "TargetInstanceId"))))
                ops.set(sent, "LegalAction", E("Select"))
                ops.set(sent, "Highlight", I(num(field(target, "Highlight"))))
                slot = ops.new(MESSAGING + "TargetSelection")
                ops.set(slot, "TargetIdx", U(num(field(selection, "TargetIdx"))))
                ops.call(ops.get(slot, "Targets"), "Add", sent)
                response_body = ops.new(MESSAGING + "SelectTargetsResp")
                ops.set(response_body, "Target", slot)
                message = ops.message("SelectTargetsResp", snapshot.game_state_id, snapshot.msg_id)
                ops.set(message, "SelectTargetsResp", response_body)
                ops.on_submit(request, message)

        self._submit(snapshot, build, timeout)
        all_required = required_filled >= required
        result = {
            **base,
            "slots_required": required,
            "slots_filled": len(chosen),
            "required_filled": required_filled,
        }
        if finalize and all_required:
            advanced = self._deferred_commit_targets(snapshot, timeout)
            return {"ok": True, **result, "finalized": True, "advanced_without_commit": advanced}
        if finalize:
            logger.warning("mac bridge SubmitTargets: only %d/%d required slots filled", required_filled, required)
        return {"ok": all_required, **result, "finalized": False}

    def _commit_targets(self, snapshot: Snapshot, timeout: float | None) -> None:
        def build(ops: _Ops, request: dict) -> None:
            ops.on_submit(request, ops.message("SubmitTargetsReq", snapshot.game_state_id, snapshot.msg_id))

        self._submit(snapshot, build, timeout)

    def _deferred_commit_targets(self, original: Snapshot, timeout: float | None) -> bool:
        """Commit on the GRE's updated SelectTargetsReq, like the real client.

        Returns True when the request disappeared without our commit (the client
        auto-submitted). Mirrors Plugin.DeferredSubmitTargets.
        """
        source = num(field(original.request, "SourceId"))
        started = time.monotonic()
        gone_since: float | None = None
        last_seen: Snapshot | None = None
        while time.monotonic() - started < TARGET_COMMIT_TIMEOUT_S:
            time.sleep(0.03)
            current = self.snapshot(depth=4, timeout=timeout)
            if current.request_class != "SelectTargetsRequest" or num(field(current.request, "SourceId")) != source:
                gone_since = gone_since or time.monotonic()
                if time.monotonic() - gone_since >= TARGET_GONE_GRACE_S:
                    return True
                continue
            gone_since = None
            last_seen = current
            if current.handle == original.handle:
                continue  # the GRE has not round-tripped our selection yet
            selections = items(field(current.request, "TargetSelections"))
            if all(
                num(field(s, "MinTargets")) == 0 or num(field(s, "SelectedTargets")) >= num(field(s, "MinTargets"))
                for s in selections
            ):
                self._commit_targets(current, timeout)
                return False
        # No updated request: the original ids are still current, so the legacy
        # immediate commit is right.
        self._commit_targets(last_seen or original, timeout)
        return False

    def _cmd_submit_assign_damage(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout, "AssignDamageRequest")
        existing = {num(field(a, "InstanceId")): a for a in items(field(snapshot.request, "Assigners"))}
        plans = []
        for spec in command.get("assigners") or []:
            assigner = existing.get(int(spec.get("instanceId") or 0))
            if assigner is None:
                logger.warning("mac bridge submit_assign_damage: attacker %s not in request", spec.get("instanceId"))
                continue
            templates = {num(field(a, "InstanceId")): a for a in items(field(assigner, "Assignments"))}
            plans.append((assigner, spec.get("assignments") or [], templates))

        def build(ops: _Ops, request: dict) -> None:
            built = []
            for assigner, assignments, templates in plans:
                clone = ops.new(MESSAGING + "DamageAssigner")
                ops.set(clone, "InstanceId", U(num(field(assigner, "InstanceId"))))
                ops.set(clone, "TotalDamage", U(num(field(assigner, "TotalDamage"))))
                target_list = ops.get(clone, "Assignments")
                for assignment in assignments:
                    receiver, damage = int(assignment.get("instanceId") or 0), int(assignment.get("damage") or 0)
                    template = templates.get(receiver)
                    entry = ops.new(MESSAGING + "DamageAssignment")
                    ops.set(entry, "InstanceId", U(receiver))
                    ops.set(entry, "MinDamage", U(num(field(template, "MinDamage")) if template else 0))
                    ops.set(entry, "MaxDamage", U(num(field(template, "MaxDamage")) if template else damage))
                    ops.set(entry, "AssignedDamage", U(damage))
                    ops.call(target_list, "Add", entry)
                built.append(clone)
            ops.call(request, "SubmitAssignment", LIST(built))

        self._submit(snapshot, build, timeout)
        return {"ok": True, "submitted_type": "AssignDamage", "assigner_count": len(plans)}

    def _cmd_submit_distribution(self, command: dict, timeout: float | None) -> dict:
        pairs = [[U(int(k)), U(int(v))] for k, v in (command.get("distributions") or {}).items() if str(k).isdigit()]
        self._call_on_request(command, timeout, ("DistributionRequest",), "SubmitDistribution", {"dict": pairs})
        return {"ok": True, "submitted_type": "Distribution", "target_count": len(pairs)}

    def _cmd_submit_order(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout, "OrderRequest")
        ids = command.get("ids")
        ordered = [int(i) for i in ids] if ids is not None else [num(i) for i in items(field(snapshot.request, "Ids"))]
        self._submit(snapshot, lambda ops, request: ops.call(request, "SubmitOrder", LIST([U(i) for i in ordered])),
                     timeout)
        return {"ok": True, "submitted_type": "Order", "count": len(ordered)}

    def _cmd_submit_select_replacement(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout, "SelectReplacementRequest")
        if command.get("decline"):
            if not (snapshot.getters.get("IsOptional") or field(snapshot.request, "IsOptional")):
                raise AdapterError("SelectReplacementRequest is not optional")
            self._submit(snapshot, lambda ops, request: ops.call(request, "Decline"), timeout)
            return {"ok": True, "submitted_type": "SelectReplacement", "declined": True}
        replacements = items(field(snapshot.request, "Replacements"))
        index = int(command.get("index") or 0)
        if not 0 <= index < len(replacements):
            raise AdapterError(f"Index {index} out of range (0-{len(replacements) - 1})")
        choice = H(handle(replacements[index]))
        self._submit(snapshot, lambda ops, request: ops.call(request, "SubmitReplacement", choice), timeout)
        return {"ok": True, "submitted_type": "SelectReplacement", "index": index}

    def _cmd_submit_select_counters(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout, "SelectCountersRequest")
        specs = command.get("pairs") or []

        def build(ops: _Ops, request: dict) -> None:
            pairs = []
            for spec in specs:
                pair = ops.new(MESSAGING + "CounterPair")
                if spec.get("counterType"):
                    ops.set(pair, "CounterType", E(str(spec["counterType"])))
                ops.set(pair, "Count", U(int(spec.get("amount") or spec.get("count") or 0)))
                if int(spec.get("instanceId") or 0):
                    ops.set(pair, "InstanceId", U(int(spec["instanceId"])))
                pairs.append(pair)
            ops.call(request, "SubmitCountersResponse", LIST(pairs))

        self._submit(snapshot, build, timeout)
        return {"ok": True, "submitted_type": "SelectCounters", "pair_count": len(specs)}

    def _cmd_submit_string_input(self, command: dict, timeout: float | None) -> dict:
        value = str(command.get("value") or "")
        self._call_on_request(command, timeout, ("StringInputRequest",), "SubmitValue", {"str": value})
        return {"ok": True, "submitted_type": "StringInput", "value": value}

    def _cmd_submit_intermission(self, command: dict, timeout: float | None) -> dict:
        option = str(command.get("option") or "")
        self._call_on_request(command, timeout, ("IntermissionRequest",), "SubmitOption", E(option))
        return {"ok": True, "submitted_type": "Intermission", "option": option}

    def _cmd_submit_gather(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout, "GatherRequest")
        specs = command.get("gatherings") or []

        def build(ops: _Ops, request: dict) -> None:
            gatherings = []
            for spec in specs:
                gathering = ops.new(MESSAGING + "Gathering")
                ops.set(gathering, "InstanceId", U(int(spec.get("instanceId") or 0)))
                ops.set(gathering, "Amount", U(int(spec.get("amount") or 0)))
                gatherings.append(gathering)
            ops.call(request, "SubmitGathering", LIST(gatherings))

        self._submit(snapshot, build, timeout)
        return {"ok": True, "submitted_type": "Gather", "count": len(specs)}

    def _cmd_submit_auto_tap(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout)
        auto_tap = auto_tap_request(snapshot.request)
        if auto_tap is None:
            raise AdapterError(f"Pending is {snapshot.request_class}, no AutoTapActionsRequest available")
        solutions = items(field(auto_tap, "Solutions"))
        index = int(command.get("solution_index") or 0)
        if not solutions:
            raise AdapterError("AutoTap Solutions list is empty")
        if not 0 <= index < len(solutions):
            raise AdapterError(f"AutoTap solution index {index} out of range (0-{len(solutions) - 1})")
        chosen = H(handle(solutions[index]))
        self._submit(snapshot, lambda ops, request: ops.call(H(handle(auto_tap)), "SubmitSolution", chosen), timeout)
        return {"ok": True, "submitted_type": "AutoTap", "solution_index": index}

    def _cmd_submit_select_from_groups(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout, "SelectFromGroupsRequest")
        specs = command.get("groups") or []

        def build(ops: _Ops, request: dict) -> None:
            ops.call(request, "Submit", LIST([self._new_group(ops, spec) for spec in specs]))

        self._submit(snapshot, build, timeout)
        return {"ok": True, "submitted_type": "SelectFromGroups", "group_count": len(specs)}

    def _cmd_submit_select_n_group(self, command: dict, timeout: float | None) -> dict:
        ids = command.get("ids")
        if ids is not None:
            values = [int(i) for i in ids]
            self._call_on_request(
                command, timeout, ("SelectNGroupRequest",), "SubmitGroupSelection", LIST([U(i) for i in values])
            )
            return {"ok": True, "submitted_type": "SelectNGroup", "count": len(values)}
        single = int(command.get("id") or 0)
        self._call_on_request(command, timeout, ("SelectNGroupRequest",), "SubmitGroupSelection", U(single))
        return {"ok": True, "submitted_type": "SelectNGroup", "id": single}

    def _cmd_submit_search_from_groups(self, command: dict, timeout: float | None) -> dict:
        if command.get("zone") is not None:
            zone = int(command["zone"])
            self._call_on_request(command, timeout, ("SearchFromGroupsRequest",), "SubmitZone", U(zone))
            return {"ok": True, "submitted_type": "SearchFromGroups", "zone": zone}
        snapshot = self._require(timeout, "SearchFromGroupsRequest")
        specs = command.get("groups") or []

        def build(ops: _Ops, request: dict) -> None:
            ops.call(request, "SubmitSelection", LIST([self._new_group(ops, spec) for spec in specs]))

        self._submit(snapshot, build, timeout)
        return {"ok": True, "submitted_type": "SearchFromGroups", "group_count": len(specs)}

    def _cmd_submit_casting_mana_type(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout, "CastingTimeOption_ManaTypeRequest", "CastingTimeOptionRequest")
        request = snapshot.request
        mana = request if snapshot.request_class == "CastingTimeOption_ManaTypeRequest" else next(
            (c for c in items(field(request, "ChildRequests")) if short_class(c) == "CastingTimeOption_ManaTypeRequest"),
            None,
        )
        if mana is None:
            raise AdapterError(f"Pending is {snapshot.request_class}, no CastingTimeOption_ManaTypeRequest child")
        colors = [str(c) for c in command.get("colors") or []]
        inner = items(field(mana, "InnerRequests"))
        if len(colors) != len(inner):
            raise AdapterError(f"Need {len(inner)} colors, got {len(colors)}")
        values = LIST([E(c) for c in colors])
        self._submit(snapshot, lambda ops, req: ops.call(H(handle(mana)), "SubmitSelection", values), timeout)
        return {"ok": True, "submitted_type": "CastingManaType", "colors": ",".join(colors)}

    def _cmd_auto_respond(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout)
        self._submit(snapshot, lambda ops, request: ops.call(request, "AutoRespond"), timeout)
        return {"ok": True, "submitted_type": "AutoRespond", "request_class": snapshot.request_class}

    def _cmd_cancel_action(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout)
        cancel = bool(snapshot.getters.get("CanCancel"))
        self._submit(snapshot, lambda ops, request: ops.call(request, "Cancel" if cancel else "AutoRespond"), timeout)
        return {"ok": True, "cancelled": cancel, "request_class": snapshot.request_class}


# ---------------------------------------------------------------------------
# Shaping helpers shared by observation and submission
# ---------------------------------------------------------------------------


def serialize_action(action: dict) -> dict[str, Any]:
    """Plugin.SerializeAction for a dumped protobuf Action."""
    result: dict[str, Any] = {
        "actionType": enum_name(field(action, "ActionType")),
        "grpId": num(field(action, "GrpId")),
        "instanceId": num(field(action, "InstanceId")),
    }
    for key, member in (
        ("abilityGrpId", "AbilityGrpId"), ("sourceId", "SourceId"), ("alternativeGrpId", "AlternativeGrpId"),
        ("facetId", "FacetId"), ("uniqueAbilityId", "UniqueAbilityId"),
    ):
        value = num(field(action, member))
        if value:
            result[key] = value
    result["assumeCanBePaidFor"] = bool(field(action, "AssumeCanBePaidFor"))
    costs = mana_costs(field(action, "ManaCost"))
    if costs:
        result["manaCost"] = costs
    solution = field(action, "AutoTapSolution")
    if isinstance(solution, dict):
        result["hasAutoTap"] = True
        taps = [
            {"instanceId": num(field(tap, "InstanceId")), "manaId": num(field(tap, "ManaId"))}
            for tap in items(field(solution, "AutoTapActions"))
        ]
        if taps:
            result["autoTapActions"] = taps
    targets = [{"targetId": num(field(t, "TargetIdx"))} for t in items(field(action, "Targets"))]
    if targets:
        result["targets"] = targets
    if num(field(action, "Highlight")):
        result["highlight"] = enum_name(field(action, "Highlight"))
    if field(action, "ShouldStop"):
        result["shouldStop"] = True
    if field(action, "IsBatchable"):
        result["isBatchable"] = True
    return result


def mana_costs(node: Any) -> list[dict[str, Any]]:
    costs = []
    for cost in items(node):
        colors = field(cost, "Color")
        color = (
            "[ " + ", ".join(f'"{enum_name(c)}"' for c in items(colors)) + " ]"
            if isinstance(colors, dict) and "$items" in colors
            else enum_name(colors)
        )
        costs.append({"color": color, "count": num(field(cost, "Count"))})
    return costs


def recipient_id(recipient: dict, member: str) -> int | None:
    """DamageRecipient's oneof id; only the populated case has a value."""
    case = enum_name(field(recipient, "IdCase"))
    value = field(recipient, member)
    if value is None and "id_" in recipient and case == member:
        value = recipient["id_"]
    return num(value) if value is not None and case in ("", member) else None


def replacement_display(replacement: Any) -> str:
    if not isinstance(replacement, dict):
        return "" if replacement is None else str(replacement)
    parts = [f"{camel_case(k)}={v}" for k, v in replacement.items() if not k.startswith("$") and isinstance(v, (int, str))]
    return f"{short_class(replacement)}({', '.join(parts)})"


def plain_object(node: Any, depth: int = 0) -> Any:
    """JSON-friendly copy of a dumped node (enums by name, lists as lists)."""
    if isinstance(node, dict):
        if "e" in node and "v" in node and len(node) == 2:
            return node["e"]
        if "$items" in node:
            return [plain_object(item, depth + 1) for item in node["$items"]] if depth < 3 else []
        return {
            camel_case(k): plain_object(v, depth + 1)
            for k, v in node.items()
            if not k.startswith("$") and depth < 3 and v is not None
        }
    return node


def auto_tap_request(request: dict | None) -> dict | None:
    if request is None:
        return None
    name = short_class(request)
    if name == "AutoTapActionsRequest":
        return request
    if name != "PayCostsRequest":
        return None
    child = field(request, "AutoTapActions")
    if isinstance(child, dict):
        return child
    return next((c for c in items(field(request, "ChildRequests")) if short_class(c) == "AutoTapActionsRequest"), None)


def numeric_values(request: dict) -> list[int]:
    """Plugin.EnumerateNumericInputValues."""
    low, high = num(field(request, "Min")), num(field(request, "Max"))
    step = num(field(request, "StepSize")) or 1
    disallowed = {num(v) for v in items(field(request, "DisallowedValues"))}
    values = []
    value = low
    while value <= high and len(values) < MAX_NUMERIC_INPUT_ENTRIES:
        if value not in disallowed and not (field(request, "DisallowEven") and value % 2 == 0) and not (
            field(request, "DisallowOdd") and value % 2 == 1
        ):
            values.append(value)
        value += step
    return values


def casting_time_entries(request: dict) -> list[dict[str, Any]]:
    """Plugin.BuildCastingTimeOptionEntries: one entry per selectable child option.

    Each entry carries the plugin's payload plus how to submit it: the child's
    handle and class, the method and its encoded arguments.
    """
    entries: list[dict[str, Any]] = []

    def add(child: dict, index: int, kind: str, label: str, method: str, args: list[dict],
            option_index: int | None = None, grp_id: int = 0, **extra: Any) -> dict:
        payload: dict[str, Any] = {
            "actionType": "CastingTimeOption", "choiceKind": kind, "requestClass": short_class(child),
            "childIndex": index, "label": label,
        }
        if option_index is not None:
            payload["optionIndex"] = option_index
        if grp_id:
            payload["grpId"] = grp_id
        if num(field(child, "SourceId")):
            payload["sourceId"] = num(field(child, "SourceId"))
        payload.update(extra)
        entries.append(
            {"payload": payload, "child_handle": handle(child), "child_class": short_class(child), "method": method,
             "args": args}
        )
        return payload

    for index, child in enumerate(items(field(request, "ChildRequests"))):
        kind = short_class(child)
        grp = num(field(child, "GrpId"))
        if kind == "CastingTimeOption_ModalRequest":
            for option, option_grp in enumerate(num(v) for v in items(field(child, "ModalOptions"))):
                extra: dict[str, Any] = {}
                if num(field(child, "AbilityGrpId")):
                    extra["abilityGrpId"] = num(field(child, "AbilityGrpId"))
                if num(field(child, "Min")) > 0:
                    extra["min"] = num(field(child, "Min"))
                if num(field(child, "Max")) > 0:
                    extra["max"] = num(field(child, "Max"))
                other = [num(v) for v in items(field(child, "OtherSelection"))]
                if other:
                    extra["otherSelection"] = other
                add(child, index, "modal", f"Mode {option + 1}", "SubmitModal", [LIST([U(option_grp)])],
                    option, option_grp, **extra)
        elif kind == "CastingTimeOption_ChooseOrCostRequest":
            for option, pair in enumerate(items(field(child, "Options"))):
                prompt_id, selection = num(field(pair, "Key")), num(field(pair, "Value"))
                extra = {"selection": selection}
                if prompt_id:
                    extra["promptId"] = prompt_id
                if num(field(child, "Min")) > 0:
                    extra["min"] = num(field(child, "Min"))
                if num(field(child, "Max")) > 0:
                    extra["max"] = num(field(child, "Max"))
                add(child, index, "choose_or_cost", f"Choice {option + 1}", "SubmitChoice", [LIST([U(selection)])],
                    option, grp, **extra)
        elif kind == "CastingTimeOption_DoneRequest":
            payload = add(child, index, "done", "Done", "SubmitDone", [])
            costs = mana_costs(field(child, "ManaCost"))
            if costs:
                payload["manaCost"] = costs
        elif kind in (
            "CastingTimeOption_TimingPermissionRequest", "CastingTimeOption_KickerRequest",
            "CastingTimeOption_AdditionalCostRequest",
        ):
            choice, label, method = {
                "CastingTimeOption_TimingPermissionRequest": ("timing_permission", "Timing Permission", "SubmitFlash"),
                "CastingTimeOption_KickerRequest": ("kicker", "Kicker", "SubmitKicked"),
                "CastingTimeOption_AdditionalCostRequest": ("additional_cost", "Additional Cost", "SubmitAdditionalCost"),
            }[kind]
            payload = add(child, index, choice, label, method, [], grp_id=grp)
            costs = mana_costs(field(child, "ManaCost"))
            if costs:
                payload["manaCost"] = costs
        elif kind == "CastingTimeOption_CostKeywordRequest":
            add(child, index, "cost_keyword", enum_name(field(child, "OptionType")), "SubmitKeywordAction", [],
                grp_id=grp)
        elif kind == "CastingTimeOption_NumericInputRequest":
            fixed = num(field(child, "Min")) == num(field(child, "Max"))
            for value in [num(field(child, "Min"))] if fixed else numeric_values(child):
                extra = {"numericValue": value}
                if not fixed:
                    extra.update(min=num(field(child, "Min")), max=num(field(child, "Max")))
                add(child, index, "numeric_input", f"Value {value}" if fixed else f"X = {value}", "SubmitX",
                    [U(value)], grp_id=grp, **extra)
        elif kind == "CastingTimeOption_Replicate":
            low, high = num(field(child, "Min")), num(field(child, "Max"))
            for value in range(low, min(high, low + MAX_NUMERIC_INPUT_ENTRIES - 1) + 1):
                extra = {"numericValue": value}
                if low != high:
                    extra.update(min=low, max=high)
                add(child, index, "replicate", f"Replicate {value}", "SubmitValue", [U(value)], **extra)
        elif kind == "CastingTimeOption_SpecializeRequest":
            for color in items(field(child, "SelectableColors")):
                add(child, index, "specialize", f"Specialize: {enum_name(color)}", "SubmitSpecialization",
                    [I(num(color))], grp_id=num(field(child, "SourceAbilityId")), colorName=enum_name(color),
                    colorValue=num(color))
        elif kind == "CastingTimeOption_ManaTypeRequest":
            defaults = []
            for inner in items(field(child, "InnerRequests")):
                options = items(field(inner, "ManaColorOptions"))
                if options:
                    defaults.append(options[max(0, min(num(field(inner, "DefaultIndex")), len(options) - 1))])
            names = [enum_name(c) for c in defaults]
            add(child, index, "mana_type", f"Mana types: {','.join(names)}", "SubmitSelection",
                [LIST([I(num(c)) for c in defaults])], colors=names)
    return entries


def build_request_payload(request: dict, request_type: str, request_class: str) -> dict[str, Any]:
    """Plugin.BuildPendingRequestPayload, from dumped fields instead of properties."""
    payload: dict[str, Any] = {"requestType": request_type, "requestClass": request_class}
    for key, value in request.items():
        if key.startswith("$"):
            continue
        name = camel_case(key)
        if name in ("type", "canCancel", "allowUndo") or not name:
            continue
        if request_type == "ActionsAvailableReq" and name == "actions":
            continue
        if request_type == "CastingTimeOptionsReq" and name == "childRequests":
            continue
        if value is None or value == "" or (isinstance(value, dict) and value.get("$n") == 0):
            continue
        if not any(keyword in name.lower() for keyword in INTERESTING_NAME_KEYWORDS):
            continue
        plain = plain_object(value)
        if plain not in (None, "", [], {}):
            payload[name] = plain
    return payload if len(payload) > 2 else {}


def build_decision_context(request_type: str, request_class: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Plugin.BuildPendingRequestDecisionContext."""
    context: dict[str, Any] = {"requestType": request_type, "requestClass": request_class}
    mapped = DECISION_TYPES.get(request_type)
    if mapped:
        context["type"] = mapped
    for name in (
        "prompt", "promptText", "message", "messageText", "help", "helpText", "sourceId", "grpId", "abilityGrpId",
        "zoneId", "count", "min", "max", "minCount", "maxCount", "amount", "total", "ids", "options", "targets",
        "validTargets", "targetsToSelect", "qualifiedTargets", "attackers", "qualifiedAttackers", "blockers",
        "qualifiedBlockers", "groups", "counterTypes",
    ):
        if payload.get(name) is not None:
            context[name] = payload[name]
    return context if len(context) > 2 else None
