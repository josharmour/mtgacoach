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

Observation is bridge-first for board state: ``get_game_state`` walks the live
``MtgGameState`` through the reflect ops (arenamcp/mac_game_state.py), while
reveal/annotation history still comes from Player.log (docs/DECISIONS.md,
2026-07-16 and 2026-09-23).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from arenamcp.color_choice import color_options, color_selection_ids
from arenamcp.decisions import TargetSlot, assign_target_slots

logger = logging.getLogger(__name__)

SendFn = Callable[[dict[str, Any], "float | None"], dict[str, Any]]

MESSAGING = "Wotc.Mtgo.Gre.External.Messaging."
# Namespaced wrapper classes: the probe resolves a bare name only in the global
# namespace, and an optional find of an unknown class reads as "screen closed".
DRAFT_CONTROLLER = "Wotc.Mtga.Wrapper.Draft.DraftContentController"
EVENT_PAGE_CONTROLLER = "EventPage.EventPageContentController"
EVENT_MAIN_BUTTON = "EventPage.Components.MainButtonComponent"
# Sealed pool reveal after joining (members verified in live IL2CPP metadata 2026-10-06).
SEALED_OPEN = "SealedBoosterOpenAnimation"
# Post-match "Did you have fun in the match?" dialog (MatchEndScene.SurveyUI);
# members verified in live IL2CPP metadata 2026-10-07 (see _cmd_dismiss_survey).
SURVEY = "GameEndSurvey"
# Emote ids a click on the emote wheel puts on the wire (onChat.text); members of
# the emote path verified in live IL2CPP metadata 2026-10-06 (see _cmd_send_emote).
EMOTE_IDS = {"oops": "Phrase_Basic_Oops"}
# Request fields never needed for the protocol and bulky or recursive to dump.
SNAPSHOT_SKIP = ["OriginalMessage", "ParentRequest", "_outboundMessage", "OnSubmit", "OnRequestSubmit"]
SNAPSHOT_DEPTH = 7
# List entries the request snapshot keeps (the probe's default is 100). Herald's Horn's
# creature-type SelectN offers 353 subtype ids; cut at 100, Rabbit (232) was not offered and
# the autopilot named Angel (ledger #186, match 4d934402).
SNAPSHOT_MAX_ITEMS = 600
# Speculative getters read in the snapshot batch; absent members come back null.
SNAPSHOT_GETTERS = [
    "Type",
    "CanCancel",
    "AllowUndo",
    "CanPass",
    "CanSubmit",
    "IsInstanceIdSelection",
    "IsZoneSelection",
    "IsManaColorSelection",
    "IsCardColorSelection",
    "IsCounterSelection",
    "IsBasicLandSelection",
    "IsTriggeredAbilitySelection",
    "IsStackingDecision",
    "IsMultiZoneSearch",
    "IsOptional",
]
MAX_NUMERIC_INPUT_ENTRIES = 20
TARGET_COMMIT_TIMEOUT_S = 1.2
TARGET_GONE_GRACE_S = 0.6
# Concede/emote: how long a live turn behind the expected one is re-read while
# Arena's duel scene plays the newer states back (see _await_live_game), and
# how much of the command's expires_in_ms is left for the guarded batch.
LIVE_TURN_CATCH_UP_S = 3.0
LIVE_TURN_POLL_S = 0.1
LIVE_TURN_SEND_MARGIN_S = 0.5
# The player's own selection began its round trip before we noticed it; the
# one live case took 1.72 s (2026-10-18 21:15:30.263 -> 21:15:31.981).
TARGET_ANSWERED_WAIT_S = 3.0

# Commands answered by the injected library directly.
PASSTHROUGH = {"ping", "reflect_batch"}

# The BepInEx plugin (Windows client, natively or under Proton) speaks reflect-1
# too, but keeps its own in-game commands. Only the limited-event, deck, screen
# and survey hands it lacks run here; get_draft_state is included because the
# plugin's reply ({"scenes": ...}) lacks the pack fields the draft driver reads.
BEPINEX_RUNTIME = "bepinex"
BEPINEX_REFLECT_COMMANDS = frozenset(
    {"confirm_draft_pick", "dismiss_survey", "event_play", "finish_sealed_open", "get_deck_editor",
     "get_decks", "get_draft_state", "get_event_page", "get_limited_pool", "get_match_end", "get_screen",
     "get_survey", "go_to_event", "leave_match", "preview_draft_pick", "set_limited_deck",
     "submit_draft_pick", "submit_limited_deck"}
)  # fmt: skip

DECISION_TYPES = {
    "SelectTargetsReq": "target_selection",
    "SearchReq": "search",
    "DistributionReq": "distribution",
    "NumericInputReq": "numeric_input",
    "SelectNReq": "select_n",
    "GroupReq": "group_selection",
    "GroupOptionReq": "modal_choice",
    "DeclareAttackersReq": "declare_attackers",
    "DeclareBlockersReq": "declare_blockers",
    "PayCostsReq": "pay_costs",
    "ChooseStartingPlayerReq": "choose_starting_player",
    "SelectReplacementReq": "select_replacement",
    "SelectNGroupReq": "select_n_group",
    "SelectFromGroupsReq": "select_from_groups",
    "SearchFromGroupsReq": "search_from_groups",
    "SelectCountersReq": "select_counters",
    "OrderReq": "order_triggers",
    "GatherReq": "gather",
}
INTERESTING_NAME_KEYWORDS = (
    "prompt",
    "help",
    "message",
    "label",
    "text",
    "context",
    "target",
    "option",
    "choice",
    "select",
    "group",
    "search",
    "source",
    "zone",
    "attack",
    "block",
    "counter",
    "id",
    "count",
    "min",
    "max",
    "amount",
    "total",
    "value",
)


class AdapterError(Exception):
    """A reflection batch failed; the message is reported as the command error."""


class LiveTurnBehind(AdapterError):
    """Arena's live turn is still behind the command's expected turn."""

    def __init__(self, what: str, wanted: int, live: int) -> None:
        super().__init__(f"Stale {what}: Arena is still on turn {live}, behind the expected turn {wanted}")
        self.wanted = wanted
        self.live = live


class RequestAlreadyAnswered(AdapterError):
    """The pending request's OnSubmit is spent: it was answered (by the player) before our submit.

    BaseUserRequest.Submit() invokes OnSubmit and then sets it to null; the
    request object stays pending until the GRE's reply replaces it. Invoking
    the null delegate fails in the probe as "ref does not name an object
    result" (2026-10-18 Seam Rip 18:26:12, Silkguard 21:15:30).
    """


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


def attacker_nodes(request: dict, member: str) -> list[dict]:
    """Resolve shared list entries only by their exact native object handle."""
    expanded = {
        handle(node): node
        for name in ("QualifiedAttackers", "Attackers")
        for node in items(field(request, name))
        if handle(node) is not None and field(node, "AttackerInstanceId") is not None
    }
    return [expanded.get(handle(node), node) for node in items(field(request, member))]


def guid(node: Any) -> str:
    fields = [field(node, name) for name in "abcdefghijk"]
    if any(type(value) is not int for value in fields):
        raise AdapterError("Incomplete deck identity")
    widths = (32, 16, 16, 8, 8, 8, 8, 8, 8, 8, 8)
    encoded = "".join(
        f"{value & ((1 << width) - 1):0{width // 4}x}" for value, width in zip(fields, widths, strict=True)
    )
    return str(uuid.UUID(hex=encoded))


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


# The casting-time parent request (the one with ChildRequests): Arena 2026.64.50 renamed it from
# CastingTimeOptionRequest, now the children's base class, to CastingTimeOptionsRequest
# (2026-10-20 10:01: no shaper matched, Iroh's Demonstration's modes went to MANUAL REQUIRED).
CASTING_TIME_PARENTS = ("CastingTimeOptionsRequest", "CastingTimeOptionRequest")


def REF(index: int) -> dict:
    return {"ref": index}


def LIST(values: list[dict]) -> dict:
    return {"list": list(values)}


def UINTS(values: list[int]) -> dict:
    """Adapter-only argument expanded into a managed uint array in the batch."""
    return {"uint_array": [int(value) for value in values]}


class _Ops:
    """Builds a reflect batch whose ops can refer to earlier results by index."""

    def __init__(self) -> None:
        self.ops: list[dict[str, Any]] = []
        self.submit_invokes: set[int] = set()

    def add(self, op: str, **fields: Any) -> dict:
        self.ops.append({"op": op, **fields})
        return REF(len(self.ops) - 1)

    def pending(self, depth: int = 0, max_items: int | None = None) -> dict:
        if max_items is None:
            return self.add("pending", depth=depth, skip=SNAPSHOT_SKIP)
        return self.add("pending", depth=depth, skip=SNAPSHOT_SKIP, max_items=max_items)

    def get(self, target: dict, member: str, depth: int = 0, optional: bool = False) -> dict:
        return self.add("get", target=target, member=member, depth=depth, optional=optional)

    def call(self, target: dict, method: str, *args: dict, depth: int = 0) -> dict:
        resolved = [self.uint_array(arg["uint_array"]) if "uint_array" in arg else arg for arg in args]
        return self.add("call", target=target, method=method, args=resolved, depth=depth)

    def uint_array(self, values: list[int]) -> dict:
        """Build through managed scalar writes, including with an older loaded probe.

        Old probes stride primitive LIST arrays as pointers: [480, 475] reaches
        Arena as [480, 0]. A fresh protobuf collection supplies the closed generic
        type without modifying any pending request. Exact capacity is necessary
        because some methods require uint[] rather than IEnumerable<uint>.
        Keep the array dump shallow: old probes also misread primitive arrays.
        """
        collection = self.get(self.new(MESSAGING + "SearchResp"), "ItemsFound")
        for value in values:
            self.call(collection, "Add", U(value))
        self.set(collection, "Capacity", I(len(values)))
        self.expect_member(collection, "Count", len(values))
        array = self.get(collection, "array")
        self.expect_member(array, "Length", len(values))
        return array

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
        self.submit_invokes.add(self.call(delegate, "Invoke", message)["ref"])


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
    def __init__(
        self,
        send: SendFn,
        runtime: str = "il2cpp-macos",
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._send = send
        self._runtime = runtime
        self._sleep = sleep
        self._clock = clock

    # -- transport ---------------------------------------------------------

    def _run(self, ops: _Ops, timeout: float | None = None) -> list[Any]:
        response = self._send({"action": "reflect_batch", "ops": ops.ops}, timeout)
        if not response.get("ok"):
            error = response.get("error") or "reflect_batch failed"
            if response.get("failed_op") in ops.submit_invokes and "does not name an object" in error:
                raise RequestAlreadyAnswered(
                    "the request was already answered (its OnSubmit is spent): the player acted first"
                )
            raise AdapterError(error)
        return response.get("results") or []

    def snapshot(self, depth: int = SNAPSHOT_DEPTH, timeout: float | None = None) -> Snapshot:
        ops = _Ops()
        request = ops.pending(depth, max_items=SNAPSHOT_MAX_ITEMS)
        ops.get(request, "OriginalMessage", depth=1, optional=True)
        for name in SNAPSHOT_GETTERS:
            ops.get(request, name, depth=1, optional=True)
        return Snapshot(self._run(ops, timeout))

    def _submit(
        self, snapshot: Snapshot, build: Callable[[_Ops, dict], None], timeout: float | None
    ) -> list[Any]:
        """Run `build`'s ops only if the snapshot's request is still the pending one."""
        ops = _Ops()
        request = ops.pending(0)
        ops.expect_pending(H(snapshot.handle))
        build(ops, request)
        return self._run(ops, timeout)

    def handles(self, command: dict[str, Any]) -> bool:
        if self._runtime == BEPINEX_RUNTIME:
            return command.get("action") in BEPINEX_REFLECT_COMMANDS
        return command.get("action") not in PASSTHROUGH

    @property
    def plays_matches(self) -> bool:
        """True when in-game commands run here; the BepInEx plugin plays its own."""
        return self._runtime != BEPINEX_RUNTIME

    def handle(self, command: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        action = str(command.get("action") or "")
        handler = getattr(self, "_cmd_" + action, None)
        if handler is None:
            return {
                "ok": False,
                "unsupported": True,
                "error": f"not supported by the macOS IL2CPP bridge: {action}",
            }
        try:
            result = handler(command, timeout)
        except AdapterError as exc:
            logger.info("mac bridge %s failed: %s", action, exc)
            if isinstance(exc, RequestAlreadyAnswered):
                return {"ok": False, "error": str(exc), "already_answered": True}
            return {"ok": False, "error": str(exc)}
        if action not in self._READ_ONLY_COMMANDS and action != "submit_action":
            # submit_action logs its own line; everything else that changes the
            # client is logged here so the coaching log shows each "hand" the
            # bridge lent Arena (2026-10-09: between-game navigation was invisible).
            summary = (
                {k: v for k, v in result.items() if k in self._LOGGED_RESULT_KEYS}
                if isinstance(result, dict)
                else result
            )
            logger.info("mac bridge %s: %s", action, summary)
        return result

    _READ_ONLY_COMMANDS = frozenset(
        {"ping", "get_pending_actions", "get_game_state", "get_timer_state", "get_match_info",
         "get_screen", "get_draft_state", "get_event_page", "get_deck_editor", "get_survey",
         "get_match_end", "get_limited_pool", "preview_draft_pick", "get_replay_status", "list_replays"}
    )  # fmt: skip
    _LOGGED_RESULT_KEYS = frozenset(
        {"ok", "submitted_type", "survey_skipped", "waiting", "already_home", "step", "event_name",
         "module", "scope", "game_number", "turn", "gems_added", "deck_id", "error"}
    )  # fmt: skip

    def _require(self, timeout: float | None, *classes: str) -> Snapshot:
        snapshot = self.snapshot(timeout=timeout)
        if snapshot.request is None:
            raise AdapterError("No pending interaction")
        if classes and snapshot.request_class not in classes:
            raise AdapterError(f"Pending is {snapshot.request_class}, not {'/'.join(classes)}")
        return snapshot

    # -- draft events --------------------------------------------------------
    # Hands for limited events, through the client's own UI handlers (see
    # re-output DraftContentController / DeckBuilderWidget / MainButtonComponent):
    # a pick is ReserveCardAndLockIn (the double-click path, which commits the
    # pick to the deck manager), a deck is DeckBuilderModel edits plus the Done
    # button, and the event page's Play button runs MainButton_OnPlayButtonClicked,
    # which has no Join/Pay case. Entry-fee paths are never called.

    def _find(self, class_name: str, timeout: float | None) -> int | None:
        ops = _Ops()
        ops.add("find", **{"class": class_name}, depth=0, optional=True)
        return handle(self._run(ops, timeout)[0])

    _SCREENS = (
        ("match_end", "MatchEndScene"),
        ("draft", DRAFT_CONTROLLER),
        ("deck_builder", "DeckBuilderWidget"),
        ("event_page", EVENT_PAGE_CONTROLLER),
        ("home", "HomePageContentController"),
        ("sealed_open", SEALED_OPEN),
        # Only found while shown: the prefab is instantiated inactive at
        # MatchEndScene.Init and FindAnyObjectByType skips inactive objects.
        ("survey", SURVEY),
    )

    def _cmd_get_screen(self, command: dict, timeout: float | None) -> dict:
        """Which wrapper screens are active, in one read-only batch."""
        ops = _Ops()
        refs = [ops.add("find", **{"class": cls}, depth=0, optional=True) for _key, cls in self._SCREENS]
        values = self._run(ops, timeout)
        found = {
            key: bool(handle(values[ref["ref"]]))
            for (key, _cls), ref in zip(self._SCREENS, refs, strict=True)
        }
        if found["deck_builder"]:
            ops = _Ops()
            active = ops.get(H(handle(values[refs[2]["ref"]])), "_isActive", optional=True)
            found["deck_builder"] = bool(self._run(ops, timeout)[active["ref"]])
        return {"ok": True, **found}

    def _cmd_get_draft_state(self, command: dict, timeout: float | None) -> dict:
        found = self._find(DRAFT_CONTROLLER, timeout)
        if not found:
            return {"ok": True, "is_open": False}
        controller = H(found)
        ops = _Ops()
        ok_to_pick = ops.get(controller, "_okToPickCard")
        holder = ops.get(controller, "_draftPackHolder", optional=True)
        animating = ops.get(holder, "IsAnimating", optional=True)
        pod = ops.get(controller, "DraftPod", optional=True)
        to_take = ops.get(pod, "PickNumCardsToTake", optional=True)
        manager = ops.get(controller, "_draftDeckManager", optional=True)
        reserved = ops.call(manager, "ReservedCardCount")
        views = ops.get(holder, "CardViews", depth=1, optional=True)
        values = self._run(ops, timeout)
        pod_node = values[pod["ref"]]
        mode = (
            "Human" if "Human" in short_class(pod_node) else "Bot" if "Bot" in short_class(pod_node) else ""
        )
        view_nodes = [node for node in items(values[views["ref"]]) if handle(node)]

        ops = _Ops()
        refs = []
        for node in view_nodes:
            card = ops.get(H(handle(node)), "Card", optional=True)
            refs.append(
                (handle(node), ops.get(card, "GrpId", optional=True), ops.get(card, "TitleId", optional=True))
            )
        seconds = pack_ref = pick_ref = None
        if mode == "Human" and handle(pod_node):
            # PickInfo fields verified in the live client's metadata (2026-10-05).
            pick_info = ops.get(H(handle(pod_node)), "_currentPickInfo", optional=True)
            pack_ref = ops.get(pick_info, "SelfPack", optional=True)
            pick_ref = ops.get(pick_info, "SelfPick", optional=True)
            seconds = ops.get(H(handle(pod_node)), "PickSecondsRemaining", optional=True)
        elif mode == "Bot" and handle(pod_node):
            pack_ref = ops.get(H(handle(pod_node)), "_currentPack", optional=True)
            pick_ref = ops.get(H(handle(pod_node)), "_currentPick", optional=True)
        details = self._run(ops, timeout) if ops.ops else []

        pack_views = [
            {"view": view, "grp_id": num(details[grp["ref"]]), "title_id": num(details[title["ref"]])}
            for view, grp, title in refs
            if num(details[grp["ref"]]) > 0
        ]
        response = {
            "ok": True,
            "is_open": True,
            "controller": found,
            "draft_mode": mode,
            "ok_to_pick": bool(values[ok_to_pick["ref"]]),
            "animating": bool(values[animating["ref"]]),
            "reserved_count": num(values[reserved["ref"]]),
            "pick_num_cards_to_take": num(values[to_take["ref"]]) or 1,
            "pack_views": pack_views,
            "pack_cards": [view["grp_id"] for view in pack_views],
        }
        if seconds is not None:
            response["pack_number"] = num(details[pack_ref["ref"]])
            response["pick_number"] = num(details[pick_ref["ref"]])
            response["pick_seconds_remaining"] = num(details[seconds["ref"]])
        elif pack_ref is not None:
            # Bot pods count from zero; report 1-based like Draft.Notify.
            response["pack_number"] = num(details[pack_ref["ref"]]) + 1
            response["pick_number"] = num(details[pick_ref["ref"]]) + 1
        return response

    def _cmd_submit_draft_pick(self, command: dict, timeout: float | None) -> dict:
        wanted = [card for card in command.get("cards") or [] if int(card.get("grp_id") or 0) > 0]
        state = self._cmd_get_draft_state({}, timeout)
        if not state.get("is_open"):
            raise AdapterError("No draft pick screen is open")
        if not state["ok_to_pick"] or state["animating"]:
            raise AdapterError("The draft pack is not ready to pick yet")
        if state["reserved_count"]:
            raise AdapterError("Cards are already reserved in this pack; not mixing with a manual pick")
        if len(wanted) != state["pick_num_cards_to_take"]:
            raise AdapterError(
                f"This pick takes {state['pick_num_cards_to_take']} card(s), not {len(wanted)}"
            )
        chosen: list[dict] = []
        for card in wanted:
            free = [view for view in state["pack_views"] if view not in chosen]
            title_id = int(card.get("title_id") or 0)
            match = next((v for v in free if v["grp_id"] == int(card["grp_id"])), None) or next(
                (v for v in free if title_id and v["title_id"] == title_id), None
            )
            if match is None:
                raise AdapterError(f"Card {card['grp_id']} is not in the current pack")
            chosen.append(match)
        ops = _Ops()
        controller = H(state["controller"])
        ops.expect_member(controller, "_okToPickCard", True)
        for view in chosen:
            ops.call(controller, "ReserveCardAndLockIn", H(view["view"]), {"null": True})
        self._run(ops, timeout)
        return {"ok": True, "submitted_type": "DraftPick", "grp_ids": [view["grp_id"] for view in chosen]}

    # A narrated pick: select first, confirm later. preview_draft_pick runs the
    # single-click path (ToggleCardReservation: pick tag on, Confirm enabled)
    # so viewers see the choice while it is explained; confirm_draft_pick
    # presses Confirm (HandleOnConfirmPickButtonClicked, which drafts the
    # reserved cards only when as many are reserved as the pick takes). The
    # double-click handler is never called: two clicks within 0.5 s lock a card
    # in. An unconfirmed selection is harmless: when the pick clock runs out,
    # CardsToAutopick drafts reserved cards first. Members verified in live
    # IL2CPP metadata 2026-10-07: DraftContentController.ToggleCardReservation/1,
    # HandleOnConfirmPickButtonClicked/0, get_NumberOfCardsCurrentlySelected/0,
    # get_AtMaxReservedCards/0; DraftDeckManager.IsCardAlreadyReserved/1.

    def _ready_pick(self, command: dict, timeout: float | None) -> tuple[dict, list[dict]]:
        """The open, pickable draft state and the pack views for the wanted cards."""
        wanted = [card for card in command.get("cards") or [] if int(card.get("grp_id") or 0) > 0]
        state = self._cmd_get_draft_state({}, timeout)
        if not state.get("is_open"):
            raise AdapterError("No draft pick screen is open")
        if not state["ok_to_pick"] or state["animating"]:
            raise AdapterError("The draft pack is not ready to pick yet")
        if len(wanted) != state["pick_num_cards_to_take"]:
            raise AdapterError(
                f"This pick takes {state['pick_num_cards_to_take']} card(s), not {len(wanted)}"
            )
        chosen: list[dict] = []
        for card in wanted:
            free = [view for view in state["pack_views"] if view not in chosen]
            title_id = int(card.get("title_id") or 0)
            match = next((v for v in free if v["grp_id"] == int(card["grp_id"])), None) or next(
                (v for v in free if title_id and v["title_id"] == title_id), None
            )
            if match is None:
                raise AdapterError(f"Card {card['grp_id']} is not in the current pack")
            chosen.append(match)
        return state, chosen

    def _reserved_views(self, state: dict, timeout: float | None) -> set[int]:
        """Read-only: handles of the pack's card views the player has selected."""
        ops = _Ops()
        manager = ops.get(H(state["controller"]), "_draftDeckManager")
        refs = [
            (view["view"], ops.call(manager, "IsCardAlreadyReserved", H(view["view"])))
            for view in state["pack_views"]
        ]
        values = self._run(ops, timeout)
        return {view for view, ref in refs if num(values[ref["ref"]])}

    def _cmd_preview_draft_pick(self, command: dict, timeout: float | None) -> dict:
        """Select the pick's cards without drafting them; idempotent."""
        state, chosen = self._ready_pick(command, timeout)
        mine = {view["view"] for view in chosen}
        reserved = self._reserved_views(state, timeout)
        if reserved - mine:
            raise AdapterError("Other cards are selected in this pack; not overriding a manual selection")
        missing = [view for view in chosen if view["view"] not in reserved]
        if missing:
            ops = _Ops()
            controller = H(state["controller"])
            ops.expect_member(controller, "_okToPickCard", True)
            # A toggle deselects a selected card: act only on the selection just read.
            ops.expect_member(controller, "NumberOfCardsCurrentlySelected", len(reserved))
            for view in missing:
                ops.call(controller, "ToggleCardReservation", H(view["view"]))
            self._run(ops, timeout)
        if not mine <= self._reserved_views(state, timeout):
            raise AdapterError("The cards did not stay selected")
        return {
            "ok": True,
            "previewed": True,
            "newly_selected": len(missing),
            "grp_ids": [view["grp_id"] for view in chosen],
        }

    def _cmd_confirm_draft_pick(self, command: dict, timeout: float | None) -> dict:
        """Press Confirm for exactly the cards preview_draft_pick selected."""
        state, chosen = self._ready_pick(command, timeout)
        if self._reserved_views(state, timeout) != {view["view"] for view in chosen}:
            raise AdapterError("The selected cards changed; not confirming a different selection")
        ops = _Ops()
        controller = H(state["controller"])
        ops.expect_member(controller, "_okToPickCard", True)
        ops.expect_member(controller, "AtMaxReservedCards", True)
        ops.call(controller, "HandleOnConfirmPickButtonClicked")
        still_open = ops.get(controller, "_okToPickCard")
        values = self._run(ops, timeout)
        if num(values[still_open["ref"]]):
            raise AdapterError("Confirm did not draft the selected cards")
        return {
            "ok": True,
            "submitted_type": "DraftPickConfirm",
            "grp_ids": [view["grp_id"] for view in chosen],
        }

    def _limited_editor(self, timeout: float | None) -> tuple[dict, dict]:
        """The open, editable limited deck builder's widget and model handles."""
        found = self._find("DeckBuilderWidget", timeout)
        if not found:
            raise AdapterError("The deck builder is not open")
        widget = H(found)
        ops = _Ops()
        ops.expect_member(widget, "_isActive", True)
        context = ops.get(widget, "Context")
        ops.expect_member(context, "IsLimited", True)
        ops.expect_member(context, "IsSideboarding", False)
        ops.expect_member(context, "IsReadOnly", False)
        provider = ops.get(widget, "ModelProvider")
        model = ops.get(provider, "_model", depth=0)
        values = self._run(ops, timeout)
        model_handle = handle(values[model["ref"]])
        if not model_handle:
            raise AdapterError("The limited deck model is unavailable")
        return widget, H(model_handle)

    def _cmd_get_limited_pool(self, command: dict, timeout: float | None) -> dict:
        """Main deck, sideboard and how many of each candidate basic the pool holds."""
        widget, model = self._limited_editor(timeout)
        ops = _Ops()
        deck = ops.call(model, "GetServerModel")
        main = ops.get(deck, "mainDeck", depth=2)
        side = ops.get(deck, "sideboard", depth=2)
        candidates = [int(grp_id) for grp_id in command.get("basic_candidates") or [] if int(grp_id) > 0]
        quantities = [ops.call(model, "GetQuantityInCardPool", U(grp_id)) for grp_id in candidates]
        values = self._run(ops, timeout)

        def entries(reference: dict) -> list[dict]:
            return [
                {"grp_id": num(field(entry, "Id")), "count": num(field(entry, "Quantity"))}
                for entry in items(values[reference["ref"]])
                if num(field(entry, "Id")) > 0 and num(field(entry, "Quantity")) > 0
            ]

        return {
            "ok": True,
            "main_deck": entries(main),
            "sideboard": entries(side),
            "basics_in_pool": {
                str(grp_id): num(values[quantity["ref"]])
                for grp_id, quantity in zip(candidates, quantities, strict=True)
                if num(values[quantity["ref"]]) > 0
            },
        }

    def _cmd_set_limited_deck(self, command: dict, timeout: float | None) -> dict:
        """Replace the limited main deck with exact grpId counts, then read it back."""
        target = {
            int(e["grp_id"]): int(e["count"]) for e in command.get("main_deck") or [] if int(e["count"]) > 0
        }
        if sum(target.values()) < 40:
            raise AdapterError("A limited deck needs at least 40 cards")
        current = self._cmd_get_limited_pool({}, timeout)
        widget, model = self._limited_editor(timeout)
        ops = _Ops()
        ops.expect_member(widget, "_isActive", True)
        for entry in current["main_deck"]:
            ops.call(model, "RemoveCardFromMainDeck", U(entry["grp_id"]), U(entry["count"]))
        for grp_id, count in target.items():
            ops.call(model, "AddCardToMainDeck", U(grp_id), U(count))
        ops.call(model, "UpdateMainDeck")
        self._run(ops, timeout)
        result = self._cmd_get_limited_pool({}, timeout)
        actual = {entry["grp_id"]: entry["count"] for entry in result["main_deck"]}
        missing = {grp_id: count for grp_id, count in target.items() if actual.get(grp_id, 0) != count}
        return {"ok": not missing, "main_deck": result["main_deck"], "mismatched": missing}

    def _cmd_submit_limited_deck(self, command: dict, timeout: float | None) -> dict:
        widget, _model = self._limited_editor(timeout)
        ops = _Ops()
        ops.expect_member(widget, "_isActive", True)
        ops.call(widget, "DoneButton_OnClick")
        self._run(ops, timeout)
        return {"ok": True, "submitted_type": "LimitedDeckDone"}

    _EVENT_PLAY_MODULES = {
        "Draft",
        "HumanDraft",
        "DeckSelect",
        "TransitionToMatches",
        "WinLossGate",
        "WinNoGate",
        "ClaimPrize",
    }

    # -- deck library ---------------------------------------------------------
    # WrapperController.Instance.DecksManager.GetAllCachedDecks(): every
    # constructed deck with its contents, filled by the client at login (see
    # re-output DeckDataProvider: GetFullDeck is a cache read, no request).
    _DECKS_PER_BATCH = 4
    _PILE_NAMES = {"Main": "main", "Sideboard": "sideboard", "CommandZone": "command_zone", "Companions": "companions"}

    def _cmd_get_decks(self, command: dict, timeout: float | None) -> dict:
        ops = _Ops()
        wrapper = ops.add("static", **{"class": "WrapperController"}, member="Instance", depth=0)
        manager = ops.get(wrapper, "DecksManager", optional=True)
        decks = ops.add("call", target=manager, method="GetAllCachedDecks", depth=1, optional=True)
        values = self._run(ops, timeout)
        if handle(values[wrapper["ref"]]) is None:
            # The front-end wrapper is unloaded during a duel (Instance = null
            # in OnDestroy, live 2026-10-11 10:46); the home screen has it.
            return {"ok": False, "error": "Arena's deck list is only readable outside a match (home screen)"}
        handles = [handle(node) for node in items(values[decks["ref"]]) if handle(node)]
        out: list[dict] = []
        for start in range(0, len(handles), self._DECKS_PER_BATCH):
            chunk = handles[start : start + self._DECKS_PER_BATCH]
            ops = _Ops()
            refs = []
            for deck_handle in chunk:
                summary = ops.get(H(deck_handle), "Summary", depth=2)
                fmt = ops.get(summary, "Format", optional=True)
                contents = ops.get(H(deck_handle), "Contents", optional=True)
                piles = ops.add("get", target=contents, member="Piles", depth=6, optional=True, max_items=256)
                refs.append((summary, fmt, piles))
            values = self._run(ops, timeout)
            for summary, fmt, piles in refs:
                node = values[summary["ref"]]
                if not isinstance(node, dict):
                    continue
                try:
                    deck_id = guid(field(node, "DeckId"))
                except AdapterError:
                    continue
                record: dict[str, Any] = {
                    "deck_id": deck_id,
                    "name": str(field(node, "Name") or ""),
                    "format": str(values[fmt["ref"]] or ""),
                    "main": [],
                    "sideboard": [],
                    "command_zone": [],
                    "companions": [],
                }
                for entry in items(field(values[piles["ref"]], "entries")):
                    pile = self._PILE_NAMES.get(enum_name(field(entry, "key")))
                    if pile is None:
                        continue
                    record[pile] = [
                        {"grp_id": num(field(card, "Id")), "count": num(field(card, "Quantity"))}
                        for card in items(field(entry, "value"))
                        if num(field(card, "Id")) > 0 and num(field(card, "Quantity")) > 0
                    ]
                out.append(record)
        return {"ok": True, "decks": out}

    def _cmd_get_event_page(self, command: dict, timeout: float | None) -> dict:
        found = self._find(EVENT_PAGE_CONTROLLER, timeout)
        if not found:
            return {"ok": True, "is_open": False}
        ops = _Ops()
        context = ops.get(H(found), "_currentEventContext")
        player_event = ops.get(context, "PlayerEvent")
        course = ops.get(player_event, "CourseData")
        module = ops.get(course, "CurrentModule")
        info = ops.get(player_event, "EventInfo")
        name = ops.get(info, "InternalEventName")
        values = self._run(ops, timeout)
        return {
            "ok": True,
            "is_open": True,
            "event_name": str(values[name["ref"]] or ""),
            "module": enum_name(values[module["ref"]]),
        }

    def _cmd_event_play(self, command: dict, timeout: float | None) -> dict:
        """Press the event page's Play button, only in a stage that cannot pay."""
        page = self._cmd_get_event_page({}, timeout)
        if not page.get("is_open"):
            raise AdapterError("The event page is not open")
        expected = str(command.get("event_name") or "")
        if expected and page["event_name"] != expected:
            raise AdapterError(f"Event page shows {page['event_name']}, not {expected}")
        if page["module"] not in self._EVENT_PLAY_MODULES:
            raise AdapterError(f"Event stage {page['module'] or 'unknown'} is not one autoplay may advance")
        button = self._find(EVENT_MAIN_BUTTON, timeout)
        if not button:
            raise AdapterError("The event page has no Play button")
        ops = _Ops()
        delegate = ops.get(H(button), "PlayButton_OnClick")
        ops.call(delegate, "Invoke")
        self._run(ops, timeout)
        return {
            "ok": True,
            "submitted_type": "EventPlay",
            "module": page["module"],
            "event_name": page["event_name"],
        }

    def _cmd_finish_sealed_open(self, command: dict, timeout: float | None) -> dict:
        """Reveal the granted sealed pool, then press Done to reach the deck builder.

        One button per call: Open while packs are closed, Done once the reveal
        has finished. Neither path can purchase anything; the pool was already
        granted when the player entered the event.
        """
        found = self._find(SEALED_OPEN, timeout)
        if not found:
            raise AdapterError("The sealed pool opening screen is not open")
        ops = _Ops()
        screen = H(found)
        buttons = {}
        for name in ("_openButton", "_doneButton"):
            button = ops.get(screen, name, optional=True)
            shown = ops.get(ops.get(button, "gameObject", optional=True), "activeSelf", optional=True)
            buttons[name] = (shown, ops.get(button, "Interactable", optional=True))
        gems = ops.get(screen, "_gemsAdded", optional=True)
        values = self._run(ops, timeout)

        def ready(name: str) -> bool:
            shown, interactable = buttons[name]
            return bool(values[shown["ref"]]) and bool(values[interactable["ref"]])

        step = "done" if ready("_doneButton") else "open" if ready("_openButton") else "revealing"
        if step != "revealing":
            ops = _Ops()
            ops.call(screen, "DoneButton_OnClick" if step == "done" else "OpenButton_OnClick")
            self._run(ops, timeout)
        return {"ok": True, "step": step, "gems_added": int(values[gems["ref"]] or 0)}

    def _cmd_leave_match(self, command: dict, timeout: float | None) -> dict:
        """Leave the match result screen (the client returns to the event page)."""
        found = self._find("MatchEndScene", timeout)
        if not found:
            raise AdapterError("No match result screen is open")
        if self._skip_open_survey(timeout):
            # LeaveMatch is a no-op until the survey closes; the caller retries.
            return {"ok": True, "survey_skipped": True}
        if not self._match_end_state(timeout)["exit_ready"]:
            return {"ok": True, "waiting": True}
        ops = _Ops()
        ops.call(H(found), "LeaveMatch")
        self._run(ops, timeout)
        return {"ok": True}

    # -- match result screen -------------------------------------------------
    # MatchSceneManager.AnimateMatchEnd (re-output/Core/MatchSceneManager.cs)
    # fetches the match results (up to 8 s), plays the rank animation (up to
    # 10 s), shows the survey if drawn (up to 60 s), and only then subscribes
    # ExitMatchCompleted and calls EnableEndOfMatchControls, which activates
    # _exitMatchBackgroundButton, the click-anywhere surface. Until then a click
    # on the result screen and LeaveMatch (ExitMatchCompleted?.Invoke()) do
    # nothing: 2026-10-20 10:05:48-10:05:59 and 10:13:37-10:13:48 auto-queue's
    # three centre clicks on VICTORY each had no effect and navigation paused.
    # Members verified in live IL2CPP metadata 2026-10-20 (Arena 2026.64.50).

    def _match_end_state(self, timeout: float | None) -> dict:
        """Read-only: is the result screen open, and does leaving it work yet?"""
        ops = _Ops()
        scene = ops.add("find", **{"class": "MatchEndScene"}, depth=0, optional=True)
        listener = ops.get(scene, "ExitMatchCompleted", optional=True)
        background = ops.get(scene, "_exitMatchBackgroundButton", optional=True)
        shown = ops.get(ops.get(background, "gameObject", optional=True), "activeInHierarchy", optional=True)
        values = self._run(ops, timeout)
        found = bool(handle(values[scene["ref"]]))
        return {
            "ok": True,
            "is_open": found,
            "exit_ready": found and bool(handle(values[listener["ref"]])),
            "click_ready": found and values[shown["ref"]] is True,
        }

    def _cmd_get_match_end(self, command: dict, timeout: float | None) -> dict:
        return self._match_end_state(timeout)

    # -- post-match survey ---------------------------------------------------
    # "Did you have fun in the match?" (loc DuelScene/EndMatch/PostMatchSurvey_Ask,
    # buttons: good face, bad face, "Skip", plus a full-screen click shield).
    # MatchSceneManager.MatchEndCoroutine activates MatchEndScene.SurveyUI (a
    # GameEndSurvey under _canvasPopup) after the rank animation and waits up to
    # 60 s for FeedbackSubmitted before EnableEndOfMatchControls and before it
    # subscribes ExitMatchCompleted, so LeaveMatch does nothing while the survey
    # is open. GameEndSurvey.Awake wires _buttonSkip.OnClick to
    # FeedbackSubmitted("IntentionallySkipped") + ClearButtonCallbacks; the
    # shield sends "NoFeedback"; ForceClose (the timeout) also sends "NoFeedback".
    # CustomButton.Click runs the same OnClick UnityEvent a pointer click does,
    # gated on Interactable. All members verified in live IL2CPP metadata 2026-10-07.

    def _survey_state(self, timeout: float | None) -> dict:
        """Read-only: is the survey shown and still waiting for an answer?"""
        ops = _Ops()
        survey = ops.add("find", **{"class": SURVEY}, depth=0, optional=True)
        listener = ops.get(survey, "FeedbackSubmitted", optional=True)
        skip = ops.get(survey, "_buttonSkip", optional=True)
        interactable = ops.get(skip, "Interactable", optional=True)
        shown = ops.get(ops.get(skip, "gameObject", optional=True), "activeInHierarchy", optional=True)
        values = self._run(ops, timeout)
        found = bool(handle(values[survey["ref"]]))
        # The scene manager adds its FeedbackSubmitted handler right before
        # showing the survey and removes it once any answer is in.
        awaiting = found and bool(handle(values[listener["ref"]]))
        skip_ready = bool(values[interactable["ref"]]) and values[shown["ref"]] is not False
        return {
            "ok": True,
            "is_open": awaiting,
            "found": found,
            "skip_ready": awaiting and skip_ready,
        }

    def _cmd_get_survey(self, command: dict, timeout: float | None) -> dict:
        return self._survey_state(timeout)

    def _cmd_dismiss_survey(self, command: dict, timeout: float | None) -> dict:
        """Press the survey's Skip button (no rating is sent), then re-check it."""
        state = self._survey_state(timeout)
        if not state["is_open"]:
            raise AdapterError("The post-match survey is not open")
        if not state["skip_ready"]:
            raise AdapterError("The survey's Skip button is not clickable")
        ops = _Ops()
        survey = ops.add("find", **{"class": SURVEY}, depth=0)
        skip = ops.get(survey, "_buttonSkip")
        ops.expect_member(skip, "Interactable", True)
        ops.call(skip, "Click")
        self._run(ops, timeout)
        logger.info("mac bridge: skipped the post-match survey (GameEndSurvey._buttonSkip.Click)")
        after = self._survey_state(timeout)
        return {
            "ok": True,
            "submitted_type": "SurveySkip",
            "feedback": "IntentionallySkipped",
            "closed": not after["is_open"],
        }

    def _skip_open_survey(self, timeout: float | None) -> bool:
        """Skip the survey if it is blocking the result screen; False when absent."""
        state = self._survey_state(timeout)
        if not state["is_open"]:
            return False
        if not state["skip_ready"]:
            raise AdapterError("The post-match survey is open but its Skip button is not clickable")
        self._cmd_dismiss_survey({}, timeout)
        return True

    # -- live game: shared guards for concede and emotes ----------------------

    @staticmethod
    def _live_game_reads(ops: _Ops) -> dict[str, dict]:
        """Read-only ops that identify the game in play (every one optional)."""
        refs: dict[str, dict] = {}
        refs["manager"] = manager = ops.add("find", **{"class": "GameManager"}, depth=0, optional=True)
        refs["match"] = match = ops.get(manager, "MatchManager", optional=True)
        refs["match_state"] = ops.get(match, "MatchState", optional=True)
        refs["scene"] = ops.get(
            ops.get(manager, "MatchSceneManager", optional=True), "Current", optional=True
        )
        refs["game"] = game = ops.get(manager, "CurrentGameState", optional=True)
        refs["stage"] = ops.get(game, "Stage", optional=True)
        refs["turn"] = ops.get(game, "GameWideTurn", optional=True)
        # The newest state received, ahead of CurrentGameState while the duel
        # scene plays states back (GameManager.Update -> SetLatestGameState).
        # Only ever used to refuse sooner; not verified in IL2CPP metadata, so
        # an absent member reads as null and changes nothing.
        refs["latest_turn"] = ops.get(
            ops.get(manager, "LatestGameState", optional=True), "GameWideTurn", optional=True
        )
        refs["info"] = info = ops.get(game, "GameInfo", optional=True)
        refs["match_id"] = ops.get(info, "MatchID", optional=True)
        refs["game_number"] = ops.get(info, "GameNumber", optional=True)
        refs["gre"] = gre = ops.get(match, "GreInterface", optional=True)
        refs["disposed"] = ops.get(gre, "_disposed", optional=True)
        return refs

    @staticmethod
    def _live_game(
        values: list[Any], refs: dict[str, dict], command: dict, what: str
    ) -> tuple[str, int, int]:
        """(match id, game number, turn) of a game in play, or AdapterError.

        A game in progress in the duel scene, stage Play, a live GreInterface,
        and the command's expected match/game/turn when it names them. The
        match and game must match exactly; a turn ahead of the expected one is
        stale, and one still behind it raises LiveTurnBehind (the caller may
        wait for Arena to catch up, see _await_live_game).
        """

        def value(name: str) -> Any:
            index = refs[name]["ref"]
            return values[index] if index < len(values) else None

        if not handle(value("manager")):
            raise AdapterError("Not in a match (no GameManager)")
        if not handle(value("match")):
            raise AdapterError("No MatchManager")
        if enum_name(value("match_state")) != "GameInProgress":
            raise AdapterError(
                f"Match state is {enum_name(value('match_state')) or 'unknown'}, not GameInProgress"
            )
        if enum_name(value("scene")) != "DuelScene":
            raise AdapterError(f"Not in the duel scene ({enum_name(value('scene')) or 'unknown'})")
        if enum_name(value("stage")) != "Play":
            raise AdapterError(f"Game stage is {enum_name(value('stage')) or 'unknown'}, not Play")
        if not handle(value("gre")) or value("disposed") is not False:
            raise AdapterError("No live GreInterface")
        live_match = value("match_id")
        live_turn = num(value("turn"), -1)
        live_game = num(value("game_number"), -1)
        if not isinstance(live_match, str) or not live_match or live_turn <= 0 or live_game <= 0:
            raise AdapterError("Match, game or turn unknown")
        wanted_match = command.get("expected_match_id")
        if wanted_match and str(wanted_match) != live_match:
            raise AdapterError(f"Stale {what}: the match changed")
        wanted = command.get("expected_game_number")
        if wanted not in (None, "", 0, -1) and num(wanted, -1) != live_game:
            raise AdapterError(f"Stale {what}: the game changed ({wanted} -> {live_game})")
        wanted = command.get("expected_turn")
        if wanted not in (None, "", 0, -1):
            newest = max(live_turn, num(value("latest_turn"), -1))
            if newest > num(wanted, -1):
                raise AdapterError(f"Stale {what}: the turn changed ({wanted} -> {newest})")
            if live_turn < num(wanted, -1):
                raise LiveTurnBehind(what, num(wanted, -1), live_turn)
        return live_match, live_game, live_turn

    def _await_live_game(
        self,
        command: dict,
        what: str,
        timeout: float | None,
        extra: Callable[[_Ops, dict[str, dict]], dict[str, dict]] | None = None,
    ) -> tuple[list[Any], dict[str, dict], tuple[str, int, int]]:
        """The read-only live-game batch (plus ``extra``'s reads) and its guards.

        Arena's duel scene sets ``GameManager.CurrentGameState`` only when it
        starts playing a state back (GameStatePlaybackController.StartPlayback),
        behind its animations, so GameWideTurn can trail Player.log, where the
        coach's expected turn comes from, by seconds. 2026-10-19: two
        auto-concedes were refused as "Stale concede: the turn changed (15 ->
        14)" and "(14 -> 13)" while the log had been on the newer turn for
        3.0 s and 1.5 s. A turn ahead of the expected one is still refused at
        once (the game moved past the evidence); one behind it is re-read every
        LIVE_TURN_POLL_S until it catches up, for up to LIVE_TURN_CATCH_UP_S
        and never past the command's expires_in_ms, then refused. Mirrors
        Plugin.CheckExpectedTurn.
        """
        budget = LIVE_TURN_CATCH_UP_S
        expires_ms = num(command.get("expires_in_ms"), 0)
        if expires_ms > 0:
            budget = min(budget, expires_ms / 1000.0 - LIVE_TURN_SEND_MARGIN_S)
        started = self._clock()
        behind: LiveTurnBehind | None = None
        while True:
            ops = _Ops()
            refs = self._live_game_reads(ops)
            if extra is not None:
                refs.update(extra(ops, refs))
            values = self._run(ops, timeout)
            waited = self._clock() - started
            try:
                live = self._live_game(values, refs, command, what)
            except LiveTurnBehind as exc:
                if waited >= budget:
                    raise AdapterError(f"{exc} after {waited:.1f}s") from None
                if behind is None:
                    logger.info(
                        "mac bridge %s: Arena is still playing back turn %s (expected %s); "
                        "waiting up to %.1fs for it to catch up",
                        what,
                        exc.live,
                        exc.wanted,
                        budget,
                    )
                behind = exc
                self._sleep(LIVE_TURN_POLL_S)
                continue
            if behind is not None:
                logger.info("mac bridge %s: Arena caught up to turn %s after %.1fs", what, live[2], waited)
            return values, refs, live

    @staticmethod
    def _expect_live_game(ops: _Ops, live_match: str, live_game: int, live_turn: int) -> dict[str, dict]:
        """Guard ops: still that game, in play, on that turn, with a live GreInterface."""
        manager = ops.add("find", **{"class": "GameManager"}, depth=0)
        match = ops.get(manager, "MatchManager")
        ops.expect_member(match, "MatchState", "GameInProgress")
        ops.expect_member(ops.get(manager, "MatchSceneManager"), "Current", "DuelScene")
        game = ops.get(manager, "CurrentGameState")
        ops.expect_member(game, "Stage", "Play")
        ops.expect_member(game, "GameWideTurn", live_turn)
        info = ops.get(game, "GameInfo")
        ops.expect_member(info, "MatchID", live_match)
        ops.expect_member(info, "GameNumber", live_game)
        gre = ops.get(match, "GreInterface")
        ops.expect_member(gre, "_disposed", False)
        return {"manager": manager, "match": match, "gre": gre}

    def _cmd_concede(self, command: dict, timeout: float | None) -> dict:
        """Concede the current game the way Arena's own Concede button does.

        SettingsMenuHost.OnConcedeGameRequested calls
        ``MatchManager.GreInterface.ConcedeGame()``, which queues ConcedeReq
        {Scope = Game}; MatchManager.Update sends it. It needs no pending
        request, so it works on the opponent's turn. ``BaseUserRequest.Concede``
        is not used: it consumes the pending request and defaults to conceding
        the whole match. ``GameManager._gre`` is not used either: MatchManager
        replaces its GreInterface on every new game.

        One read-only batch (re-read while Arena's client is still playing back
        an older turn, see _await_live_game), then one guarded batch that
        re-checks everything in the same frame as the call: a game in progress
        in the duel scene, stage Play, the same match/game/turn, and a live
        GreInterface. Never retried; a main-thread timeout is reported as
        ``outcome_unknown``.
        """
        if str(command.get("scope") or "game").lower() != "game":
            raise AdapterError("Only conceding the current game is supported")
        _values, _refs, (live_match, live_game, live_turn) = self._await_live_game(
            command, "concede", timeout
        )

        ops = _Ops()
        guarded = self._expect_live_game(ops, live_match, live_game, live_turn)
        ops.call(guarded["gre"], "ConcedeGame")
        logger.warning(
            "mac bridge: conceding game %s of %s on turn %s via GreInterface.ConcedeGame",
            live_game,
            live_match,
            live_turn,
        )
        try:
            self._run(ops, timeout)
        except AdapterError as exc:
            if "outcome unknown" in str(exc):
                logger.warning("mac bridge: concede outcome unknown: %s", exc)
                return {"ok": False, "outcome_unknown": True, "error": str(exc)}
            raise
        logger.warning("mac bridge: ConcedeGame called (game %s of %s)", live_game, live_match)
        return {
            "ok": True,
            "submitted_type": "Concede",
            "scope": "Game",
            "match_id": live_match,
            "game_number": live_game,
            "turn": live_turn,
        }

    def _cmd_send_emote(self, command: dict, timeout: float | None) -> dict:
        """Send Arena's "Oops" emote exactly the way a click on the emote wheel does.

        A wheel click runs ``EmoteOptionsController.EmoteClicked(id)``: it looks
        the id up in the wheel (``_temporaryEmotes``, then ``_emotesByIdMap``)
        and fires ``OnEmoteOptionClicked``, whose handler
        ``LocalPlayerDialogController._sendEmoteDataPacket`` calls
        ``UIMessageHandler.TrySendEmote`` -> ``GreInterface.SubmitUIMessage``
        (UIMessage {OnChat {Text = id}} to the opponent's seat), counts the
        use and shows our own speech bubble. "Phrase_Basic_Oops" is the id a
        real Oops click puts on the wire (an opponent's, 2026-10-06 14:00:30).

        Batch 1 (read-only): the concede guards, re-read while Arena catches up
        to the expected turn (_await_live_game); no tutorial (NPEDirector);
        the UIMessageHandler's send delegate targets this game's GreInterface;
        exactly one LocalPlayerDialogController among the handlers of its
        EmoteRecievedCallback. Batch 2 (read-only): that controller uses this
        UIMessageHandler, its wheel is live, and the Oops id is on it
        (``_equippedEmoteOptions``, which the wheel keeps in step with
        ``_emotesByIdMap``); otherwise nothing is sent. Batch 3: the guards
        again, the same wheel entry still holding the Oops id, then
        ``EmoteClicked`` in the same frame. Never retried: a failure at or
        after the click is ``outcome_unknown``.
        """
        kind = str(command.get("emote") or "oops").strip().lower()
        emote_id = EMOTE_IDS.get(kind)
        if emote_id is None:
            raise AdapterError(f"Unsupported emote: {kind}")

        def emote_reads(ops: _Ops, refs: dict[str, dict]) -> dict[str, dict]:
            npe = ops.get(refs["manager"], "_npeDirector", optional=True)
            ui = ops.get(refs["manager"], "UIMessageHandler", optional=True)
            return {
                "npe": npe,
                "ui": ui,
                "channel": ops.get(ops.get(ui, "_sendUIMessage", optional=True), "m_target", optional=True),
                "receivers": ops.add(
                    "call",
                    target=ops.get(ui, "EmoteRecievedCallback", optional=True),
                    method="GetInvocationList",
                    args=[],
                    depth=2,
                    optional=True,
                ),
            }

        values, refs, (live_match, live_game, live_turn) = self._await_live_game(
            command, "emote", timeout, emote_reads
        )
        npe, ui, channel, receivers = refs["npe"], refs["ui"], refs["channel"], refs["receivers"]

        def value(reference: dict) -> Any:
            index = reference["ref"]
            return values[index] if index < len(values) else None

        if handle(value(npe)):
            raise AdapterError("No emotes in the tutorial")
        if not handle(value(ui)):
            raise AdapterError("No UIMessageHandler")
        if short_class(value(channel)) != "GreInterface" or handle(value(channel)) != handle(
            value(refs["gre"])
        ):
            raise AdapterError("The emote channel is not this game's GreInterface")
        dialogs = {
            handle(target)
            for target in (field(entry, "m_target") for entry in items(value(receivers)))
            if short_class(target) == "LocalPlayerDialogController" and handle(target)
        }
        if len(dialogs) != 1:
            raise AdapterError(f"Emote wheel unavailable ({len(dialogs)} local emote controllers)")
        dialog = H(dialogs.pop())

        ops = _Ops()
        ops.expect_class(dialog, "LocalPlayerDialogController")
        dialog_ui = ops.get(dialog, "_uiMessageHandler")
        wheel = ops.get(dialog, "_emoteOptionsController")
        wheel_disposed = ops.get(wheel, "Disposed")
        equipped = ops.add("get", target=wheel, member="_equippedEmoteOptions", depth=2, max_items=64)
        checks = self._run(ops, timeout)

        def checked(reference: dict) -> Any:
            index = reference["ref"]
            return checks[index] if index < len(checks) else None

        if handle(checked(dialog_ui)) != handle(value(ui)):
            raise AdapterError("Emote wheel unavailable (another game's emote controller)")
        if checked(wheel_disposed) is not False or not handle(checked(wheel)):
            raise AdapterError("Emote wheel unavailable (disposed)")
        options = checked(equipped)
        on_wheel = [field(entry, "Id") for entry in items(options)]
        if not isinstance(options, dict) or options.get("$n") != len(on_wheel):
            raise AdapterError("Emote wheel unavailable (incomplete wheel)")
        if emote_id not in on_wheel:
            raise AdapterError(
                f"{emote_id} is not on this deck's emote wheel ({', '.join(map(str, on_wheel)) or 'empty'})"
            )
        slot = on_wheel.index(emote_id)

        ops = _Ops()
        self._expect_live_game(ops, live_match, live_game, live_turn)
        ops.expect_class(dialog, "LocalPlayerDialogController")
        channel = ops.get(ops.get(ops.get(dialog, "_uiMessageHandler"), "_sendUIMessage"), "m_target")
        ops.expect_class(channel, "GreInterface")
        ops.expect_member(channel, "_disposed", False)
        wheel = ops.get(dialog, "_emoteOptionsController")
        ops.expect_member(wheel, "Disposed", False)
        options = ops.get(wheel, "_equippedEmoteOptions")
        ops.expect_member(options, "Count", len(on_wheel))
        ops.expect_member(ops.call(options, "get_Item", I(slot), depth=1), "Id", emote_id)
        click = len(ops.ops)
        ops.call(wheel, "EmoteClicked", {"str": emote_id})
        logger.warning(
            "mac bridge: sending emote %s (game %s of %s, turn %s) via EmoteOptionsController.EmoteClicked",
            emote_id,
            live_game,
            live_match,
            live_turn,
        )
        response = self._send({"action": "reflect_batch", "ops": ops.ops}, timeout)
        if not response.get("ok"):
            error = str(response.get("error") or "reflect_batch failed")
            failed = response.get("failed_op")
            if "outcome unknown" in error or (isinstance(failed, int) and failed >= click):
                logger.warning("mac bridge: emote outcome unknown: %s", error)
                return {"ok": False, "outcome_unknown": True, "error": error}
            raise AdapterError(f"Emote not sent: {error}")
        logger.warning("mac bridge: emote %s sent (game %s of %s)", emote_id, live_game, live_match)
        return {
            "ok": True,
            "submitted_type": "Emote",
            "emote": kind,
            "emote_id": emote_id,
            "match_id": live_match,
            "game_number": live_game,
            "turn": live_turn,
        }

    def _cmd_go_to_event(self, command: dict, timeout: float | None) -> dict:
        event_name = str(command.get("event_name") or "")
        if not event_name:
            raise AdapterError("An event name is required")
        home = self._find("HomePageContentController", timeout)
        if not home:
            raise AdapterError("Arena is not on the home screen")
        ops = _Ops()
        ops.call(H(home), "GoToEventScreen", {"str": event_name})
        self._run(ops, timeout)
        return {"ok": True, "event_name": event_name}

    # -- observation ---------------------------------------------------------

    def _cmd_return_to_home(self, command: dict, timeout: float | None) -> dict:
        ops = _Ops()
        end_scene = ops.add("find", **{"class": "MatchEndScene"}, depth=0, optional=True)
        home = ops.add("find", **{"class": "HomePageContentController"}, depth=0, optional=True)
        results = self._run(ops, timeout)
        found_end = handle(results[0])
        found_home = handle(results[1])
        if not found_end:
            if found_home:
                return {"ok": True, "already_home": True}
            raise AdapterError(
                "no MatchEndScene or HomePageContentController active — not on the match-result screen"
            )
        if self._skip_open_survey(timeout):
            # LeaveMatch is a no-op until the survey closes; the caller retries.
            return {"ok": True, "survey_skipped": True}
        if not self._match_end_state(timeout)["exit_ready"]:
            return {"ok": True, "waiting": True}

        ops = _Ops()
        ops.call(H(found_end), "LeaveMatch")
        self._run(ops, timeout)
        return {"ok": True}

    def _cmd_get_deck_editor(self, command: dict, timeout: float | None) -> dict:
        ops = _Ops()
        widget = ops.add("find", **{"class": "DeckBuilderWidget"}, depth=0, optional=True)
        ops.get(widget, "_isActive", optional=True)
        found, active = self._run(ops, timeout)
        if not active or not handle(found):
            return {"ok": True, "is_open": False}
        widget = H(handle(found))
        ops = _Ops()
        ops.expect_member(widget, "_isActive", True)
        context = ops.get(widget, "Context")
        limited = ops.get(context, "IsLimited")
        sideboarding = ops.get(context, "IsSideboarding")
        readonly = ops.get(context, "IsReadOnly")
        values = self._run(ops, timeout)
        if not values[limited["ref"]] or values[sideboarding["ref"]] or values[readonly["ref"]]:
            return {"ok": True, "is_open": False}
        ops = _Ops()
        ops.expect_member(widget, "_isActive", True)
        context = ops.get(widget, "Context")
        ops.expect_member(context, "IsLimited", True)
        ops.expect_member(context, "IsSideboarding", False)
        ops.expect_member(context, "IsReadOnly", False)
        provider = ops.get(widget, "ModelProvider")
        model = ops.get(provider, "_model")
        ops.expect_member(model, "HasLoadedDeck", True)
        deck = ops.call(model, "GetServerModel")
        main = ops.get(deck, "mainDeck", depth=2)
        side = ops.get(deck, "sideboard", depth=2)
        identity = ops.get(deck, "id", depth=1)
        values = self._run(ops, timeout)

        def counts(reference: dict) -> list[dict]:
            node = values[reference["ref"]]
            entries = items(node)
            if not isinstance(node, dict) or node.get("$n") != len(entries):
                raise AdapterError("Incomplete deck editor snapshot")
            result = []
            for entry in entries:
                grp_id, count = num(field(entry, "Id")), num(field(entry, "Quantity"))
                if grp_id is None or count is None or grp_id <= 0 or count <= 0:
                    raise AdapterError("Invalid deck editor card entry")
                result.append({"grp_id": grp_id, "count": count})
            return result

        return {
            "ok": True,
            "is_open": True,
            "is_limited": True,
            "deck_id": guid(values[identity["ref"]]),
            "main_deck": counts(main),
            "sideboard": counts(side),
        }

    def _cmd_get_game_state(self, command: dict, timeout: float | None) -> dict:
        from arenamcp.mac_game_state import fetch_game_state

        return fetch_game_state(self._send, timeout)

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
        if name == "SearchRequest":
            response["search_candidates"] = self._search_card_options(snapshot, response, timeout)
        payload = build_request_payload(request, response["request_type"], name)
        if payload:
            response["request_payload"] = payload
        if "decision_context" not in response:
            context = build_decision_context(response["request_type"], name, payload)
            if context:
                response["decision_context"] = context
        return response

    def _search_card_options(self, snapshot: Snapshot, response: dict, timeout: float | None) -> list[dict]:
        """Identify only the instances Arena explicitly offers in this search.

        Library choices need not appear in Player.log. Read each offered card
        from the client, without enumerating hidden zones or library order.

        ``LatestGameState`` first: it is the GRE state the SearchReq arrived
        with, which reveals the library to the searcher. ``CurrentGameState``
        is what playback shows and trails it through combat-damage and
        trigger animations, so a Search raised mid-combat read every
        candidate as grpId 0 (seconds-long "unreadable" waits; 2026-10-18
        18:35:41 the autopilot never answered and the game was lost on time).
        The current state remains the fallback for each card.
        """
        ids = response["search_candidates"]
        if not ids:
            return []
        ops = _Ops()
        ops.expect_pending(H(snapshot.handle))
        manager = ops.add("find", **{"class": "GameManager"}, depth=0, optional=True)
        states = [
            ops.get(manager, "LatestGameState", optional=True),
            ops.get(manager, "CurrentGameState", optional=True),
        ]
        grp_refs = []
        for instance_id in ids:
            refs = []
            for state in states:
                card = ops.add(
                    "call", target=state, method="GetCardById", args=[U(instance_id)], depth=0, optional=True
                )
                refs.append(ops.get(card, "GrpId", optional=True))
            grp_refs.append(refs)
        results = self._run(ops, timeout)
        return [
            {
                "instanceId": instance_id,
                "grpId": next((num(results[ref["ref"]]) for ref in refs if num(results[ref["ref"]])), 0),
            }
            for instance_id, refs in zip(ids, grp_refs, strict=True)
        ]

    def _shape_ActionsAvailableRequest(self, request: dict, getters: dict, response: dict) -> None:
        response["actions"] = [serialize_action(action) for action in items(field(request, "Actions"))]
        response["can_pass"] = bool(getters.get("CanPass"))

    def _shape_OptionalActionMessageRequest(self, request: dict, getters: dict, response: dict) -> None:
        response["source_instance_id"] = num(field(request, "SourceId"))
        response["optional_mechanics"] = [enum_name(value) for value in items(field(request, "Mechanics"))]
        response["optional_recipients"] = [num(value) for value in items(field(request, "RecipientIds"))]

    def _shape_DeclareBlockersRequest(self, request: dict, getters: dict, response: dict) -> None:
        response["blockers"] = [
            {
                "blockerInstanceId": num(field(blocker, "BlockerInstanceId")),
                "mustBlock": bool(field(blocker, "MustBlock")),
                "minAttackers": num(field(blocker, "MinAttackers")),
                "maxAttackers": num(field(blocker, "MaxAttackers")),
                "attackerInstanceIds": [num(i) for i in items(field(blocker, "AttackerInstanceIds"))],
                "selectedAttackerInstanceIds": [
                    num(instance_id) for instance_id in items(field(blocker, "SelectedAttackerInstanceIds"))
                ],
            }
            for blocker in items(field(request, "AllBlockers"))
        ]
        response["can_pass"] = False

    def _shape_DeclareAttackerRequest(self, request: dict, getters: dict, response: dict) -> None:
        attackers = []
        # QualifiedAttackers is the legal menu; Attackers holds the mutable
        # declaration used by UpdateAttacker/SubmitAttackers. The two can be
        # separate objects, so the menu's selected recipient may remain null
        # even after a real selection (captured during manual recovery).
        declarations = {
            num(field(attacker, "AttackerInstanceId")): attacker
            for attacker in attacker_nodes(request, "Attackers")
        }
        for attacker in attacker_nodes(request, "QualifiedAttackers"):
            recipients = []
            recipient_nodes = items(field(attacker, "LegalDamageRecipients"))
            for recipient in recipient_nodes:
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
            entry = {
                "attackerInstanceId": num(field(attacker, "AttackerInstanceId")),
                "mustAttack": bool(field(attacker, "MustAttack")),
                "legalDamageRecipients": recipients,
            }
            declared = declarations.get(num(field(attacker, "AttackerInstanceId")))
            if field(request, "Attackers") is None:
                declared = attacker  # Compatibility with snapshots exposing only the legal menu.
            selected = field(declared, "SelectedDamageRecipient")
            entry["selectedDamageRecipient"] = None
            if isinstance(selected, dict):
                selected_recipient = {"type": enum_name(field(selected, "Type"))}
                for key, member in (
                    ("playerSystemSeatId", "PlayerSystemSeatId"),
                    ("planeswalkerInstanceId", "PlaneswalkerInstanceId"),
                    ("teamId", "TeamId"),
                ):
                    value = recipient_id(selected, member)
                    if value is not None:
                        selected_recipient[key] = value
                # Shared objects can be encoded as handle-only $ref nodes.
                # Reuse the legal recipient's expanded identity in that case.
                if handle(selected) is not None:
                    declaration_recipients = items(field(declared, "LegalDamageRecipients"))
                    selected = next(
                        (node for node in declaration_recipients if handle(node) == handle(selected)),
                        selected,
                    )
                    for key, member in (
                        ("playerSystemSeatId", "PlayerSystemSeatId"),
                        ("planeswalkerInstanceId", "PlaneswalkerInstanceId"),
                        ("teamId", "TeamId"),
                    ):
                        value = recipient_id(selected, member)
                        if value is not None:
                            selected_recipient[key] = value
                    selected_recipient["type"] = (
                        enum_name(field(selected, "Type")) or selected_recipient["type"]
                    )
                    selected_recipient = next(
                        (
                            value
                            for node, value in zip(recipient_nodes, recipients, strict=True)
                            if handle(node) == handle(selected)
                        ),
                        selected_recipient,
                    )
                if any(key in selected_recipient for key in ("playerSystemSeatId", "planeswalkerInstanceId")):
                    entry["selectedDamageRecipient"] = selected_recipient
            attackers.append(entry)
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


    _shape_CastingTimeOptionsRequest = _shape_CastingTimeOptionRequest  # Arena 2026.64.50's name

    def _shape_SelectTargetsRequest(self, request: dict, getters: dict, response: dict) -> None:
        selections, flat = [], []
        for selection in items(field(request, "TargetSelections")):
            index = num(field(selection, "TargetIdx"))
            slot = []
            for target in items(field(selection, "Targets")):
                # grpId stays 0: the plugin resolves it from the live board, which
                # the log-first coach already knows by instance id.
                entry = {
                    "targetInstanceId": num(field(target, "TargetInstanceId")),
                    "targetIdx": index,
                    "grpId": 0,
                }
                action = field(target, "LegalAction")
                if action is not None:
                    entry["legalAction"] = enum_name(action) or num(action)
                entry["highlight"] = enum_name(field(target, "Highlight"))
                slot.append(dict(entry))
                if action is None or enum_name(action) == "Select" or num(action) == 1:
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
        options = field(request, "Options")
        if isinstance(options, dict) and options.get("$n", len(items(options))) != len(items(options)):
            raise AdapterError("Search options snapshot is incomplete")
        if field(request, "Min") is None or field(request, "Max") is None:
            raise AdapterError("Search selection limits are missing")
        response["search_candidates"] = [num(i) for i in items(options)]
        response["select_n_min"] = num(field(request, "Min"))
        response["select_n_max"] = num(field(request, "Max"))
        response["search_zones"] = [num(i) for i in items(field(request, "ZonesToSearch"))]
        response["search_additional_zones"] = [num(i) for i in items(field(request, "AdditionalZones"))]
        response["search_context_options"] = [num(i) for i in items(field(request, "ContextOptions"))]
        response["search_is_multi_zone"] = bool(getters.get("IsMultiZoneSearch"))
        response["can_pass"] = False

    def _shape_SelectNRequest(self, request: dict, getters: dict, response: dict) -> None:
        response["select_n_ids"] = [num(i) for i in items(field(request, "Ids"))]
        listed = field(request, "Ids")
        if isinstance(listed, dict) and num(listed.get("$n"), 0) > len(response["select_n_ids"]):
            # The snapshot cut the list (SNAPSHOT_MAX_ITEMS): the log's SelectNReq has the rest.
            response["select_n_ids_truncated"] = True
        response["select_n_zone_ids"] = [num(i) for i in items(field(request, "ZoneIds"))]
        response["select_n_id_type"] = enum_name(field(request, "IdType"))
        response["select_n_list_type"] = enum_name(field(request, "ListType"))
        response["select_n_context"] = enum_name(field(request, "Context"))
        response["select_n_option_context"] = enum_name(field(request, "OptionContext"))
        response["select_n_static_list"] = enum_name(field(request, "StaticList"))
        response["select_n_min"] = num(field(request, "MinSel"))
        response["select_n_max"] = num(field(request, "MaxSel"))
        response["select_n_weights"] = [num(weight) for weight in items(field(request, "Weights"))]
        response["select_n_min_weight"] = num(field(request, "MinWeight"), -(2**31))
        response["select_n_max_weight"] = num(field(request, "MaxWeight"), 2**31 - 1)
        should_cancel = bool(field(request, "ShouldCancel"))
        response["select_n_can_cancel"] = should_cancel
        for key, getter in (
            ("instance_id", "IsInstanceIdSelection"),
            ("zone", "IsZoneSelection"),
            ("mana_color", "IsManaColorSelection"),
            ("card_color", "IsCardColorSelection"),
            ("counter", "IsCounterSelection"),
            ("basic_land", "IsBasicLandSelection"),
            ("triggered_ability", "IsTriggeredAbilitySelection"),
            ("stacking_decision", "IsStackingDecision"),
        ):
            response["select_n_is_" + key] = bool(getters.get(getter))
        response["select_n_should_cancel"] = should_cancel
        response["can_pass"] = False
        # "Choose a color" (Room of Refuge, live 2026-10-07): a Static
        # StaticList_Colors request carries no Ids; the client answers
        # SubmitSelection((uint)CardColor) White=1..Green=5 (color_choice.py).
        is_color = getters.get("IsCardColorSelection") or getters.get("IsManaColorSelection")
        color_ids = color_selection_ids(
            list_type=response["select_n_list_type"],
            static_list=response["select_n_static_list"],
            context=response["select_n_context"],
            ids=response["select_n_ids"],
            is_color=True if is_color else None,
        )
        if color_ids is not None:
            response["select_n_color_options"] = color_options(color_ids)
            response["decision_context"] = {
                "type": "choose_color",
                "source_id": num(field(request, "SourceId")),
                "ids": color_ids,
                "options": [option["name"] for option in response["select_n_color_options"]],
                "count": response["select_n_max"],
                "min": response["select_n_min"],
                "max": response["select_n_max"],
            }

    def _shape_PayCostsRequest(self, request: dict, getters: dict, response: dict) -> None:
        selection = cost_selection_request(request)
        if selection is None:
            return
        self._shape_SelectNRequest(selection, {}, response)
        response["request_type"] = "SelectN"
        response["payment_selection"] = True
        source_id = num(field(request, "SourceId")) or num(field(selection, "SourceId"))
        # Arena's own prompt for the cost (ledger #160, match be1a945f T6: Sneak's "Return an
        # unblocked attacking creature you control to its owner's hand." (prompt 8580, CardId =
        # the spell) reached the model as "Pay non-mana cost", which it read as a tap cost).
        prompt = _request_prompt(request) or _request_prompt(selection)
        text = ""
        if prompt:
            response["prompt"] = prompt
            try:
                from arenamcp.arena_prompts import prompt_text

                text = prompt_text(prompt)
            except Exception:
                text = ""
        response["source_card"] = f"Pay non-mana cost: {text}" if text else "Pay non-mana cost"
        if source_id:
            response["source_instance_id"] = source_id
        response["decision_context"] = {"type": "select_n", "source_id": source_id}
        if text:
            response["decision_context"]["prompt_text"] = text

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
        snapshot = self._require(timeout, "ActionsAvailableRequest", *CASTING_TIME_PARENTS)
        index = int(command.get("action_index") or 0)
        if snapshot.request_class in CASTING_TIME_PARENTS:
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
        # Loyalty abilities of one planeswalker share instanceId and grpId.
        if command.get("expected_ability_grp_id") is not None and int(
            command["expected_ability_grp_id"]
        ) != int(info.get("abilityGrpId") or 0):
            raise AdapterError(f"identity mismatch: action {index} is {info}")
        if (
            command.get("expected_game_state_id") not in (None, -1)
            and int(command["expected_game_state_id"]) != snapshot.game_state_id
        ):
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
        if payload.get("choiceKind") == "modal" and int(payload.get("min", 1)) > 1:
            raise AdapterError("This modal request requires multiple modes in one submission")
        self._check_request_identity(snapshot, command)
        for expected, key in (
            ("expected_child_index", "childIndex"),
            ("expected_numeric_value", "numericValue"),
        ):
            if command.get(expected) is not None and command[expected] != payload.get(key):
                raise AdapterError(f"identity mismatch: option {index} is {payload}")
        if command.get("expected_choice_kind") and command["expected_choice_kind"] != payload["choiceKind"]:
            raise AdapterError(f"identity mismatch: option {index} is {payload}")
        if command.get("expected_option_index") is not None and command[
            "expected_option_index"
        ] != payload.get("optionIndex"):
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

    def _cmd_submit_casting_options(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout, *CASTING_TIME_PARENTS)
        self._check_request_identity(snapshot, command)
        entries = casting_time_entries(snapshot.request)
        indices = command.get("action_indices") or []
        expected = command.get("expected_options") or []
        if (
            not indices
            or any(type(index) is not int or not 0 <= index < len(entries) for index in indices)
            or len(set(indices)) != len(indices)
            or len(expected) != len(indices)
        ):
            raise AdapterError("Invalid casting option indices or missing identities")
        selected = [entries[index] for index in indices]
        first = selected[0]
        if first["payload"]["choiceKind"] != "modal" or any(
            entry["child_handle"] != first["child_handle"] or entry["payload"]["choiceKind"] != "modal"
            for entry in selected
        ):
            raise AdapterError("Casting modes must belong to one modal child request")
        for entry, identity in zip(selected, expected, strict=True):
            if not isinstance(identity, dict):
                raise AdapterError("Missing casting option identity")
            for key in ("childIndex", "choiceKind", "optionIndex", "grpId"):
                if identity.get(key) != entry["payload"].get(key):
                    raise AdapterError(f"identity mismatch: casting option {key} changed")
        minimum = int(first["payload"].get("min", 1))
        maximum = int(first["payload"].get("max", max(1, minimum)))
        if not minimum <= len(selected) <= maximum:
            raise AdapterError(f"Casting request requires {minimum}-{maximum} modes, got {len(selected)}")
        groups = [entry["payload"]["grpId"] for entry in selected]

        def build(ops: _Ops, request: dict) -> None:
            ops.expect_class(H(first["child_handle"]), first["child_class"])
            ops.call(H(first["child_handle"]), "SubmitModal", UINTS(groups))

        self._submit(snapshot, build, timeout)
        return {"ok": True, "submitted_type": "CastingTimeOptions", "submitted_grp_ids": groups}

    def _call_on_request(
        self, command: dict, timeout: float | None, classes: tuple[str, ...], method: str, *args: dict
    ) -> Snapshot:
        snapshot = self._require(timeout, *classes)
        self._check_request_identity(snapshot, command)
        self._submit(snapshot, lambda ops, request: ops.call(request, method, *args), timeout)
        return snapshot

    @staticmethod
    def _check_request_identity(snapshot: Snapshot, command: dict) -> None:
        for expected, actual in (
            ("expected_game_state_id", snapshot.game_state_id),
            ("expected_msg_id", snapshot.msg_id),
        ):
            if command.get(expected) not in (None, 0, -1) and int(command[expected]) != actual:
                raise AdapterError("stale command: the pending request changed")

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
        self._call_on_request(
            command, timeout, ("ChooseStartingPlayerRequest",), "ChooseStartingPlayer", U(seat)
        )
        return {"ok": True, "submitted_type": "ChooseStartingPlayer", "seat_id": seat}

    def _cmd_submit_optional(self, command: dict, timeout: float | None) -> dict:
        accept = bool(command.get("accept"))
        response = "AllowYes" if accept else "CancelNo"
        self._call_on_request(
            command, timeout, ("OptionalActionMessageRequest",), "SubmitResponse", E(response)
        )
        return {"ok": True, "submitted_type": "Optional", "response": response}

    def _cmd_submit_numeric(self, command: dict, timeout: float | None) -> dict:
        value = int(command.get("value") or 0)
        self._call_on_request(command, timeout, ("NumericInputRequest",), "SubmitValue", U(value))
        return {"ok": True, "submitted_type": "NumericInput", "value": value}

    def _cmd_submit_x(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout, *CASTING_TIME_PARENTS, "NumericInputRequest")
        requested = int(command.get("value") or 0)
        if snapshot.request_class == "NumericInputRequest":
            target, method, kind = snapshot.request, "SubmitValue", "NumericInput"
        else:
            target = next(
                (
                    c
                    for c in items(field(snapshot.request, "ChildRequests"))
                    if short_class(c) == "CastingTimeOption_NumericInputRequest"
                ),
                None,
            )
            if target is None:
                raise AdapterError(
                    "CastingTimeOptionRequest has no CastingTimeOption_NumericInputRequest child"
                )
            method, kind = "SubmitX", "CastingTimeOption_X"
        # Clamp before the uint conversion (issue #390: out-of-range X wedged the slider).
        value = max(num(field(target, "Min")), min(num(field(target, "Max")), requested))
        self._submit(snapshot, lambda ops, request: ops.call(H(handle(target)), method, U(value)), timeout)
        return {"ok": True, "submitted_type": kind, "value": value, "clamped": value != requested}

    def _cmd_submit_selection(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout, "SelectNRequest", "SearchRequest", "PayCostsRequest")
        ids = [int(i) for i in command.get("ids") or []]
        if snapshot.request_class == "SearchRequest":
            offered = {num(i) for i in items(field(snapshot.request, "Options"))}
            minimum, maximum = num(field(snapshot.request, "Min")), num(field(snapshot.request, "Max"))
            if (
                len(set(ids)) != len(ids)
                or not set(ids).issubset(offered)
                or not minimum <= len(ids) <= maximum
            ):
                raise AdapterError("Selection does not satisfy the current search constraints")
        if snapshot.request_class == "PayCostsRequest":
            from arenamcp.decisions import build_pending_decision

            child = cost_selection_request(snapshot.request)
            if child is None:
                raise AdapterError("PayCosts has no supported non-mana selection")
            poll = {"has_pending": True, "request_type": "SelectN"}
            self._shape_SelectNRequest(child, {}, poll)
            decision = build_pending_decision(poll)
            if decision is None or not decision.selection_is_valid(
                [f"sel:{instance_id}" for instance_id in ids]
            ):
                raise AdapterError("Selection does not satisfy the current non-mana payment constraints")
            selection = UINTS(ids)
            self._submit(
                snapshot,
                lambda ops, request: ops.call(H(handle(child)), "SubmitSelection", selection),
                timeout,
            )
            return {"ok": True, "submitted_type": "CostSelection"}
        if snapshot.request_class == "SelectNRequest":
            shape: dict[str, Any] = {}
            self._shape_SelectNRequest(snapshot.request, snapshot.getters, shape)
            offered_colors = shape.get("select_n_color_options")
            if offered_colors is not None:
                allowed = {option["id"] for option in offered_colors}
                minimum, maximum = shape["select_n_min"], max(1, shape["select_n_max"])
                if (
                    len(set(ids)) != len(ids)
                    or not set(ids).issubset(allowed)
                    or not minimum <= len(ids) <= maximum
                ):
                    raise AdapterError("Selection does not satisfy the colour choice")
                # Colours are answered with SubmitSelection even when none is
                # chosen (SelectColorWorkflow.SubmitSelections), never arbitrarily.
                selection = UINTS(ids)
                self._submit(
                    snapshot, lambda ops, request: ops.call(request, "SubmitSelection", selection), timeout
                )
                return {"ok": True, "submitted_type": "SelectN"}
        if snapshot.request_class == "SelectNRequest" and not ids:
            self._submit(snapshot, lambda ops, request: ops.call(request, "SubmitArbitrary"), timeout)
            return {"ok": True, "submitted_type": "SelectN"}
        selection = UINTS(ids)
        self._submit(snapshot, lambda ops, request: ops.call(request, "SubmitSelection", selection), timeout)
        return {
            "ok": True,
            "submitted_type": "SelectN" if snapshot.request_class == "SelectNRequest" else "Search",
        }

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
            for key, prefix, member in (
                ("zone", "ZoneType_", "ZoneType"),
                ("sub_zone", "SubZoneType_", "SubZoneType"),
            ):
                raw = str(spec.get(key) or "")
                if raw:
                    ops.set(group, member, E(raw.removeprefix(prefix)))
        elif int(spec.get("groupId") or 0) > 0:
            ops.set(group, "GroupId", I(int(spec["groupId"])))
        return group

    def _cmd_submit_blockers(self, command: dict, timeout: float | None) -> dict:
        """Declare assignments first; finalize only after the caller checks acceptance."""
        snapshot = self._require(timeout, "DeclareBlockersRequest")
        assignments = command.get("assignments") or []
        if not assignments:
            self._submit(snapshot, lambda ops, request: ops.call(request, "SubmitBlockers"), timeout)
            return {"ok": True, "submitted_type": "DeclareBlockers"}
        by_id = {num(field(b, "BlockerInstanceId")): b for b in items(field(snapshot.request, "AllBlockers"))}
        selected = []
        used_blockers: set[int] = set()
        for assignment in assignments:
            blocker_id = int(assignment.get("blockerInstanceId") or 0)
            if blocker_id in used_blockers:
                raise AdapterError(f"Duplicate blocker instance {blocker_id}")
            used_blockers.add(blocker_id)
            blocker = by_id.get(blocker_id)
            if blocker is None:
                raise AdapterError(f"Blocker {blocker_id} not in AllBlockers")
            attacker_ids = [int(attacker_id) for attacker_id in assignment.get("attackerInstanceIds") or []]
            legal_attackers = {
                num(instance_id) for instance_id in items(field(blocker, "AttackerInstanceIds"))
            }
            # No attackers withdraws the blocker's selection, as Arena's own UI
            # does (DeclareBlockersWorkflow: SelectedAttackerInstanceIds.Clear()
            # then UpdateBlockers(blocker)); 2026-10-19 a block kept from a
            # rejected declaration was otherwise committed with the next one.
            if not set(attacker_ids).issubset(legal_attackers):
                raise AdapterError(f"Illegal attackers for blocker {blocker_id}: {attacker_ids}")
            selected.append((blocker, attacker_ids))

        def build(ops: _Ops, request: dict) -> None:
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

        self._submit(snapshot, build, timeout)
        return {"ok": True, "submitted_type": "DeclareBlockers", "needs_finalize": True}

    def _cmd_submit_attackers(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout, "DeclareAttackerRequest")
        for name, actual in (
            ("expected_game_state_id", snapshot.game_state_id),
            ("expected_msg_id", snapshot.msg_id),
        ):
            if command.get(name) is not None and command[name] != actual:
                raise AdapterError("Stale attack declaration: request changed before confirmation")
        wanted = {int(a.get("attackerInstanceId") or 0) for a in command.get("attackers") or []}
        available = attacker_nodes(snapshot.request, "Attackers")
        legal_ids = {num(field(attacker, "AttackerInstanceId")) for attacker in available}
        if not wanted.issubset(legal_ids):
            raise AdapterError(
                f"Requested attackers are not in this combat window: {sorted(wanted - legal_ids)}"
            )
        matched = []
        targets = {
            int(entry.get("attackerInstanceId") or 0): entry.get("damageRecipient")
            for entry in command.get("attackers") or []
        }
        for attacker in available:
            identity = num(field(attacker, "AttackerInstanceId"))
            if identity not in wanted:
                continue
            legal = items(field(attacker, "LegalDamageRecipients"))
            target = targets[identity]
            if target:
                from arenamcp.combat_targets import recipient_key

                try:
                    requested_kind, requested_id = recipient_key(target)
                except (ValueError, TypeError) as error:
                    raise AdapterError(str(error)) from error
                member = {
                    "player": "PlayerSystemSeatId",
                    "planeswalker": "PlaneswalkerInstanceId",
                    "battle": "BattleInstanceId",
                }[requested_kind]
                matches = [
                    recipient for recipient in legal if recipient_id(recipient, member) == requested_id
                ]
                if len(matches) != 1:
                    raise AdapterError(f"Illegal damage recipient for attacker {identity}")
                recipient = matches[0]
            elif len(legal) == 1:
                recipient = legal[0]
            else:
                raise AdapterError(f"Attacker {identity} needs an explicit legal damage recipient")
            selected = field(attacker, "SelectedDamageRecipient")
            if not same_damage_recipient(selected, recipient):
                matched.append((attacker, recipient))
        if command.get("finalize_only") or (wanted and not matched):
            selected_ids = {
                num(field(attacker, "AttackerInstanceId"))
                for attacker in available
                if field(attacker, "SelectedDamageRecipient") is not None
            }
            if matched or selected_ids != wanted:
                raise AdapterError("Attack declaration changed before confirmation; no selection submitted")
        if not wanted or not matched:
            # Finalize: the second step of the two-step flow, or "attack with nobody".
            if snapshot.getters.get("CanSubmit") is False:
                raise AdapterError("Attack declaration cannot be confirmed; finish choosing legal recipients")
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

        slots = []
        candidates = []
        for selection in selections:
            legal = {
                num(field(target, "TargetInstanceId")): target
                for target in items(field(selection, "Targets"))
                if field(target, "LegalAction") is None
                or enum_name(field(target, "LegalAction")) == "Select"
                or num(field(target, "LegalAction")) == 1
            }
            candidates.append(legal)
            slots.append(
                TargetSlot(
                    target_idx=num(field(selection, "TargetIdx")),
                    min_targets=num(field(selection, "MinTargets")),
                    max_targets=num(field(selection, "MaxTargets")),
                    selected=num(field(selection, "SelectedTargets")),
                    candidate_ids=tuple(legal),
                )
            )
        assignments = assign_target_slots(tuple(slots), preferred)
        if assignments is None:
            raise AdapterError("Explicit targets do not satisfy every target slot; no targets submitted")
        chosen = [
            (selection, [legal[instance_id] for instance_id in assigned])
            for selection, legal, assigned in zip(selections, candidates, assignments, strict=True)
            if assigned
        ]

        base = {"submitted_type": "SelectTargets", "target_instance_id": caller_target}
        if finalize and not chosen and selections and not any(slot.needs for slot in slots):
            self._commit_targets(snapshot, timeout)
            return {"ok": True, **base, "already_selected": True, "finalized": True}
        if not chosen:
            raise AdapterError("No legal targets to fill")

        # One target slot per GRE round trip, as the real client clicks them.
        # Both slots in one batch reached Arena as the first slot only (the
        # request is consumed by the first OnSubmit): Felling Blow 18:31:40 and
        # Smell Fear 15:18:11 / 18:40:49 on 2026-10-18 sent slot 1, then failed
        # "not acknowledged complete" / "acknowledged different targets" and
        # announced MANUAL REQUIRED while slot 2 sat waiting.
        first, later = (chosen[:1], chosen[1:]) if finalize else (chosen, [])
        player_selected = False
        try:
            self._send_target_slots(snapshot, first, timeout)
        except RequestAlreadyAnswered:
            # 2026-10-18 21:15:30.263: the player clicked Evolution Sage for
            # Silkguard 230 ms before the model's identical pick was sent; our
            # submit hit the spent OnSubmit and MANUAL REQUIRED was announced
            # while the player finished the spell. Their selection is still on
            # its way: if Arena acknowledges exactly our targets, commit them.
            # Never add our own slots on top of a player's choice in progress.
            if not finalize or later:
                raise
            logger.info("mac bridge submit_targets: the player selected first; commit only matching targets")
            player_selected = True
        required = sum(slot.needs > 0 for slot in slots)
        result = {
            **base,
            "slots_required": required,
            "slots_filled": len(chosen),
            "required_filled": required,
            "targets_selected": len(preferred),
        }
        if finalize:
            expected = {
                slot.target_idx: set(assigned) for slot, assigned in zip(slots, assignments, strict=True)
            }
            pending = [
                (
                    num(field(selection, "TargetIdx")),
                    [num(field(target, "TargetInstanceId")) for target in targets],
                )
                for selection, targets in later
            ]
            advanced = self._deferred_commit_targets(
                snapshot, timeout, expected, pending, player_selected=player_selected
            )
            return {"ok": True, **result, "finalized": True, "advanced_without_commit": advanced}
        return {"ok": True, **result, "finalized": False}

    def _send_target_slots(
        self, snapshot: Snapshot, chosen: list[tuple[dict, list[dict]]], timeout: float | None
    ) -> None:
        """One SelectTargetsResp per (selection, targets) on ``snapshot``'s request."""

        def build(ops: _Ops, request: dict) -> None:
            for selection, targets in chosen:
                sent_targets = []
                for target in targets:
                    sent = ops.new(MESSAGING + "Target")
                    ops.set(sent, "TargetInstanceId", U(num(field(target, "TargetInstanceId"))))
                    ops.set(sent, "LegalAction", E("Select"))
                    ops.set(sent, "Highlight", I(num(field(target, "Highlight"))))
                    sent_targets.append(sent)
                slot = ops.new(MESSAGING + "TargetSelection")
                ops.set(slot, "TargetIdx", U(num(field(selection, "TargetIdx"))))
                for sent in sent_targets:
                    ops.call(ops.get(slot, "Targets"), "Add", sent)
                response_body = ops.new(MESSAGING + "SelectTargetsResp")
                ops.set(response_body, "Target", slot)
                message = ops.message("SelectTargetsResp", snapshot.game_state_id, snapshot.msg_id)
                ops.set(message, "SelectTargetsResp", response_body)
                ops.on_submit(request, message)

        self._submit(snapshot, build, timeout)

    def _commit_targets(self, snapshot: Snapshot, timeout: float | None) -> None:
        def build(ops: _Ops, request: dict) -> None:
            ops.on_submit(request, ops.message("SubmitTargetsReq", snapshot.game_state_id, snapshot.msg_id))

        self._submit(snapshot, build, timeout)

    def _send_next_target_slot(
        self, current: Snapshot, slot: tuple[int, list[int]], timeout: float | None
    ) -> bool:
        """Send a later slot's targets on the GRE's updated request; False when already selected."""
        target_idx, wanted = slot
        selection = next(
            (
                item
                for item in items(field(current.request, "TargetSelections"))
                if num(field(item, "TargetIdx")) == target_idx
            ),
            None,
        )
        if selection is None:
            raise AdapterError(f"Target slot {target_idx} is no longer offered; refusing to finalize")
        targets = items(field(selection, "Targets"))
        selected = {
            num(field(target, "TargetInstanceId"))
            for target in targets
            if enum_name(field(target, "LegalAction")) == "Unselect" or num(field(target, "LegalAction")) == 2
        }
        if set(wanted) <= selected:
            return False
        offered = {
            num(field(target, "TargetInstanceId")): target
            for target in targets
            if field(target, "LegalAction") is None
            or enum_name(field(target, "LegalAction")) == "Select"
            or num(field(target, "LegalAction")) == 1
        }
        if not set(wanted) <= set(offered):
            raise AdapterError(f"Target slot {target_idx} no longer offers {wanted}; refusing to finalize")
        self._send_target_slots(current, [(selection, [offered[instance_id] for instance_id in wanted])], timeout)
        return True

    def _deferred_commit_targets(
        self,
        original: Snapshot,
        timeout: float | None,
        expected: dict[int, set[int]],
        later: list[tuple[int, list[int]]] | None = None,
        player_selected: bool = False,
    ) -> bool:
        """Commit on the GRE's updated SelectTargetsReq, like the real client.

        ``later`` holds further slots, ``(target_idx, instance_ids)``, each sent
        on the request the GRE returns for the previous one. Returns True when
        the request disappeared without our commit (the client auto-submitted).
        Mirrors Plugin.DeferredSubmitTargets. ``player_selected``: the player's
        own selection is the one in flight (our send found OnSubmit spent), so
        wait longer for it, and a request that vanishes confirms nothing of ours.
        """
        later = list(later or [])
        source = num(field(original.request, "SourceId"))
        waiting_on = original.handle
        limit = TARGET_ANSWERED_WAIT_S if player_selected else TARGET_COMMIT_TIMEOUT_S
        started = time.monotonic()
        gone_since: float | None = None
        while time.monotonic() - started < limit:
            time.sleep(0.03)
            current = self.snapshot(depth=SNAPSHOT_DEPTH, timeout=timeout)
            if (
                current.request_class != "SelectTargetsRequest"
                or num(field(current.request, "SourceId")) != source
            ):
                gone_since = gone_since or time.monotonic()
                if time.monotonic() - gone_since >= TARGET_GONE_GRACE_S:
                    if player_selected:
                        # The player's answer, not ours, ended it: nothing we sent was confirmed.
                        raise RequestAlreadyAnswered("the player answered this target request")
                    return True
                continue
            gone_since = None
            if current.handle == waiting_on:
                continue  # the GRE has not round-tripped our selection yet
            sent = False
            while later and not sent:
                sent = self._send_next_target_slot(current, later.pop(0), timeout)
            if sent:
                waiting_on = current.handle
                started = time.monotonic()
                continue
            selections = items(field(current.request, "TargetSelections"))
            if selections and all(
                num(field(s, "MinTargets")) == 0
                or num(field(s, "SelectedTargets")) >= num(field(s, "MinTargets"))
                for s in selections
            ):
                for selection in selections:
                    targets = items(field(selection, "Targets"))
                    acknowledged = {
                        num(field(target, "TargetInstanceId"))
                        for target in targets
                        if enum_name(field(target, "LegalAction")) == "Unselect"
                        or num(field(target, "LegalAction")) == 2
                    }
                    if any(
                        field(target, "LegalAction") is not None for target in targets
                    ) and not expected.get(num(field(selection, "TargetIdx")), set()).issubset(acknowledged):
                        if player_selected:
                            raise RequestAlreadyAnswered("the player chose other targets; left to the player")
                        raise AdapterError("GRE acknowledged different targets; refusing to finalize")
                try:
                    self._commit_targets(current, timeout)
                except RequestAlreadyAnswered:
                    return True  # the player confirmed these same targets first
                return False
        if player_selected:
            raise RequestAlreadyAnswered("the player's target selection was not acknowledged in time")
        raise AdapterError("Target selection was not acknowledged complete; refusing to finalize")

    def _cmd_submit_assign_damage(self, command: dict, timeout: float | None) -> dict:
        """Answer AssignDamage as Arena's Done button does: every attacker, every receiver.

        Each attacker's entries keep Arena's order and template (Min/MaxDamage),
        with AssignedDamage from the command and 0 for a receiver it leaves out.
        An attacker the command leaves out keeps Arena's own assignment. Player.log
        2026-10-18 18:41: an answer without the player's entry (5 on a lethal-3
        blocker) was refused FailureReason_InvalidDamageAssignment seven times;
        Arena's own answers always list every entry (2026-10-19 12:23:04).
        """
        snapshot = self._require(timeout, "AssignDamageRequest")
        requested = [a for a in items(field(snapshot.request, "Assigners")) if isinstance(a, dict)]
        attackers = {num(field(a, "InstanceId")) for a in requested}
        wanted: dict[int, dict[int, int]] = {}
        for spec in command.get("assigners") or []:
            attacker = int(spec.get("instanceId") or 0)
            if attacker not in attackers:
                logger.warning("mac bridge submit_assign_damage: attacker %s not in request", attacker)
                continue
            damage = wanted.setdefault(attacker, {})
            for assignment in spec.get("assignments") or []:
                receiver = int(assignment.get("instanceId") or 0)
                damage[receiver] = damage.get(receiver, 0) + int(assignment.get("damage") or 0)
        if not wanted:
            raise AdapterError("no attacker in the command is in the AssignDamage request")
        plans = []
        for assigner in requested:
            attacker = num(field(assigner, "InstanceId"))
            templates = [t for t in items(field(assigner, "Assignments")) if isinstance(t, dict)]
            receivers = {num(field(t, "InstanceId")) for t in templates}
            damage = wanted.get(attacker)
            if damage is None:
                damage = {num(field(t, "InstanceId")): num(field(t, "AssignedDamage")) for t in templates}
            elif set(damage) - receivers:
                raise AdapterError(
                    f"receivers {sorted(set(damage) - receivers)} are not in attacker {attacker}'s assignments"
                )
            plans.append((assigner, templates, damage))

        def build(ops: _Ops, request: dict) -> None:
            built = []
            for assigner, templates, damage in plans:
                clone = ops.new(MESSAGING + "DamageAssigner")
                ops.set(clone, "InstanceId", U(num(field(assigner, "InstanceId"))))
                ops.set(clone, "TotalDamage", U(num(field(assigner, "TotalDamage"))))
                target_list = ops.get(clone, "Assignments")
                for template in templates:
                    receiver = num(field(template, "InstanceId"))
                    entry = ops.new(MESSAGING + "DamageAssignment")
                    ops.set(entry, "InstanceId", U(receiver))
                    ops.set(entry, "MinDamage", U(num(field(template, "MinDamage"))))
                    ops.set(entry, "MaxDamage", U(num(field(template, "MaxDamage"))))
                    ops.set(entry, "AssignedDamage", U(damage.get(receiver, 0)))
                    ops.call(target_list, "Add", entry)
                built.append(clone)
            ops.call(request, "SubmitAssignment", LIST(built))

        self._submit(snapshot, build, timeout)
        return {"ok": True, "submitted_type": "AssignDamage", "assigner_count": len(plans)}

    def _cmd_submit_distribution(self, command: dict, timeout: float | None) -> dict:
        pairs = [
            [U(int(k)), U(int(v))]
            for k, v in (command.get("distributions") or {}).items()
            if str(k).isdigit()
        ]
        self._call_on_request(
            command, timeout, ("DistributionRequest",), "SubmitDistribution", {"dict": pairs}
        )
        return {"ok": True, "submitted_type": "Distribution", "target_count": len(pairs)}

    def _cmd_submit_order(self, command: dict, timeout: float | None) -> dict:
        snapshot = self._require(timeout, "OrderRequest")
        ids = command.get("ids")
        ordered = (
            [int(i) for i in ids]
            if ids is not None
            else [num(i) for i in items(field(snapshot.request, "Ids"))]
        )
        self._submit(
            snapshot,
            lambda ops, request: ops.call(request, "SubmitOrder", UINTS(ordered)),
            timeout,
        )
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
        self._submit(
            snapshot, lambda ops, request: ops.call(H(handle(auto_tap)), "SubmitSolution", chosen), timeout
        )
        return {"ok": True, "submitted_type": "AutoTap", "solution_index": index}

    def _cmd_submit_payment_action(self, command: dict, timeout: float | None) -> dict:
        """Activate one mana ability of a PayCostsRequest that has no Auto Pay
        (Plugin.Payment.cs): a copy of the action keeping only the option that
        makes the requested color, as the client's ManaSelectionFlow submits it."""
        snapshot = self._require(timeout, "PayCostsRequest")
        payment = field(snapshot.request, "PaymentActions")
        instance_id = int(command.get("instance_id") or 0)
        ability = int(command.get("ability_grp_id") or 0)
        source = next(
            (
                action
                for action in items(field(payment, "Actions"))
                if num(field(action, "InstanceId")) == instance_id
                and (not ability or num(field(action, "AbilityGrpId")) == ability)
            ),
            None,
        )
        if handle(payment) is None or source is None or handle(source) is None:
            raise AdapterError(f"No payment action for instance {instance_id}")
        color = str(command.get("color") or "")
        option = None
        if color:
            # IL2CPP enum names are the CLR ones ("Red"); a protobuf-style
            # "ManaColor_Red" means the same color.
            wanted = color.removeprefix("ManaColor_")
            option = next(
                (
                    candidate
                    for candidate in items(field(source, "ManaPaymentOptions"))
                    if any(
                        enum_name(field(mana, "Color")).removeprefix("ManaColor_") == wanted
                        for mana in items(field(candidate, "Mana"))
                    )
                ),
                None,
            )
            if option is None or handle(option) is None:
                raise AdapterError(f"Instance {instance_id} cannot make {color}")

        def build(ops: _Ops, request: dict) -> None:
            action = ops.new(MESSAGING + "Action", H(handle(source)))
            if option is not None:
                options = ops.get(action, "ManaPaymentOptions")
                ops.call(options, "Clear")
                ops.call(options, "Add", H(handle(option)))
            ops.call(H(handle(payment)), "SubmitAction", action, B(False))

        self._submit(snapshot, build, timeout)
        return {"ok": True, "submitted_type": "PaymentAction", "instance_id": instance_id}

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
                command,
                timeout,
                ("SelectNGroupRequest",),
                "SubmitGroupSelection",
                UINTS(values),
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
        snapshot = self._require(timeout, "CastingTimeOption_ManaTypeRequest", *CASTING_TIME_PARENTS)
        request = snapshot.request
        mana = (
            request
            if snapshot.request_class == "CastingTimeOption_ManaTypeRequest"
            else next(
                (
                    c
                    for c in items(field(request, "ChildRequests"))
                    if short_class(c) == "CastingTimeOption_ManaTypeRequest"
                ),
                None,
            )
        )
        if mana is None:
            raise AdapterError(
                f"Pending is {snapshot.request_class}, no CastingTimeOption_ManaTypeRequest child"
            )
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
        self._submit(
            snapshot, lambda ops, request: ops.call(request, "Cancel" if cancel else "AutoRespond"), timeout
        )
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
        ("abilityGrpId", "AbilityGrpId"),
        ("sourceId", "SourceId"),
        ("alternativeGrpId", "AlternativeGrpId"),
        ("facetId", "FacetId"),
        ("uniqueAbilityId", "UniqueAbilityId"),
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


def same_damage_recipient(selected: Any, legal: dict) -> bool:
    """Compare combat identities even when Arena copies a recipient object."""
    if not isinstance(selected, dict):
        return False
    # A handle-only $ref still identifies the exact same native object.
    if handle(selected) is not None and handle(selected) == handle(legal):
        return True
    # The acknowledged declaration can contain a fresh DamageRecipient with
    # the same oneof value as the legal menu. Its object handle is not its
    # player/planeswalker identity, and must not force another UpdateAttacker.
    for member in ("PlayerSystemSeatId", "PlaneswalkerInstanceId", "BattleInstanceId", "TeamId"):
        identity = recipient_id(legal, member)
        if identity is not None and identity > 0:
            return recipient_id(selected, member) == identity
    return False


def replacement_display(replacement: Any) -> str:
    if not isinstance(replacement, dict):
        return "" if replacement is None else str(replacement)
    parts = [
        f"{camel_case(k)}={v}"
        for k, v in replacement.items()
        if not k.startswith("$") and isinstance(v, (int, str))
    ]
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


def _request_prompt(request: dict | None) -> dict | None:
    """A request's GRE prompt ({promptId, parameters}), from BaseUserRequest.Prompt; None without one."""
    prompt = field(request, "Prompt")
    if not isinstance(prompt, dict) or num(field(prompt, "PromptId")) <= 0:
        return None
    # The reflected shape ``arena_prompts`` reads (promptId, parameters: parameterName, value...).
    plain = plain_object(prompt)
    return plain if isinstance(plain, dict) else None


def cost_selection_request(request: dict | None) -> dict | None:
    """The engine's effect-cost selection child, such as tapping creatures to crew."""
    effect = field(request, "EffectCost")
    selection = field(effect, "CostSelection")
    if short_class(selection) == "SelectNRequest" and enum_name(field(selection, "IdType")) == "InstanceId":
        return selection
    return None


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
    return next(
        (c for c in items(field(request, "ChildRequests")) if short_class(c) == "AutoTapActionsRequest"), None
    )


def numeric_values(request: dict) -> list[int]:
    """Plugin.EnumerateNumericInputValues."""
    low, high = num(field(request, "Min")), num(field(request, "Max"))
    step = num(field(request, "StepSize")) or 1
    disallowed = {num(v) for v in items(field(request, "DisallowedValues"))}
    values = []
    value = low
    while value <= high and len(values) < MAX_NUMERIC_INPUT_ENTRIES:
        if (
            value not in disallowed
            and not (field(request, "DisallowEven") and value % 2 == 0)
            and not (field(request, "DisallowOdd") and value % 2 == 1)
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

    def add(
        child: dict,
        index: int,
        kind: str,
        label: str,
        method: str,
        args: list[dict],
        option_index: int | None = None,
        grp_id: int = 0,
        **extra: Any,
    ) -> dict:
        payload: dict[str, Any] = {
            "actionType": "CastingTimeOption",
            "choiceKind": kind,
            "requestClass": short_class(child),
            "childIndex": index,
            "label": label,
        }
        if option_index is not None:
            payload["optionIndex"] = option_index
        if grp_id:
            payload["grpId"] = grp_id
        if num(field(child, "SourceId")):
            payload["sourceId"] = num(field(child, "SourceId"))
        payload.update(extra)
        entries.append(
            {
                "payload": payload,
                "child_handle": handle(child),
                "child_class": short_class(child),
                "method": method,
                "args": args,
            }
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
                add(
                    child,
                    index,
                    "modal",
                    f"Mode {option + 1}",
                    "SubmitModal",
                    [UINTS([option_grp])],
                    option,
                    option_grp,
                    **extra,
                )
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
                add(
                    child,
                    index,
                    "choose_or_cost",
                    f"Choice {option + 1}",
                    "SubmitChoice",
                    [UINTS([selection])],
                    option,
                    grp,
                    **extra,
                )
        elif kind == "CastingTimeOption_DoneRequest":
            payload = add(child, index, "done", "Done", "SubmitDone", [])
            costs = mana_costs(field(child, "ManaCost"))
            if costs:
                payload["manaCost"] = costs
        elif kind in (
            "CastingTimeOption_TimingPermissionRequest",
            "CastingTimeOption_KickerRequest",
            "CastingTimeOption_AdditionalCostRequest",
        ):
            choice, label, method = {
                "CastingTimeOption_TimingPermissionRequest": (
                    "timing_permission",
                    "Timing Permission",
                    "SubmitFlash",
                ),
                "CastingTimeOption_KickerRequest": ("kicker", "Kicker", "SubmitKicked"),
                "CastingTimeOption_AdditionalCostRequest": (
                    "additional_cost",
                    "Additional Cost",
                    "SubmitAdditionalCost",
                ),
            }[kind]
            payload = add(child, index, choice, label, method, [], grp_id=grp)
            costs = mana_costs(field(child, "ManaCost"))
            if costs:
                payload["manaCost"] = costs
        elif kind == "CastingTimeOption_CostKeywordRequest":
            add(
                child,
                index,
                "cost_keyword",
                enum_name(field(child, "OptionType")),
                "SubmitKeywordAction",
                [],
                grp_id=grp,
            )
        elif kind == "CastingTimeOption_NumericInputRequest":
            fixed = num(field(child, "Min")) == num(field(child, "Max"))
            for value in [num(field(child, "Min"))] if fixed else numeric_values(child):
                extra = {"numericValue": value}
                if not fixed:
                    extra.update(min=num(field(child, "Min")), max=num(field(child, "Max")))
                add(
                    child,
                    index,
                    "numeric_input",
                    f"Value {value}" if fixed else f"X = {value}",
                    "SubmitX",
                    [U(value)],
                    grp_id=grp,
                    **extra,
                )
        elif kind == "CastingTimeOption_Replicate":
            low, high = num(field(child, "Min")), num(field(child, "Max"))
            for value in range(low, min(high, low + MAX_NUMERIC_INPUT_ENTRIES - 1) + 1):
                extra = {"numericValue": value}
                if low != high:
                    extra.update(min=low, max=high)
                add(child, index, "replicate", f"Replicate {value}", "SubmitValue", [U(value)], **extra)
        elif kind == "CastingTimeOption_SpecializeRequest":
            for color in items(field(child, "SelectableColors")):
                add(
                    child,
                    index,
                    "specialize",
                    f"Specialize: {enum_name(color)}",
                    "SubmitSpecialization",
                    [I(num(color))],
                    grp_id=num(field(child, "SourceAbilityId")),
                    colorName=enum_name(color),
                    colorValue=num(color),
                )
        elif kind == "CastingTimeOption_ManaTypeRequest":
            defaults = []
            for inner in items(field(child, "InnerRequests")):
                options = items(field(inner, "ManaColorOptions"))
                if options:
                    defaults.append(options[max(0, min(num(field(inner, "DefaultIndex")), len(options) - 1))])
            names = [enum_name(c) for c in defaults]
            add(
                child,
                index,
                "mana_type",
                f"Mana types: {','.join(names)}",
                "SubmitSelection",
                [LIST([I(num(c)) for c in defaults])],
                colors=names,
            )
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


def build_decision_context(
    request_type: str, request_class: str, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Plugin.BuildPendingRequestDecisionContext."""
    context: dict[str, Any] = {"requestType": request_type, "requestClass": request_class}
    mapped = DECISION_TYPES.get(request_type)
    if mapped:
        context["type"] = mapped
    for name in (
        "prompt",
        "promptText",
        "message",
        "messageText",
        "help",
        "helpText",
        "sourceId",
        "grpId",
        "abilityGrpId",
        "zoneId",
        "count",
        "min",
        "max",
        "minCount",
        "maxCount",
        "amount",
        "total",
        "ids",
        "options",
        "targets",
        "validTargets",
        "targetsToSelect",
        "qualifiedTargets",
        "attackers",
        "qualifiedAttackers",
        "blockers",
        "qualifiedBlockers",
        "groups",
        "counterTypes",
        "mulliganCount",
        "freeMulliganCount",
    ):
        if payload.get(name) is not None:
            context[name] = payload[name]
    return context if len(context) > 2 else None
