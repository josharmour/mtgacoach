"""Public assignment identities witnessed on the native bridge's actual submit path."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from arenamcp.mac_bridge_adapter import Snapshot, attacker_nodes, enum_name, field, handle

Edge = tuple[int, str, int]
RequestKey = tuple[int, int]

_MISSING = object()
_active = ContextVar("combat_recording", default=None)


def _number(value: Any) -> int:
    if type(value) is not int or not 0 < value <= 0xFFFFFFFF:
        raise ValueError("unresolved identity")
    return value


def _list(node: Any) -> list:
    if not isinstance(node, dict):
        raise ValueError("missing collection")
    values, count = node.get("$items"), node.get("$n")
    if not isinstance(values, list) or type(count) is not int or count != len(values) or count > 64:
        raise ValueError("incomplete collection")
    return values


def _recipient(node: dict) -> tuple[str, int]:
    if not isinstance(node, dict):
        raise ValueError("unresolved recipient")
    types = {
        "player": "PlayerSystemSeatId",
        "planeswalker": "PlaneswalkerInstanceId",
        "battle": "BattleInstanceId",
    }
    kind = enum_name(field(node, "Type")).lower()
    if kind not in types:
        raise ValueError("unsupported recipient")
    member = types[kind]
    case = enum_name(field(node, "IdCase"))
    if case and case != member:
        raise ValueError("conflicting recipient")
    supplied = [
        (key, field(node, key)) for key in (*types.values(), "TeamId") if field(node, key) not in (None, 0)
    ]
    if len(supplied) > 1 or supplied and supplied[0][0] != member:
        raise ValueError("ambiguous recipient")
    value = field(node, member)
    if case == member and "id_" in node:
        if value is not None and node["id_"] != value:
            raise ValueError("conflicting recipient oneof value")
        value = node["id_"]
    return kind, _number(value)


def _attack_menu(request):
    expanded = {}
    for member in ("QualifiedAttackers", "Attackers"):
        for node in _list(field(request, member)):
            identity = handle(node)
            if identity is not None and field(node, "AttackerInstanceId") is not None:
                if expanded.setdefault(identity, node) != node:
                    raise ValueError("contradictory native attacker handle")
    menu, selected, actors = [], [], set()
    recipients_by_handle = {}
    for node in attacker_nodes(request, "QualifiedAttackers"):
        actor = _number(field(node, "AttackerInstanceId"))
        if actor in actors:
            raise ValueError("duplicate attacker")
        actors.add(actor)
        for target in _list(field(node, "LegalDamageRecipients")):
            recipient = _recipient(target)
            menu.append((actor, *recipient))
            if handle(target) is not None:
                if recipients_by_handle.setdefault((actor, handle(target)), recipient) != recipient:
                    raise ValueError("shared recipient handle names different identities")
    declared = set()
    for node in attacker_nodes(request, "Attackers"):
        actor = _number(field(node, "AttackerInstanceId"))
        if actor not in actors or actor in declared:
            raise ValueError("unresolved declaration")
        declared.add(actor)
        target = field(node, "SelectedDamageRecipient", _MISSING)
        if target is not None:
            if isinstance(target, dict) and target.get("$ref") is True:
                recipient = recipients_by_handle[actor, handle(target)]
            else:
                recipient = _recipient(target)
            selected.append((actor, *recipient))
    return actors, menu, selected


def _block_menu(request):
    menu, selected, actors = [], [], set()
    for node in _list(field(request, "AllBlockers")):
        actor = _number(field(node, "BlockerInstanceId"))
        if actor in actors:
            raise ValueError("duplicate blocker")
        actors.add(actor)
        for dest, member in ((menu, "AttackerInstanceIds"), (selected, "SelectedAttackerInstanceIds")):
            for target in _list(field(node, member)):
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
        actors, options, selected = (_attack_menu if attack else _block_menu)(snapshot.request)
        key = tuple(_number(value) for value in snapshot.original_request_key)
        if key != (self.confirm if update is None else self.start):
            raise ValueError("native snapshot diverged from the planned request")
        for edges in (options, selected):
            if len(edges) > 128 or len(edges) != len(set(edges)):
                raise ValueError("ambiguous or unbounded menu")
        if update is not None:
            if self.update is not None or selected or not update or not options:
                raise ValueError("only a single complete update is witnessed")
            if attack:
                submitted = [(field(n, "AttackerInstanceId"), *_recipient(r)) for n, r in update]
            else:
                if any(not rows for _, rows in update):
                    raise ValueError("withdrawal is outside the supported declaration")
                submitted = [
                    (field(n, "BlockerInstanceId"), "attacker", a) for n, rows in update for a in rows
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
            name in trigger and trigger[name] != value
            for name, value in zip(("game_state_id", "msg_id"), key, strict=True)
        ):
            recording.invalid = True
    if all(type(value) is int and value > 0 for value in key):
        recording.start = recording.confirm = key


def bind_combat_confirmation(key):
    recording = _active.get()
    if recording is not None:
        recording.confirm = key


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
