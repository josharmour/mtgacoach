"""get_game_state for the native macOS/Android IL2CPP bridge.

Mirrors the Windows plugin's ``HandleGetGameState``
(bepinex-plugin/MtgaCoachBridge/Plugin.GameState.cs) using only the injected
library's generic reflect ops — no probe changes needed.

Design notes grounded in spikes/mac-il2cpp/probe.cpp:

* The ``get`` op tries the ``get_<Member>`` property getter first and falls
  back to the raw field, so the game's own convenience getters (``Battlefield``,
  ``LocalHand``, ``OpponentHand`` ...) select zones exactly like the plugin.
* The probe builds a FRESH encoder per op (own node/item budget and own cycle
  set), so one reflect_batch can carry every read without budgets interfering.
* Enums serialize as ``{"e": name, "v": n}`` and scalars plainly regardless of
  depth; only nested class/struct expansion consumes depth.
* Objects visited twice inside ONE op collapse to ``{"$h", "$ref": true}``;
  shared references (a player referenced by ActivePlayer) are resolved in
  Python by ``$h`` against the Players dump.
* ``Dictionary<K,V>`` internals have no friendly generic shape, so counters
  come from ``CounterDatas`` (List<CounterData>) rather than ``Counters``.
"""

from __future__ import annotations

from typing import Any

from arenamcp.mac_bridge_adapter import (
    AdapterError,
    _Ops,
    enum_name,
    field,
    handle,
    items,
    num,
)

IDX_GAME_MANAGER = 0
IDX_STATE = 1

# Zone getter -> response key order mirrors Plugin.GameState.SerializeZones.
ZONE_GETTERS: list[tuple[str, str]] = [
    ("Battlefield", "battlefield"),
    ("Stack", "stack"),
    ("LocalHand", "local_hand"),
    ("OpponentHand", "opponent_hand"),
    ("LocalGraveyard", "local_graveyard"),
    ("OpponentGraveyard", "opponent_graveyard"),
    ("Exile", "exile"),
    ("Command", "command"),
    ("LocalLibrary", "local_library"),
    ("OpponentLibrary", "opponent_library"),
]

_IDX_ZONE_START = IDX_STATE + 1
_IDX_PLAYERS = _IDX_ZONE_START + len(ZONE_GETTERS)

_SCALARS = ("Id", "GameWideTurn", "CurrentPhase", "CurrentStep", "Stage")

# Per-op skip lists (probe prunes these member NAMES everywhere in that op's
# tree). Zone ops prune the card->zone back-pointer (would re-walk sibling
# cards through VisibleCards), Dictionary/HashSet internals with no friendly
# dump shape, and MtgEntity webs we do not surface — so embedded
# Owner/Controller player stubs stay ~10 scalars each. Card counters ride
# CounterDatas (a friendly List<CounterData>), which stays un-pruned.
_ENTITY_WEBS = [
    "Counters",
    "TargetedBy",
    "AbilityInstances",
    "Abilities",
    "AbilityOriginalCardGrpIds",
    "ActiveAbilityWords",
    "AffectorOfQualifications",
    "AffectedByQualifications",
    "AffectorOfLinkInfos",
    "AffectedByLinkInfos",
    "GamewideCounts",
    "ReplacementEffects",
]
_ZONE_OP_SKIP = [
    "Zone",
    *_ENTITY_WEBS,
    # Heavy player members inside embedded Owner/Controller stubs.
    "ManaPool",
    "Timers",
    "CommanderIds",
    "DungeonState",
    "Designations",
    "PendingEffects",
    "IdToManaMap",
    "Team",
]
_PLAYERS_OP_SKIP = [
    *_ENTITY_WEBS,
    "CounterDatas",
    "Timers",
    "IdToManaMap",
    "PendingEffects",
    "Team",
]

_ZONE_DEPTH = 9
_ZONE_MAX_ITEMS = 128
_ZONE_MAX_NODES = 24000
_PLAYERS_DEPTH = 6
_PLAYERS_MAX_ITEMS = 8
_SCALAR_DEPTH = 0

_ATTACK_STATES = {"Declared", "Attacking"}
_BLOCK_STATES = {"Declared", "Blocking"}


def zone_index(key: str) -> int:
    """Result index of the zone getter whose response key is `key`."""
    for offset, (_, zone_key) in enumerate(ZONE_GETTERS):
        if zone_key == key:
            return _IDX_ZONE_START + offset
    raise KeyError(key)


def build_state_ops() -> tuple[_Ops, dict[str, int]]:
    """One reflect batch yielding every piece of HandleGetGameState's payload."""
    ops = _Ops()
    game_manager = ops.add("find", **{"class": "GameManager"}, depth=_SCALAR_DEPTH)
    state_ref = ops.add("get", target=game_manager, member="CurrentGameState", depth=_SCALAR_DEPTH)
    index: dict[str, int] = {"game_manager": IDX_GAME_MANAGER, "state": IDX_STATE}

    for offset, (getter_name, key) in enumerate(ZONE_GETTERS):
        ops.add(
            "get",
            target=state_ref,
            member=getter_name,
            # Library identities are hidden; only read its handle and count.
            depth=0 if key.endswith("_library") else _ZONE_DEPTH,
            max_nodes=_ZONE_MAX_NODES,
            max_items=_ZONE_MAX_ITEMS,
            skip=list(_ZONE_OP_SKIP),
            optional=True,
        )
        index[key] = _IDX_ZONE_START + offset

    ops.add(
        "get",
        target=state_ref,
        member="Players",
        depth=_PLAYERS_DEPTH,
        max_nodes=12000,
        max_items=_PLAYERS_MAX_ITEMS,
        skip=list(_PLAYERS_OP_SKIP),
        optional=True,
    )
    index["players"] = _IDX_PLAYERS

    for member in _SCALARS:
        ops.add("get", target=state_ref, member=member, depth=_SCALAR_DEPTH, optional=True)
        index[member] = len(ops.ops) - 1

    ops.add("get", target=state_ref, member="ActivePlayer", depth=_SCALAR_DEPTH)
    index["active_player"] = len(ops.ops) - 1
    ops.add("get", target=state_ref, member="DecidingPlayer", depth=_SCALAR_DEPTH)
    index["deciding_player"] = len(ops.ops) - 1

    # TotalCardCount includes hidden cards. VisibleCards / a truncated CardIds
    # reflection is not a count of the zone. Keep these cheap scalar reads in
    # the same batch (no extra bridge round trips).
    for _, key in ZONE_GETTERS:
        ops.add("get", target={"ref": index[key]}, member="TotalCardCount", depth=2, optional=True)
        index[f"{key}_count"] = len(ops.ops) - 1

    return ops, index


def fetch_game_state(send: Any, timeout: float | None) -> dict[str, Any]:
    """Run the state batch through the raw bridge `send` and assemble the payload."""
    try:
        ops, index = build_state_ops()
        response = send({"action": "reflect_batch", "ops": ops.ops}, timeout)
    except AdapterError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # defensive: never crash the bridge pump
        return {"ok": False, "error": f"game state assembly failed: {exc}"}
    if not isinstance(response, dict) or not response.get("ok"):
        error = response.get("error") if isinstance(response, dict) else None
        return {"ok": False, "error": error or "reflect_batch failed"}
    try:
        return assemble_state(response.get("results") or [], index)
    except Exception as exc:  # defensive: never crash the bridge pump
        return {"ok": False, "error": f"game state assembly failed: {exc}"}


def assemble_state(results: list[Any], index: dict[str, int]) -> dict[str, Any]:
    """Shape one reflect_batch's results into the plugin's get_game_state payload."""

    def result(key: str) -> Any:
        position = index.get(key)
        if position is None or position >= len(results):
            return None
        return results[position]

    game_manager = result("game_manager")
    if not isinstance(game_manager, dict) or "$c" not in game_manager:
        return {"ok": False, "error": "GameManager not found"}

    state_node = result("state")
    if not isinstance(state_node, dict) or "$c" not in state_node:
        return {"ok": False, "error": "No active game state"}

    # -- players -----------------------------------------------------------
    players_node = result("players")
    player_rows: list[dict[str, Any]] = []
    players_by_handle: dict[int, dict[str, Any]] = {}

    for player in items(players_node):
        if not isinstance(player, dict):
            continue
        player_num = enum_name(field(player, "ClientPlayerEnum"))
        row: dict[str, Any] = {
            "seat_id": num(field(player, "ControllerId")),
            "life_total": num(field(player, "LifeTotal")),
            "is_local": player_num == "LocalPlayer",
            "status": enum_name(field(player, "Status")),
            "mulligan_count": num(field(player, "MulliganCount")),
            "timeout_count": num(field(player, "TimeoutCount")),
            "entity_id": num(field(player, "InstanceId")),
        }
        mana: dict[str, int] = {}
        for entry in items(field(player, "ManaPool")):
            color = enum_name(field(entry, "Color"))
            if color:
                mana[color] = mana.get(color, 0) + num(field(entry, "Count"))
        if mana:
            row["mana_pool"] = mana
        commanders = _uint_list(field(player, "CommanderIds"))
        if commanders:
            row["commander_ids"] = commanders
        dungeon = field(player, "DungeonState")
        dungeon_grp = num(field(dungeon, "DungeonGrpId")) if isinstance(dungeon, dict) else 0
        if dungeon_grp:
            row["dungeon"] = {
                "dungeon_grp_id": dungeon_grp,
                "room_grp_id": num(field(dungeon, "CurrentRoomGrpId")),
            }
        designations = [
            name
            for name in (enum_name(field(datum, "Type")) for datum in items(field(player, "Designations")))
            if name
        ]
        if designations:
            row["designations"] = designations
        player_rows.append(row)
        node_handle = handle(player)
        if node_handle is not None:
            players_by_handle[node_handle] = row

    # -- turn --------------------------------------------------------------
    turn = {
        "turn_number": num(result("GameWideTurn")),
        "phase": enum_name(result("CurrentPhase")),
        "step": enum_name(result("CurrentStep")),
        "stage": enum_name(result("Stage")),
        "game_state_id": num(result("Id")),
        "active_player": _seat_of(result("active_player"), players_by_handle),
        "deciding_player": _seat_of(result("deciding_player"), players_by_handle),
    }

    # -- zones -------------------------------------------------------------
    zones: dict[str, Any] = {}
    for _, key in ZONE_GETTERS:
        zone_node = result(key)
        if not isinstance(zone_node, dict) or "$c" not in zone_node:
            continue
        cards: list[dict[str, Any]] = []
        for card_node in items(field(zone_node, "VisibleCards")):
            if isinstance(card_node, dict) and "$c" in card_node:
                cards.append(_card_entry(card_node))
        zones[key] = {
            "zone_id": num(field(zone_node, "Id")),
            "total_count": _zone_count(result(f"{key}_count")),
            "cards": cards,
        }

    response: dict[str, Any] = {"ok": True, "turn": turn, "players": player_rows}
    if zones:
        response["zones"] = zones

    # Combat maps derived from per-card combat state (mirrors the plugin's
    # attack_info/block_info built from gs.AttackInfo/gs.BlockInfo).
    attacking: dict[str, str] = {}
    blocked_by: dict[str, list[int]] = {}
    battlefield_cards = zones.get("battlefield", {}).get("cards", [])
    for card in battlefield_cards:
        instance_id = card.get("instance_id")
        if not instance_id:
            continue
        if card.get("is_attacking"):
            target = card.get("attack_target_id") or 0
            attacking[str(instance_id)] = str(target)
        blockers = card.get("blocked_by_ids") or []
        if blockers:
            blocked_by[str(instance_id)] = [int(b) for b in blockers]
    if attacking:
        response["attack_info"] = attacking
    if blocked_by:
        response["block_info"] = blocked_by

    return response


def _zone_count(value: Any) -> int | None:
    """An unreadable counter is unknown, never an empty zone."""
    if isinstance(value, bool):
        return None
    count = _sbi(value) if isinstance(value, dict) else value
    return count if isinstance(count, int) and count >= 0 else None


def _seat_of(node: Any, players_by_handle: dict[int, dict[str, Any]]) -> int:
    """Resolve an MtgPlayer reference dump to its seat id."""
    node_handle = handle(node)
    if node_handle is not None and node_handle in players_by_handle:
        return int(players_by_handle[node_handle]["seat_id"])
    return 0


def _card_entry(node: dict[str, Any]) -> dict[str, Any]:
    """Shape one card from a pruned MtgCardInstance dump."""
    entry: dict[str, Any] = {
        "instance_id": num(field(node, "InstanceId")),
        # GrpId property prefers OverlayGrpId when set; both are plain uints.
        # NOTE: OverlayGrpId is Nullable<uint>; a dump of None means unset.
        # The probe emits Nullable<T> structs with hasValue/value fields.
        # We resolve via _nullable_int below after reading both candidates.
    }
    overlay_grp = _nullable_int(field(node, "OverlayGrpId"))
    entry["grp_id"] = overlay_grp if overlay_grp else num(field(node, "BaseGrpId"))
    entry["base_grp_id"] = num(field(node, "BaseGrpId"))
    entry["object_type"] = enum_name(field(node, "ObjectType"))
    entry["is_tapped"] = bool(field(node, "IsTapped"))

    owner_ref = field(node, "Owner")
    controller_ref = field(node, "Controller")
    entry["owner_entity_id"] = num(field(owner_ref, "InstanceId"))
    entry["controller_entity_id"] = num(field(controller_ref, "InstanceId"))

    power = _sbi(field(node, "Power"))
    toughness = _sbi(field(node, "Toughness"))
    if power is not None:
        entry["power"] = power
    if toughness is not None:
        entry["toughness"] = toughness

    loyalty = _nullable_int(field(node, "Loyalty"))
    defense = _nullable_int(field(node, "Defense"))
    if defense is not None:
        entry["defense"] = defense

    attack_state = enum_name(field(node, "AttackState"))
    block_state = enum_name(field(node, "BlockState"))
    if attack_state in _ATTACK_STATES:
        entry["is_attacking"] = True
        target_id = num(field(node, "AttackTargetId"))
        if target_id:
            entry["attack_target_id"] = target_id
    if block_state in _BLOCK_STATES:
        entry["is_blocking"] = True
    blocked_by_ids = _uint_list(field(node, "BlockedByIds"))
    blocking_ids = _uint_list(field(node, "BlockingIds"))
    if blocked_by_ids:
        entry["blocked_by_ids"] = blocked_by_ids
    if blocking_ids:
        entry["blocking_ids"] = blocking_ids

    if bool(field(node, "HasSummoningSickness")):
        entry["summoning_sickness"] = True
    zone_ref_of_card = field(node, "Zone")
    if enum_name(field(zone_ref_of_card, "Type")) == "PhasedOut":
        entry["is_phased_out"] = True

    damage = num(field(node, "Damage"))
    if damage:
        entry["damage"] = damage
    if bool(field(node, "IsDamagedThisTurn")):
        entry["damaged_this_turn"] = True

    class_level = num(field(node, "ClassLevel"))
    if class_level:
        entry["class_level"] = class_level

    copy_grp = num(field(node, "CopyObjectGrpId"))
    if bool(field(node, "IsCopy")):
        entry["is_copy"] = True
        if copy_grp:
            entry["copied_from_grp_id"] = copy_grp

    card_types = _enum_list(field(node, "CardTypes"))
    subtypes = _enum_list(field(node, "Subtypes"))
    colors = _enum_list(field(node, "Colors"))
    if card_types:
        entry["card_types"] = card_types
    if subtypes:
        entry["subtypes"] = subtypes
    if colors:
        entry["colors"] = colors

    counters: dict[str, int] = {}
    for datum in items(field(node, "CounterDatas")):
        name = enum_name(field(datum, "Type"))
        if name:
            counters[name] = counters.get(name, 0) + num(field(datum, "Count"))
    if counters:
        entry["counters"] = counters

    # Loyalty IS the loyalty counter (the client itself keeps it in
    # Counters[CounterType.Loyalty]). The reflected Nullable<uint> Loyalty
    # field can lose its value to IL2CPP boxing: bug_20261006_135027's
    # Jace token read loyalty 0 beside a Loyalty counter of 1, so combat
    # analysis valued killing it at nothing. Never report a planeswalker's
    # zero from that field; unknown is safer than a false zero.
    counted = next((count for kind, count in counters.items() if kind.lower().endswith("loyalty")), None)
    if counted is not None:
        entry["loyalty"] = counted
    elif loyalty is not None and not (loyalty <= 0 and "Planeswalker" in card_types):
        entry["loyalty"] = loyalty

    production = _enum_list(field(node, "ColorProduction"))
    if production:
        entry["color_production"] = production

    target_ids = _uint_list(field(node, "TargetIds"))
    if target_ids:
        entry["target_ids"] = target_ids

    attached_to_id = num(field(node, "AttachedToId"))
    if attached_to_id:
        entry["attached_to_id"] = attached_to_id

    face_down_reason = enum_name(field(field(node, "FaceDownState"), "reasonFaceDown"))
    if face_down_reason and face_down_reason != "None":
        entry["face_down"] = True

    return entry


def _nullable_int(value: Any) -> int | None:
    """Nullable<int/uint> dump -> int or None."""
    if value is None:
        return None
    if isinstance(value, dict):
        has_value = value.get("hasValue", value.get("HasValue"))
        if has_value is False:
            return None
        for key in ("value", "$", "_value"):
            inner = value.get(key)
            if inner is not None:
                try:
                    return int(inner)
                except (TypeError, ValueError):
                    continue
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sbi(value: Any) -> int | None:
    """StringBackedInt struct dump -> int (None when undefined)."""
    if not isinstance(value, dict):
        return None
    # RawText is the game's canonical representation. Reflecting the nested
    # Nullable<int> can lose its value when IL2CPP boxes it as the underlying
    # int, yielding a misleading zero in DefinedValue. Do not let that zero
    # overwrite real creature stats (or turn an undefined '*' into zero).
    raw = field(value, "RawText")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    return _nullable_int(value.get("DefinedValue", value.get("definedValue")))


def _enum_list(node: Any) -> list[str]:
    """List<SomeEnum> dump -> ["Name", ...]."""
    return [enum_name(v) for v in items(node)]


def _uint_list(node: Any) -> list[int]:
    """List<uint>/HashSet<uint> dump -> [n, ...]."""
    return [num(v) for v in items(node)]
