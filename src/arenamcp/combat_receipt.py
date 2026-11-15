"""Public assignment identities witnessed on the native bridge's actual submit path."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from arenamcp.mac_bridge_adapter import Snapshot, enum_name, short_class

Edge = tuple[int, str, int]
RequestKey = tuple[int, int]

_MISSING = object()
_active = ContextVar("combat_recording", default=None)


def _number(value: Any) -> int:
    if type(value) is not int or not 0 < value <= 0xFFFFFFFF:
        raise ValueError("unresolved identity")
    return value


def _member(node, name, default=None):
    # The gameplay reader accepts the first matching C# backing-field alias.
    # A recorded identity must have one unambiguous native source.
    if not isinstance(node, dict):
        raise ValueError("unreadable native object")
    camel = name[:1].lower() + name[1:]
    values = [node[key] for key in (name, camel + "_", "_" + camel, camel, "m_" + name) if key in node]
    if len(values) > 1:
        raise ValueError("ambiguous native member")
    return values[0] if values else default


def _handle(node, *, allow_ref=False):
    if not isinstance(node, dict):
        raise ValueError("missing native object")
    if "$ref" in node and (not allow_ref or node["$ref"] is not True or set(node) - {"$h", "$c", "$ref"}):
        raise ValueError("ambiguous native reference")
    return _number(node.get("$h"))


def _list(node: Any) -> list:
    _handle(node)
    values, count = node.get("$items"), node.get("$n")
    if not isinstance(values, list) or type(count) is not int or count != len(values) or count > 64:
        raise ValueError("incomplete collection")
    return values


def _recipient(node: dict) -> tuple[str, int]:
    _handle(node)
    types = {
        "player": "PlayerSystemSeatId",
        "planeswalker": "PlaneswalkerInstanceId",
        "battle": "BattleInstanceId",
    }
    kind = enum_name(_member(node, "Type")).lower()
    if kind not in types:
        raise ValueError("unsupported recipient")
    member = types[kind]
    case = enum_name(_member(node, "IdCase"))
    if case and case != member:
        raise ValueError("conflicting recipient")
    supplied = [
        (key, _member(node, key))
        for key in (*types.values(), "TeamId")
        if _member(node, key) not in (None, 0)
    ]
    if len(supplied) > 1 or supplied and supplied[0][0] != member:
        raise ValueError("ambiguous recipient")
    value = _member(node, member)
    oneof = _member(node, "Id")
    if oneof is not None:
        if case != member or value is not None and _number(oneof) != _number(value):
            raise ValueError("conflicting recipient oneof value")
        value = oneof
    identity = _number(value)
    if kind == "player" and identity not in (1, 2):
        raise ValueError("unknown player seat")
    return kind, identity


def _attack_menu(request):
    expanded, collections = {}, {}
    for member in ("QualifiedAttackers", "Attackers"):
        nodes = _list(_member(request, member))
        collections[member] = []
        for node in nodes:
            identity = _handle(node, allow_ref=True)
            collections[member].append(identity)
            if node.get("$ref") is not True:
                if identity in expanded:
                    raise ValueError("duplicate expanded object handle")
                expanded[identity] = node
    menu, selected, actors = [], [], set()
    recipients_by_handle = {}
    offered_handles = set()
    for identity in collections["QualifiedAttackers"]:
        node = expanded[identity]
        actor = _number(_member(node, "AttackerInstanceId"))
        if actor in actors:
            raise ValueError("duplicate attacker")
        actors.add(actor)
        for target in _list(_member(node, "LegalDamageRecipients")):
            recipient = _recipient(target)
            menu.append((actor, *recipient))
            target_handle = _handle(target)
            offered_handles.add((actor, target_handle))
            if (
                target_handle in expanded
                or recipients_by_handle.setdefault(target_handle, recipient) != recipient
            ):
                raise ValueError("shared recipient handle names different identities")
    declared = set()
    for identity in collections["Attackers"]:
        node = expanded[identity]
        actor = _number(_member(node, "AttackerInstanceId"))
        if actor not in actors or actor in declared:
            raise ValueError("unresolved declaration")
        declared.add(actor)
        target = _member(node, "SelectedDamageRecipient", _MISSING)
        if target is not None:
            if isinstance(target, dict) and target.get("$ref") is True:
                target_handle = _handle(target, allow_ref=True)
                if (actor, target_handle) not in offered_handles:
                    raise ValueError("selection references another attacker's recipient")
                recipient = recipients_by_handle[target_handle]
            else:
                recipient = _recipient(target)
            selected.append((actor, *recipient))
    return actors, menu, selected


def _block_menu(request):
    menu, selected, actors = [], [], set()
    handles = set()
    for node in _list(_member(request, "AllBlockers")):
        identity = _handle(node)
        if identity in handles:
            raise ValueError("duplicate blocker handle")
        handles.add(identity)
        actor = _number(_member(node, "BlockerInstanceId"))
        if actor in actors:
            raise ValueError("duplicate blocker")
        actors.add(actor)
        for dest, member in ((menu, "AttackerInstanceIds"), (selected, "SelectedAttackerInstanceIds")):
            for target in _list(_member(node, member)):
                dest.append((actor, "attacker", _number(target)))
    return actors, menu, selected


@dataclass(frozen=True)
class CombatReceipt:
    kind: str
    offered_request: RequestKey
    submitted_request: RequestKey
    options: tuple[Edge, ...]
    chosen: tuple[Edge, ...]

    def option_id(self, edge: Edge) -> str:
        actor, recipient_kind, recipient = edge
        verb = "attack" if self.kind == "DeclareAttackers" else "block"
        return f"{verb}:{actor}:{recipient_kind}:{recipient}"


class _Recording:
    def __init__(self):
        from arenamcp.match_packets import get_current_packet

        self.packet = get_current_packet()
        self.state = None
        self.start = None
        self.confirm = None
        self.update = None
        self.receipt = None
        self.invalid = False

    def observe(self, snapshot: Snapshot, update: list | None) -> None:
        if self.invalid or self.receipt is not None:
            self.invalid = True
            self.receipt = None
            return
        attack = snapshot.request_class == "DeclareAttackerRequest"
        kind = "DeclareAttackers" if attack else "DeclareBlockers"
        if not attack and snapshot.request_class != "DeclareBlockersRequest":
            raise ValueError("wrong request")
        original = snapshot.original_message
        original_handle = _handle(original)
        if original_handle == _handle(snapshot.request) or short_class(original) != "GREToClientMessage":
            raise ValueError("invalid original GRE message")
        attached = _member(snapshot.request, "OriginalMessage", _MISSING)
        if attached is not _MISSING and (
            _handle(attached, allow_ref=True) != original_handle
            or "$c" in attached
            and short_class(attached) != short_class(original)
        ):
            raise ValueError("different original GRE message")
        original_type = _member(original, "Type", _MISSING)
        if (
            original_type is not _MISSING
            and enum_name(original_type).removeprefix("GREMessageType_") != kind + "Req"
        ):
            raise ValueError("original GRE message belongs to a different request family")
        key = tuple(_number(_member(original, name)) for name in ("GameStateId", "MsgId"))
        if isinstance(attached, dict) and not attached.get("$ref"):
            for name, value in zip(("GameStateId", "MsgId"), key, strict=True):
                other = _member(attached, name, _MISSING)
                if other is not _MISSING and _number(other) != value:
                    raise ValueError("conflicting original GRE message identity")
        actors, options, selected = (_attack_menu if attack else _block_menu)(snapshot.request)
        if key != (self.confirm if update is None else self.start):
            raise ValueError("native snapshot diverged from the planned request")
        for edges in (options, selected):
            if len(edges) > 128 or len(edges) != len(set(edges)):
                raise ValueError("ambiguous or unbounded menu")
        if update is not None:
            if self.update is not None or selected or not update or not options:
                raise ValueError("only a single complete update is witnessed")
            if attack:
                submitted = [(_member(n, "AttackerInstanceId"), *_recipient(r)) for n, r in update]
            else:
                if any(not rows for _, rows in update):
                    raise ValueError("withdrawal is outside the supported declaration")
                submitted = [
                    (_member(n, "BlockerInstanceId"), "attacker", a) for n, rows in update for a in rows
                ]
            if len(submitted) != len(set(submitted)) or not set(submitted) <= set(options):
                raise ValueError("submission outside offered menu")
            self.update = kind, key, actors, tuple(options), set(submitted)
            return
        if self.update is None and selected:
            raise ValueError("the existing selections were not submitted by this declaration")
        offered = key
        if self.update:
            initial_kind, offered, initial_actors, original, submitted = self.update
            # A declaration update causes one new GRE state and reissues the request.
            if (
                kind != initial_kind
                or key[0] != offered[0] + 1
                or key[1] <= offered[1]
                or actors != initial_actors
            ):
                raise ValueError("unrelated request")
            if set(selected) != submitted or not set(options) <= set(original):
                raise ValueError("different refreshed declaration")
            options = original
        if not options or not set(selected) <= set(options):
            raise ValueError("unwitnessed declaration")
        self.receipt = CombatReceipt(kind, offered, key, tuple(options), tuple(selected))


@contextmanager
def record_native_combat():
    recording = _Recording()
    token = _active.set(recording)
    try:
        yield recording
    finally:
        _active.reset(token)


def bind_combat_state(state, pending):
    recording = _active.get()
    if recording is None:
        return
    if recording.state is not None:
        recording.invalid = True  # recovery/retry on a different declaration
    recording.state = state
    pending = pending if isinstance(pending, dict) else {}
    key = (pending.get("game_state_id"), pending.get("msg_id"))
    trigger = state.get("_bridge_trigger") if isinstance(state, dict) else None
    if isinstance(trigger, dict):
        if any(
            name in trigger and (type(trigger[name]) is not int or trigger[name] != value)
            for name, value in zip(("game_state_id", "msg_id"), key, strict=True)
        ):
            recording.invalid = True
    if all(type(value) is int and value > 0 for value in key):
        recording.start = recording.confirm = key


def bind_combat_confirmation(key):
    recording = _active.get()
    if recording is not None:
        recording.confirm = (
            key if isinstance(key, tuple) and all(type(value) is int for value in key) else None
        )


def discard_native_combat():
    recording = _active.get()
    if recording is not None:
        recording.invalid = True
        recording.receipt = None


def observe_native_combat(snapshot, update=None):
    """Called only after the request-handle-guarded native operation succeeded."""
    recording = _active.get()
    if recording is None:
        return
    try:
        recording.observe(snapshot, update)
    except Exception:
        # A recording defect cannot change the outcome of a native submission.
        discard_native_combat()
