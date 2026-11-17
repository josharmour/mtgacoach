"""Local-only decision corpus and offline planner replay. Never uploads cases."""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import contextlib
import dataclasses
import gzip
import hashlib
import inspect
import json
import logging
import math
import os
import re
import sys
import threading
import time
from pathlib import Path

PRIVATE = {
    "match_id",
    "arena_match_id",
    "event_id",
    "install_id",
    "account_id",
    "user_id",
    "player_name",
    "opponent_name",
    "display_name",
    "email",
    "username",
    "screen_name",
}


PUBLIC_FORMATS = frozenset(
    {
        "Brawl",
        "Brawl Ladder",
        "Ladder",
        "Standard",
        "Historic",
        "Timeless",
        "Alchemy",
        "Explorer",
        "Draft",
        "Sealed",
        "Limited",
        "",
    }
)


def public_snapshot_event(value, key, item, path):
    """Classify format aliases at three native game-state source roles only.

    The event field is still stripped from exported payloads. This only avoids
    treating a validated public format enum as a customer identity globally.
    """
    native_role = (
        (
            len(path) == 5
            and path[:2] == ("packet", "decisions")
            and type(path[2]) is int
            and path[3:] == ("state_snapshot", "state")
        )
        or path == ("final_state",)
        or (
            len(path) == 4
            and path[0] == "bug_reports"
            and type(path[1]) is int
            and path[2:] == ("report", "game_state")
        )
    )
    return (
        native_role
        and key == "event_id"
        and type(item) is str
        and item in PUBLIC_FORMATS
        and item == value.get("format_name")
    )


def producer_role_label(key, item, path, schema):
    """Recognize only v1 producer-redacted opponent-name source fields."""
    if type(schema) is not int or schema != 1 or key != "opponent_name" or type(item) is not str:
        return False
    if path == ("packet",):
        return item == "opponent"
    role_path = path == ("final_state",) or (
        len(path) == 4
        and path[0] == "bug_reports"
        and type(path[1]) is int
        and path[2:] == ("report", "game_state")
    )
    return role_path and item in {"you", "opponent"}


def producer_role_map(value, key, item, path, schema):
    """Recognize only complete, seat-aligned native producer-redacted maps."""
    role_path = path == ("final_state",) or (
        len(path) == 4
        and path[0] == "bug_reports"
        and type(path[1]) is int
        and path[2:] == ("report", "game_state")
    )
    local = value.get("local_seat_id")
    if (
        type(schema) is not int
        or schema != 1
        or not role_path
        or key != "player_names"
        or not isinstance(item, dict)
        or not item
        or type(local) is not int
        or local not in {1, 2, 3, 4}
    ):
        return False
    seats = set()
    for seat, role in item.items():
        if not (
            (type(seat) is int and seat in {1, 2, 3, 4})
            or (type(seat) is str and seat in {"1", "2", "3", "4"})
        ):
            return False
        number = int(seat)
        if number in seats or type(role) is not str or role != ("you" if number == local else "opponent"):
            return False
        seats.add(number)
    return local in seats


def normalized_key(key):
    return str(key).lower().replace("_", "").replace("-", "")


def private_key(key):
    return normalized_key(key) in {normalized_key(k) for k in PRIVATE} | {
        "playernames",
        "playerid",
        "opponentnames",
        "accountids",
        "usernames",
    }


def discover_identities(value, player=False, *, path=(), schema=None):
    found = set()
    if not path and isinstance(value, dict):
        schema = value.get("schema")
    if isinstance(value, dict):
        for key, item in value.items():
            sensitive = private_key(key) or (player and normalized_key(key) in {"name", "id"})
            if sensitive and (
                public_snapshot_event(value, key, item, path)
                or producer_role_label(key, item, path, schema)
                or producer_role_map(value, key, item, path, schema)
            ):
                continue
            if sensitive:

                def strings(node):
                    if isinstance(node, (str, int, float)) and not isinstance(node, bool):
                        found.add(str(node))
                    elif isinstance(node, dict):
                        for v in node.values():
                            strings(v)
                    elif isinstance(node, list):
                        for v in node:
                            strings(v)

                strings(item)
            else:
                found.update(
                    discover_identities(
                        item, normalized_key(key) == "players", path=path + (key,), schema=schema
                    )
                )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.update(discover_identities(item, player, path=path + (index,), schema=schema))
    return found


def scrub(value, identities=(), *, player=False):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if private_key(key) or (player and normalized_key(key) in {"name", "id"}):
                continue
            result[key] = scrub(item, identities, player=normalized_key(key) == "players")
        return result
    if isinstance(value, list):
        return [scrub(v, identities, player=player) for v in value]
    if isinstance(value, str):
        for identity in sorted(identities, key=len, reverse=True):
            if identity:
                value = re.sub(re.escape(identity), "[redacted]", value, flags=re.I)
    return value


class UnsafeCase(ValueError):
    pass


def sanitize_semantics(value, identities, *, player=False, presentation=False):
    """Remove identity fields; never rewrite game semantics or opaque references."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if private_key(key) or (player and normalized_key(key) in {"name", "id"}):
                continue
            if numeric(str(key)):
                sanitize_semantics(str(key), identities)
            # Generic name fields outside player records must identify game cards.
            if key == "name" and not any(
                k in value for k in ("instance_id", "grp_id", "type_line", "card_types")
            ):
                raise UnsafeCase("untyped name field")
            result[key] = sanitize_semantics(
                item, identities, player=normalized_key(key) == "players", presentation=key == "label"
            )
        return result
    if isinstance(value, list):
        return [sanitize_semantics(v, identities, player=player, presentation=presentation) for v in value]
    if type(value) in (int, float) or (isinstance(value, str) and numeric(value)):
        from decimal import Decimal, InvalidOperation

        for identity in identities:
            try:
                if Decimal(str(value)) == Decimal(identity):
                    raise UnsafeCase("numeric identity overlaps semantic field")
            except InvalidOperation:
                continue
    if isinstance(value, str):
        sanitized = scrub(value, identities)
        if sanitized != value:
            if not presentation:
                raise UnsafeCase("identity overlaps semantic field")
            return sanitized
    return value


# Deliberately bounded replay schema. Unsupported populated containers are rejected,
# not dropped; widening requires a reviewed semantic field contract.
_CARD_TEXT = {
    "name",
    "card_name",
    "oracle_text",
    "type_line",
    "mana_cost",
    "object_kind",
    "zone",
    "visibility",
    "face_down_reason",
}
_CARD_LIST_TEXT = {
    "card_types",
    "subtypes",
    "super_types",
    "keywords",
    "colors",
    "color_production",
    "mechanics",
}
_CARD_NUM = {
    "instance_id",
    "grp_id",
    "base_grp_id",
    "controller_seat_id",
    "owner_seat_id",
    "power",
    "toughness",
    "cmc",
    "damage",
    "zone_id",
    "parent_instance_id",
    "attack_target_id",
    "turn_entered_battlefield",
    "loyalty",
    "class_level",
    "token_creator_id",
    "attached_to_id",
}
_CARD_BOOL = {
    "is_tapped",
    "is_attacking",
    "is_blocking",
    "is_blocked",
    "summoning_sickness",
    "is_copy",
    "face_down",
    "identity_known",
    "is_room",
    "damaged_this_turn",
    "entered_via_play_land",
}
_META_NUM = {
    "instanceId",
    "instance_id",
    "grpId",
    "grp_id",
    "source_id",
    "sourceId",
    "abilityGrpId",
    "alternativeGrpId",
    "alternativeSourceZcid",
    "_source_id",
    "_ability_grp_id",
    "_alternative_grp_id",
    "source_parent_instance_id",
    "card_grp_id",
    "target_idx",
    "targetIdx",
    "choice",
    "ctoId",
    "childIndex",
    "min",
    "max",
    "lower",
    "upper",
    "minTargets",
    "maxTargets",
    "selected",
    "spec_index",
    "weight",
}
_META_BOOL = {
    "_alternative",
    "alternative",
    "_castable",
    "_unaffordable",
    "hasAutoTap",
    "assumeCanBePaidFor",
    "isRequired",
    "isBatchable",
    "isManaAbilityWithSideEffect",
    "repeatedSelectAllowed",
}
_META_TEXT = {
    "alternativeCostText",
    "ability_text",
    "source_oracle_text",
    "source_card_oracle_text",
    "mana_cost",
    "_mana_cost_str",
    "choiceKind",
}


def numeric(value):
    return type(value) in (int, float) or (
        isinstance(value, str) and re.fullmatch(r"-?\d+(?:\.\d+)?", value) is not None
    )


def numeric_tree(value):
    if isinstance(value, dict):
        return all(str(k).isdecimal() and numeric_tree(v) for k, v in value.items())
    if isinstance(value, list):
        return all(numeric_tree(v) for v in value)
    return numeric(value)


def assert_record_schema(value, context, *, zone=None, local_seat=None):
    if not isinstance(value, dict):
        raise UnsafeCase("expected typed container")
    for key, item in value.items():
        if private_key(key) or (context == "player" and key in {"name", "id"}):
            continue
        from arenamcp.benchmark_schema import extended_field

        if extended_field(context, key, item):
            continue
        valid = False
        if context == "card":
            if key == "single_face_source":
                # The native source producer marks only unresolved or linked prints.
                valid = item is False
            elif key == "generated":
                valid = item is True
            elif key == "public_native_print":
                from arenamcp.card_facts.source_identity import legacy_lookup_hash
                from arenamcp.commentary.observations import card_identity_visible, generated_or_changed_print

                gid = value.get("grp_id")
                digest = legacy_lookup_hash(value)
                valid = (
                    type(local_seat) is int
                    and local_seat in (1, 2)
                    and zone in {"battlefield", "stack", "graveyard", "command", "exile"}
                    and bool(value.get("oracle_text"))
                    and "Basic Land" not in str(value.get("type_line") or "")
                    and value.get("object_kind") == "CARD"
                    and type(value.get("owner_seat_id")) is int
                    and value["owner_seat_id"] in (1, 2)
                    and type(value.get("controller_seat_id")) is int
                    and value["controller_seat_id"] == value["owner_seat_id"]
                    and type(gid) is int
                    and gid > 0
                    and card_identity_visible(value, zone)
                    and not generated_or_changed_print(value)
                    and bool(digest)
                    and item == f"{gid}:{digest}"
                )
            elif key in _CARD_TEXT:
                valid = isinstance(item, str)
            elif key in _CARD_LIST_TEXT:
                valid = isinstance(item, list) and all(isinstance(v, str) for v in item)
            elif key in _CARD_NUM:
                valid = item is None or numeric(item)
            elif key in _CARD_BOOL:
                valid = type(item) is bool
            elif key == "counters":
                valid = isinstance(item, dict) and all(
                    k
                    in {
                        "unknown",
                        "Flying",
                        "P1P1",
                        "M1M1",
                        "Loyalty",
                        "Lore",
                        "Charge",
                        "Stun",
                        "Time",
                        "Finality",
                        "Energy",
                        "Quest",
                        "Wish",
                        "Burden",
                        "Plan",
                    }
                    and numeric(v)
                    for k, v in item.items()
                )
        elif context == "meta":
            if key in _META_NUM:
                valid = item is None or numeric(item)
            elif key in _META_BOOL:
                valid = item is None or type(item) is bool
            elif key in _META_TEXT:
                valid = isinstance(item, str)
            elif key == "actionType":
                valid = item in {
                    "ActionType_Cast",
                    "ActionType_Play",
                    "ActionType_Activate",
                    "ActionType_Pass",
                    "ActionType_ActivateMana",
                    "ActionType_Special",
                }
            elif key in {"card", "source_card"}:
                assert_record_schema(item, "card")
                valid = True
        elif context == "player":
            if key in {
                "seat_id",
                "life_total",
                "life_gained_this_turn",
                "life_gained_this_game",
                "lands_played",
                "team_id",
                "controller_seat_id",
                "mulligan_count",
                "timeout_count",
            }:
                valid = numeric(item)
            elif key in {"is_local", "has_enduring_story"}:
                valid = type(item) is bool
            elif key in {"mana_pool", "counters"}:
                valid = isinstance(item, dict) and all(
                    k
                    in {
                        "W",
                        "U",
                        "B",
                        "R",
                        "G",
                        "C",
                        "White",
                        "Blue",
                        "Black",
                        "Red",
                        "Green",
                        "Colorless",
                        "Energy",
                        "Poison",
                    }
                    and numeric(v)
                    for k, v in item.items()
                )
            elif key == "commander_ids":
                valid = isinstance(item, list) and all(numeric(v) for v in item)
            elif key == "status":
                valid = item in {
                    "InGame",
                    "Playing",
                    "PlayerStatus_InGame",
                    "PlayerStatus_Conceded",
                    "Conceded",
                    "Won",
                    "Lost",
                    "",
                }
            elif key == "designations":
                valid = item in ([], {})
        if not valid:
            raise UnsafeCase("unsupported " + context + " field or type: " + key)


def assert_case_schema(snapshot, pending):
    from arenamcp.benchmark_schema import legal_labels, mulligan_context, state_field, target_context
    from arenamcp.decision_snapshot import _STATE_KEYS
    from arenamcp.decisions import PendingDecision, TargetSlot

    if set(snapshot) - {"version", "state"}:
        raise UnsafeCase("snapshot fields")
    state = snapshot["state"]
    if set(state) - set(_STATE_KEYS) - {"zones", "_zone_aliases"}:
        raise UnsafeCase("state fields")
    is_target_request = pending.get("request_type") == "SelectTargets"
    if is_target_request and not target_context(state, pending):
        raise UnsafeCase("unverified target request or menu")
    for key, value in state.items():
        if private_key(key):
            continue
        if key == "decision_context" and is_target_request:
            continue
        if key == "decision_context" and pending.get("request_type") == "Mulligan":
            if not (
                mulligan_context(value)
                and state.get("_bridge_request_type") == "Mulligan"
                and state.get("_bridge_request_class") == "MulliganRequest"
            ):
                raise UnsafeCase("mismatched opening-hand context")
            continue

        def card_check(card, zone="graveyard" if key == "zones" else None):
            try:
                # Nested zone card lists accepted by state_field are both graveyards.
                assert_record_schema(card, "card", zone=zone, local_seat=state.get("local_seat_id"))
                return True
            except (UnsafeCase, TypeError, ValueError):
                return False

        if state_field(key, value, card_check):
            continue
        if key in {"hand", "battlefield", "graveyard", "exile", "command", "stack", "deck_cards"}:
            if not isinstance(value, list):
                raise UnsafeCase("card list")
            for card in value:
                assert_record_schema(card, "card", zone=key, local_seat=state.get("local_seat_id"))
        elif key == "players":
            if not isinstance(value, list):
                raise UnsafeCase("players list")
            for player in value:
                assert_record_schema(player, "player")
        elif key == "turn":
            if not isinstance(value, dict):
                raise UnsafeCase("turn")
            for k, v in value.items():
                if k in {"active_player", "priority_player", "decision_player", "turn_number"} and numeric(v):
                    continue
                if k == "phase" and v in {
                    "None",
                    "Beginning",
                    "Main1",
                    "Combat",
                    "Main2",
                    "Ending",
                    "Phase_Beginning",
                    "Phase_Main1",
                    "Phase_Combat",
                    "Phase_Main2",
                    "Phase_Ending",
                    "",
                }:
                    continue
                if k == "step" and v in {
                    "",
                    "None",
                    "Untap",
                    "Upkeep",
                    "Draw",
                    "Main",
                    "PreCombatMain",
                    "BeginCombat",
                    "DeclareAttack",
                    "DeclareAttackers",
                    "DeclareBlock",
                    "DeclareBlockers",
                    "FirstStrikeDamage",
                    "CombatDamage",
                    "EndCombat",
                    "PostCombatMain",
                    "End",
                    "Cleanup",
                    "Step_None",
                    "Step_Untap",
                    "Step_Upkeep",
                    "Step_Draw",
                    "Step_Main",
                    "Step_PreCombatMain",
                    "Step_BeginCombat",
                    "Step_DeclareAttack",
                    "Step_DeclareAttackers",
                    "Step_DeclareBlock",
                    "Step_DeclareBlockers",
                    "Step_FirstStrikeDamage",
                    "Step_CombatDamage",
                    "Step_EndCombat",
                    "Step_PostCombatMain",
                    "Step_End",
                    "Step_Cleanup",
                }:
                    continue
                if k == "stage" and v in {"Start", "Play"}:
                    continue
                if k == "pending_combat_steps" and isinstance(v, list):
                    for combat_step in v:
                        if not isinstance(combat_step, dict) or set(combat_step) != {
                            "active_player",
                            "step",
                            "turn",
                        }:
                            raise UnsafeCase("combat_step combat shape")
                        if type(combat_step["active_player"]) is not int or combat_step[
                            "active_player"
                        ] not in {
                            1,
                            2,
                        }:
                            raise UnsafeCase("combat_step combat seat")
                        if type(combat_step["turn"]) is not int or combat_step["turn"] < 0:
                            raise UnsafeCase("combat_step combat turn")
                        if combat_step["step"] not in {"Step_DeclareAttack", "Step_DeclareBlock"}:
                            raise UnsafeCase("combat_step combat step")
                    continue
                raise UnsafeCase("turn field")
        elif key == "_zone_aliases":
            if not isinstance(value, list) or any(
                v not in {"battlefield", "hand", "my_hand", "graveyard", "exile", "command", "stack"}
                for v in value
            ):
                raise UnsafeCase("zone aliases")
        elif key == "format_name":
            if type(value) is not str or value not in PUBLIC_FORMATS:
                raise UnsafeCase("format")
        elif key == "legal_actions":
            if not legal_labels(value, state):
                raise UnsafeCase("legal action renderer grammar")
        elif key in {"legal_actions_raw", "_bridge_actions"}:
            if value is None:
                continue
            if not isinstance(value, list):
                raise UnsafeCase("actions")
            for action in value:
                assert_record_schema(action, "meta")
        elif (
            value in (None, [], {}, False, True)
            or numeric(value)
            or key in {"commander_grp_ids", "commander_casts", "damage_taken"}
            and numeric_tree(value)
        ):
            continue
        else:
            raise UnsafeCase("unsupported populated state container: " + key)
    allowed = {f.name for f in dataclasses.fields(PendingDecision)}
    if set(pending) - allowed:
        raise UnsafeCase("pending fields")
    for key, value in pending.items():
        if key in {"options", "slots"}:
            continue
        if (
            key in {"request_id"}
            and isinstance(value, list)
            and len(value) == 2
            and all(numeric(v) for v in value)
        ):
            continue
        if key in {"min_select", "max_select", "min_weight", "max_weight", "source_instance_id"} and (
            value is None or numeric(value)
        ):
            continue
        if key in {"can_pass", "can_cancel"} and type(value) is bool:
            continue
        if key == "request_type" and value in {
            "ActionsAvailable",
            "Mulligan",
            "Search",
            "OptionalAction",
            "SelectN",
            "SelectTargets",
            "CastingTimeOptions",
            "Group",
        }:
            continue
        if key in {"source_label", "prompt_text", "source_oracle_text"} and isinstance(value, str):
            continue
        if key in {"selection_context", "option_context"} and value is None:
            continue
        raise UnsafeCase("unsupported pending field/type: " + key)
    for option in pending["options"]:
        if set(option) - {"option_id", "label", "payable", "meta"}:
            raise UnsafeCase("option fields")
        if not isinstance(option["option_id"], str) or not isinstance(option["label"], str):
            raise UnsafeCase("option type")
        if option.get("payable") is not None and type(option["payable"]) is not bool:
            raise UnsafeCase("option payable type")
        assert_record_schema(option.get("meta", {}), "meta")
    for slot in pending.get("slots", []):
        if set(slot) - {f.name for f in dataclasses.fields(TargetSlot)} or not all(
            numeric_tree(v) for v in slot.values()
        ):
            raise UnsafeCase("slot")


def typed_decision(data):
    from arenamcp.decisions import DecisionOption, PendingDecision, TargetSlot

    fields = {f.name for f in dataclasses.fields(PendingDecision)}
    values = {k: v for k, v in data.items() if k in fields}
    values["options"] = tuple(DecisionOption(**o) for o in values["options"])
    values["slots"] = tuple(TargetSlot(**s) for s in values.get("slots", []))
    values["request_id"] = tuple(values["request_id"])
    return PendingDecision(**values)


def build_cases(paths):
    cases = []
    manifest = []
    counts = collections.Counter()
    for path in sorted(paths):
        raw = path.read_bytes()
        bundle = json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)
        packet = bundle.get("packet") or {}
        match = bundle.get("arena_match_id") or bundle.get("match_id") or packet.get("match_id")
        if not match:
            counts["missing_match"] += 1
            continue
        group = hashlib.sha256(str(match).encode()).hexdigest()
        # Discover identities across the entire restored bundle before testing any
        # individual case. A malformed table cannot hide evidence for sibling cases.
        from arenamcp.snapshot_transport import expand_bundle_snapshots

        try:
            bundle = expand_bundle_snapshots(bundle)
        except (ValueError, TypeError, RecursionError, OverflowError):
            counts["invalid_snapshot_encoding"] += 1
            continue
        packet = bundle.get("packet") or {}
        identities = discover_identities(bundle)
        for index, decision in enumerate(packet.get("decisions", [])):
            counts["decisions"] += 1
            snapshot = decision.get("state_snapshot")
            pending = decision.get("pending_decision")
            chosen = decision.get("chosen_options")
            if (
                not isinstance(snapshot, dict)
                or snapshot.get("version") != 1
                or not isinstance(snapshot.get("state"), dict)
            ):
                counts["no_v1_snapshot"] += 1
                continue
            if not isinstance(pending, dict) or not isinstance(chosen, list):
                counts["bad_decision"] += 1
                continue
            ids = {o.get("option_id") for o in pending.get("options", []) if isinstance(o, dict)}
            if any(c not in ids for c in chosen):
                counts["unmatched_chosen"] += 1
                continue
            try:
                valid = typed_decision(pending).selection_is_valid(chosen)
            except (TypeError, ValueError, KeyError, AttributeError):
                valid = False
            if not valid:
                counts["invalid_selection"] += 1
                continue
            if bundle.get("result") not in {"win", "loss", "draw", "unknown", None}:
                counts["invalid_result"] += 1
                continue
            try:
                assert_case_schema(snapshot, pending)
                payload = {
                    "snapshot": sanitize_semantics(snapshot, identities),
                    "decision": sanitize_semantics(pending, identities),
                    "chosen": sanitize_semantics(chosen, identities),
                    "result": bundle.get("result"),
                }
                if not typed_decision(payload["decision"]).selection_is_valid(payload["chosen"]):
                    raise UnsafeCase("sanitized selection invalid")
            except (UnsafeCase, TypeError, ValueError, KeyError):
                counts["unsafe_schema_or_identity"] += 1
                continue
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            case_id = hashlib.sha256((group + digest).encode()).hexdigest()
            cases.append(
                dict(
                    case_id=case_id,
                    match_group=group,
                    split=("test" if int(group[:8], 16) % 5 == 0 else "train"),
                    **payload,
                )
            )
            manifest.append(
                dict(
                    case_id=case_id,
                    source=str(path),
                    sha256=hashlib.sha256(raw).hexdigest(),
                    decision_index=index,
                )
            )
            counts["eligible"] += 1
    unique = {c["case_id"]: c for c in cases}
    counts["duplicates"] = len(cases) - len(unique)
    return list(unique.values()), manifest, dict(counts)


class ModelRequired(Exception):
    """Inert generation refusal; replay counters override any production fallback."""


class BackendInterfaceUnsupported(Exception):
    """Unimplemented backend capability is a harness error, not model demand."""


class NoModel:
    model = None
    last_request_metrics = None

    def __init__(self):
        self.generation_attempts = 0
        self.interface_errors = 0

    def available(self):
        return True

    def degraded(self):
        return False

    def complete(self, *args, **kwargs):
        self.generation_attempts += 1
        raise ModelRequired()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        self.interface_errors += 1
        raise BackendInterfaceUnsupported()


_DEFAULT_EMPTY_FACTS_SHA256 = hashlib.sha256(b"{}").hexdigest()
_VALID_CASE_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_VALID_THINKING_CHOICES = ("default", "none", "low", "high")


def _case_decision(case: dict) -> dict:
    decision = case.get("decision")
    return decision if isinstance(decision, dict) else case.get("pending_decision") or {}


def _load_report_helper(name: str):
    try:
        return __import__(f"tools.{name}", fromlist=[name])
    except ImportError:
        import importlib.util

        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def load_cases_file(path: Path) -> tuple[list[dict], str]:
    raw = path.read_bytes()
    corpus_sha256 = hashlib.sha256(raw).hexdigest()
    payload = json.loads(raw.decode("utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("cases"), list):
        cases = list(payload["cases"])
    elif isinstance(payload, list):
        cases = list(payload)
    else:
        raise ValueError("cases file must contain a JSON list or {'cases': [...]}")
    return cases, corpus_sha256


def load_labels(path: Path) -> tuple[dict[str, dict], str]:
    raw = path.read_bytes()
    labels_sha256 = hashlib.sha256(raw).hexdigest()
    payload = json.loads(raw.decode("utf-8"))
    mapping = (
        payload.get("labels")
        if isinstance(payload, dict) and isinstance(payload.get("labels"), dict)
        else payload
    )
    if not isinstance(mapping, dict):
        raise ValueError("labels file must be a dict or {'labels': {...}}")
    active = {
        str(cid): row
        for cid, row in mapping.items()
        if isinstance(row, dict) and row.get("status") != "abstain"
    }
    return active, labels_sha256


def load_card_facts_dir(path: Path | None) -> tuple[dict[str, dict], str]:
    if path is None:
        return {}, _DEFAULT_EMPTY_FACTS_SHA256
    if not path.is_dir():
        raise ValueError(f"card-facts directory not found: {path}")
    from arenamcp.card_facts.schema import card_facts_from_dict

    records: dict[str, dict] = {}
    file_hashes: dict[str, str] = {}
    for item in sorted(path.glob("*.json")):
        if not _VALID_CASE_HEX_RE.fullmatch(item.stem):
            continue
        raw = item.read_bytes()
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict) and "record" in data:
            if data.get("status") not in (None, "ok"):
                continue
            record = data.get("record")
        else:
            record = data
        if not isinstance(record, dict):
            continue
        digest = record.get("rules_hash")
        if not isinstance(digest, str) or not _VALID_CASE_HEX_RE.fullmatch(digest):
            continue
        if digest != item.stem or not record.get("name"):
            continue
        try:
            card_facts_from_dict(record)
        except (TypeError, ValueError, KeyError):
            continue
        records[digest] = record
        file_hashes[digest] = hashlib.sha256(raw).hexdigest()
    if not records:
        return {}, _DEFAULT_EMPTY_FACTS_SHA256
    facts_sha256 = hashlib.sha256(
        json.dumps(file_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return records, facts_sha256


@contextlib.contextmanager
def _patched_card_facts(records_by_hash: dict[str, dict] | None):
    from unittest.mock import patch

    from arenamcp.card_facts.client import hash_for_card

    facts_map = dict(records_by_hash or {})

    def lookup(cards, *, memory_only: bool = False):
        if not facts_map or not cards:
            return {}
        out = {}
        for card in cards:
            digest = hash_for_card(card)
            if digest and digest in facts_map:
                out[digest] = facts_map[digest]
        return out

    def warm(cards, *, alt_hashes=()):
        if not facts_map:
            return 0
        hits = {digest for card in (cards or ()) if (digest := hash_for_card(card)) and digest in facts_map}
        hits.update(h for h in (alt_hashes or ()) if isinstance(h, str) and h in facts_map)
        return len(hits)

    with (
        patch("arenamcp.card_facts.client.facts_for", side_effect=lookup),
        patch("arenamcp.line_search_moves.facts_for", side_effect=lookup),
        patch("arenamcp.card_facts.client.warm_memory_from_disk", side_effect=warm),
        patch("arenamcp.card_facts.client.prefetch_grp_ids", return_value=None),
    ):
        yield


@contextlib.contextmanager
def _scoped_thinking_env(thinking: str):
    key = "ARENAMCP_THINKING_ROWS"
    had_prev = key in os.environ
    prev = os.environ.get(key)
    if thinking in {"none", "low", "high"}:
        os.environ[key] = json.dumps({"*": {"decision": thinking, "plan": thinking}})
    elif had_prev:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        if had_prev:
            os.environ[key] = prev
        else:
            os.environ.pop(key, None)


class BenchmarkModelBackend:
    """Thread-safe per-case wrapper enforcing priority and thinking overrides."""

    def __init__(self, inner_or_factory, *, priority: int | None = 10, thinking: str = "default"):
        self._inner_obj = inner_or_factory() if callable(inner_or_factory) else inner_or_factory
        self._priority = priority
        self._thinking = thinking
        self._lock = threading.Lock()
        self.generation_attempts = 0
        self.interface_errors = 0

    def _inner(self):
        return self._inner_obj

    @property
    def model(self):
        return getattr(self._inner(), "model", None)

    @property
    def last_request_metrics(self):
        return getattr(self._inner(), "last_request_metrics", None)

    @property
    def last_served_model(self):
        return getattr(self._inner(), "last_served_model", None)

    @property
    def last_served_alias(self):
        return getattr(self._inner(), "last_served_alias", None)

    def available(self) -> bool:
        inner = self._inner()
        fn = getattr(inner, "available", None)
        return fn() if callable(fn) else True

    def degraded(self) -> bool:
        inner = self._inner()
        fn = getattr(inner, "degraded", None)
        return fn() if callable(fn) else False

    def complete(self, *args, **kwargs):
        with self._lock:
            self.generation_attempts += 1
        inner = self._inner()
        call_kwargs = dict(kwargs)
        candidate_extras: dict[str, object] = {}
        if self._priority is not None:
            candidate_extras["priority"] = self._priority
        if self._thinking in {"none", "low", "high"}:
            candidate_extras["enable_thinking"] = self._thinking != "none"
            candidate_extras["reasoning_effort"] = self._thinking
        if candidate_extras:
            try:
                sig = inspect.signature(inner.complete)
                accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
                for k, v in candidate_extras.items():
                    if accepts_var_kw or k in sig.parameters:
                        call_kwargs[k] = v
            except (TypeError, ValueError):
                call_kwargs.update(candidate_extras)
        return inner.complete(*args, **call_kwargs)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        with self._lock:
            self.interface_errors += 1
        raise BackendInterfaceUnsupported(name)


def replay(case, *, patch_facts: bool = True):
    from arenamcp.action_planner import ActionPlanner
    from arenamcp.decision_snapshot import game_state_from_snapshot

    decision = typed_decision(_case_decision(case))
    start = time.perf_counter()
    result = {"case_id": case["case_id"], "status": "withheld", "chosen": []}
    # No cached or remote facts silently change the benchmark input.
    facts_ctx = _patched_card_facts(None) if patch_facts else contextlib.nullcontext()
    with facts_ctx:
        try:
            backend = NoModel()
            planner = ActionPlanner(backend)
            chosen = planner.plan_decision_options(decision, game_state_from_snapshot(case["snapshot"]))
            result.update(
                status="answered" if decision.selection_is_valid(chosen) else "invalid", chosen=chosen
            )
        except ModelRequired:
            result["withheld_reason"] = "generation_required"
        except BackendInterfaceUnsupported:
            result.update(status="error", error="BackendInterfaceUnsupported")
        except Exception as error:
            result.update(status="error", error=type(error).__name__)
    # Production may catch a refusal and manufacture a fallback. That is not
    # a deterministic answer independent of generation. Capability failures
    # likewise remain harness errors even if the production probe tolerates them.
    if backend.interface_errors:
        result.update(status="error", error="BackendInterfaceUnsupported", chosen=[])
        result.pop("withheld_reason", None)
    elif backend.generation_attempts:
        result.update(status="withheld", withheld_reason="generation_required", chosen=[])
        result.pop("error", None)
    result["interface_errors"] = backend.interface_errors
    result["generation_attempts"] = backend.generation_attempts
    result["seconds"] = time.perf_counter() - start
    return result


def replay_model_case(
    case,
    backend_factory,
    *,
    planning_timeout: float = 30.0,
    patch_facts: bool = True,
    records_by_hash: dict[str, dict] | None = None,
):
    from arenamcp.action_planner import ActionPlanner
    from arenamcp.decision_snapshot import game_state_from_snapshot

    decision = typed_decision(_case_decision(case))
    start = time.perf_counter()
    result = {"case_id": case["case_id"], "status": "error", "chosen": []}
    backend = backend_factory()
    facts_ctx = _patched_card_facts(records_by_hash) if patch_facts else contextlib.nullcontext()
    llm_failure = ""
    with facts_ctx:
        try:
            planner = ActionPlanner(backend, timeout=planning_timeout)
            chosen = planner.plan_decision_options(decision, game_state_from_snapshot(case["snapshot"]))
            llm_failure = str(getattr(planner, "last_llm_failure", "") or "")
            if backend.interface_errors:
                result.update(status="error", error="BackendInterfaceUnsupported", chosen=[])
            elif llm_failure == "unavailable":
                result.update(status="error", error="ModelUnavailable", chosen=[])
            elif decision.selection_is_valid(chosen):
                result.update(status="answered", chosen=list(chosen))
            else:
                result.update(status="invalid", chosen=[])
        except BackendInterfaceUnsupported:
            result.update(status="error", error="BackendInterfaceUnsupported", chosen=[])
        except Exception as error:
            result.update(status="error", error=type(error).__name__, chosen=[])
    result["interface_errors"] = backend.interface_errors
    result["generation_attempts"] = backend.generation_attempts
    result["llm_failure"] = llm_failure
    result["seconds"] = time.perf_counter() - start
    return result


def score(chosen, label):
    """A label enumerates acceptable selections, not individual interchangeable IDs."""
    key = sorted(chosen)
    if key in [sorted(selection) for selection in label.get("blunders", [])]:
        return "blunder"
    if key in [sorted(selection) for selection in label.get("acceptable", [])]:
        return "acceptable"
    return "unlabelled_choice"


def _score_row(row: dict, labels: dict[str, dict] | None) -> str:
    if row.get("status") != "answered" or not labels or row["case_id"] not in labels:
        return "not_scored"
    return score(row["chosen"], labels[row["case_id"]])


def _selection_sha256(row: dict) -> str:
    payload = sorted(row["chosen"]) if row.get("status") == "answered" else []
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest()


def build_v1_report(
    *,
    results: list[dict],
    corpus_sha256: str,
    configuration_sha256: str,
    facts_sha256: str,
    labels_sha256: str,
    model_layer: str,
    labels: dict[str, dict] | None = None,
) -> dict:
    report_mod = _load_report_helper("benchmark_report")
    v1_rows = [
        {
            "case_id": row["case_id"],
            "status": row["status"],
            "selection_sha256": _selection_sha256(row),
            "seconds": float(row["seconds"]),
            "generation_attempts": int(row["generation_attempts"]),
            "score": _score_row(row, labels),
        }
        for row in results
    ]
    report = {
        "version": 1,
        "corpus_sha256": corpus_sha256,
        "configuration_sha256": configuration_sha256,
        "facts_sha256": facts_sha256,
        "labels_sha256": labels_sha256,
        "model_layer": model_layer,
        "results": v1_rows,
    }
    report_mod.validate(report)
    return report


def _repeat_outcome(row: dict, labels: dict[str, dict]) -> str:
    status = row.get("status")
    if status == "answered":
        return score(row["chosen"], labels[row["case_id"]])
    if status == "withheld":
        return "withheld"
    return "error"


def summarize_benchmark_repeats(
    *,
    cases: list[dict],
    repeat_runs: list[list[dict]],
    corpus_sha256: str,
    configuration_sha256: str,
    labels_sha256: str,
    labels: dict[str, dict],
) -> dict:
    repeats_mod = _load_report_helper("benchmark_repeats")
    labelled_ids = [c["case_id"] for c in cases if c["case_id"] in labels]
    labelled_set = set(labelled_ids)
    trials = [
        {
            "case_id": row["case_id"],
            "repeat": repeat_idx,
            "outcome": _repeat_outcome(row, labels),
            "seconds": float(row["seconds"]),
        }
        for repeat_idx, rows in enumerate(repeat_runs)
        for row in rows
        if row["case_id"] in labelled_set
    ]
    return repeats_mod.summarize_repeats(
        case_ids=labelled_ids,
        repeats=len(repeat_runs),
        trials=trials,
        corpus_sha256=corpus_sha256,
        labels_sha256=labels_sha256,
        configuration_sha256=configuration_sha256,
    )


def _proxy_backend_factory(*, model: str):
    from arenamcp.backends.proxy import ProxyBackend, resolve_online_base_url
    from arenamcp.settings import get_settings

    settings = get_settings()
    license_key = str(settings.get("license_key") or "")
    base_url = resolve_online_base_url(settings.get("gateway_url"))
    if not license_key:
        raise RuntimeError("license_key missing in Coach settings for proxy benchmark")
    tls = threading.local()

    def get_inner():
        inner = getattr(tls, "backend", None)
        if inner is None:
            inner = ProxyBackend.create_online(
                model=model,
                license_key=license_key,
                base_url=base_url,
            )
            tls.backend = inner
        return inner

    return get_inner


def run_benchmark(
    cases: list[dict],
    *,
    corpus_sha256: str,
    labels: dict[str, dict] | None = None,
    labels_sha256: str = "",
    records_by_hash: dict[str, dict] | None = None,
    facts_sha256: str = _DEFAULT_EMPTY_FACTS_SHA256,
    backend_mode: str = "none",
    model: str = "current",
    thinking: str = "default",
    priority: int = 10,
    repeats: int = 1,
    workers: int = 1,
    planning_timeout: float = 30.0,
    inner_backend_factory=None,
) -> tuple[list[list[dict]], dict, dict | None]:
    """Run code-only or model-layer benchmark (1 or more repeats) offline-safe for card facts."""
    if repeats < 1 or repeats > 100:
        raise ValueError("repeats must be 1..100")
    if workers < 1 or workers > 16:
        raise ValueError("workers must be 1..16")
    if thinking not in _VALID_THINKING_CHOICES:
        raise ValueError(f"unsupported thinking policy: {thinking}")

    config_payload = {
        "backend": backend_mode,
        "model": model if backend_mode == "proxy" else "none",
        "thinking": thinking if backend_mode == "proxy" else "none",
        "priority": priority if backend_mode == "proxy" else None,
        "planning_timeout": planning_timeout if backend_mode == "proxy" else None,
    }
    configuration_sha256 = hashlib.sha256(
        json.dumps(config_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    effective_labels_sha = labels_sha256 or _DEFAULT_EMPTY_FACTS_SHA256
    model_layer = "not_run" if backend_mode == "none" else "run"

    if backend_mode == "proxy":
        raw_factory = inner_backend_factory or _proxy_backend_factory(model=model)

        def case_backend_factory():
            return BenchmarkModelBackend(raw_factory, priority=priority, thinking=thinking)

        def run_one(case):
            return replay_model_case(
                case,
                case_backend_factory,
                planning_timeout=planning_timeout,
                patch_facts=False,
            )
    else:

        def run_one(case):
            return replay(case, patch_facts=False)

    repeat_runs: list[list[dict]] = []
    with (
        _patched_card_facts(records_by_hash),
        _scoped_thinking_env(thinking if backend_mode == "proxy" else "default"),
    ):
        for _ in range(repeats):
            if workers == 1:
                rows = [run_one(case) for case in cases]
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                    rows = list(pool.map(run_one, cases))
            repeat_runs.append(rows)

    v1_report = build_v1_report(
        results=repeat_runs[0],
        corpus_sha256=corpus_sha256,
        configuration_sha256=configuration_sha256,
        facts_sha256=facts_sha256,
        labels_sha256=effective_labels_sha,
        model_layer=model_layer,
        labels=labels,
    )
    repeats_summary = None
    if repeats >= 2:
        if not labels:
            raise ValueError("--labels required to compute --repeats-report across >=2 repeats")
        repeats_summary = summarize_benchmark_repeats(
            cases=cases,
            repeat_runs=repeat_runs,
            corpus_sha256=corpus_sha256,
            configuration_sha256=configuration_sha256,
            labels_sha256=effective_labels_sha,
            labels=labels,
        )
    return repeat_runs, v1_report, repeats_summary


def diff_reports(before, after):
    old = {r["case_id"]: r for r in before["results"]}
    new = {r["case_id"]: r for r in after["results"]}
    return {
        "only_before": sorted(old.keys() - new.keys()),
        "only_after": sorted(new.keys() - old.keys()),
        "changed": [
            key
            for key in sorted(old.keys() & new.keys())
            if (old[key]["status"], old[key]["chosen"]) != (new[key]["status"], new[key]["chosen"])
        ],
    }


@contextlib.contextmanager
def _silence_replay_diagnostics():
    """Dedicated-process FD and Python stream suppression, restored on errors.

    Covers direct writes and inherited subprocess stdout/stderr, not arbitrary
    file writes or hostile code. No background work may escape this scope.
    """
    saved = []
    with open(os.devnull, "w") as sink:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            for descriptor in (1, 2):
                duplicate = os.dup(descriptor)
                saved.append((descriptor, duplicate))
                os.dup2(sink.fileno(), descriptor)
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                yield
                sink.flush()
        finally:
            for descriptor, duplicate in reversed(saved):
                try:
                    os.dup2(duplicate, descriptor)
                finally:
                    os.close(duplicate)


def aggregate_only(bundles, *, native_combat=False):
    """Replay in memory and return allowlisted counters, never cases or choices.

    This CLI operation suppresses planner diagnostics process-wide. Run alone
    under OS network denial. NoModel is still the inert generation boundary.
    """
    if not bundles.is_dir():
        raise ValueError("bundle directory missing")
    from unittest.mock import patch

    previous = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        with (
            _silence_replay_diagnostics(),
            patch("arenamcp.match_context._local_card", return_value={}),
            patch(
                "arenamcp.card_db.get_card_database", side_effect=RuntimeError("offline database disabled")
            ),
        ):
            if native_combat:
                from arenamcp.benchmark_combat import aggregate

                with (
                    patch("arenamcp.card_facts.client.facts_for", return_value={}),
                    patch("arenamcp.line_search_moves.facts_for", return_value={}),
                ):
                    return {"native_combat": aggregate(bundles.glob("*.json.gz")), "model_layer": "not_run"}
            cases, _, _ = build_cases(bundles.glob("*.json.gz"))
            counts = {key: 0 for key in ("answered", "withheld", "invalid", "error")}
            seconds = 0.0
            attempts = 0
            for case in cases:
                row = replay(case)
                if row.get("status") not in counts:
                    raise ValueError("invalid replay result")
                duration = row.get("seconds")
                generations = row.get("generation_attempts")
                if (
                    type(duration) not in (int, float)
                    or duration < 0
                    or duration > 86400
                    or not math.isfinite(duration)
                ):
                    raise ValueError("invalid replay timing")
                if type(generations) is not int or not 0 <= generations <= 10000:
                    raise ValueError("invalid replay counter")
                counts[row["status"]] += 1
                seconds += duration
                attempts += generations
            if not math.isfinite(seconds):
                raise ValueError("invalid total timing")
            return {
                "cases": len(cases),
                "counts": counts,
                "seconds": seconds,
                "inert_generation_attempts": attempts,
                "model_layer": "not_run",
                "scope": "snapshot-only facts-empty offline replay; acceptance not assessed",
            }
    finally:
        logging.disable(previous)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundles", type=Path)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--cases-file", type=Path, help="explicit JSON cases file (e.g. blinded.local.json)")
    parser.add_argument("--labels", type=Path, help="official B3 labels JSON file")
    parser.add_argument(
        "--aggregate-only", action="store_true", help="in-memory code replay; no case files or private output"
    )
    parser.add_argument("--code-only", action="store_true")
    parser.add_argument("--backend", choices=("none", "proxy"), default="none")
    parser.add_argument("--model", default="current")
    parser.add_argument("--thinking", choices=_VALID_THINKING_CHOICES, default="default")
    parser.add_argument("--priority", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--planning-timeout", type=float, default=30.0)
    parser.add_argument("--card-facts-dir", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None, help="cap number of cases replayed (smoke test)")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--v1-report", type=Path, help="write strict benchmark_report v1 JSON")
    parser.add_argument("--repeats-report", type=Path, help="write benchmark_repeats aggregate JSON")
    parser.add_argument(
        "--native-combat",
        action="store_true",
        help="compare native combat submissions in aggregate-only mode",
    )
    args = parser.parse_args(argv)
    if args.native_combat and not args.aggregate_only:
        parser.exit(2, "native combat requires aggregate-only mode\n")
    if args.aggregate_only:
        if args.bundles is None or args.cases is not None or args.report is not None:
            parser.exit(2, "aggregate-only requires bundles and forbids case/report files\n")
        try:
            value = (
                aggregate_only(args.bundles, native_combat=True)
                if args.native_combat
                else aggregate_only(args.bundles)
            )
        except Exception:
            parser.exit(2, "offline aggregate replay failed; no cases exported\n")
        print(json.dumps(value, sort_keys=True))
        return
    if args.cases is None and args.cases_file is None:
        parser.exit(2, "cases directory or --cases-file required outside aggregate-only mode\n")
    if args.bundles and args.cases is not None:
        cases, manifest, counts = build_cases(args.bundles.glob("*.json.gz"))
        args.cases.mkdir(parents=True, exist_ok=True)
        (args.cases / "cases.json").write_text(json.dumps(cases))
        (args.cases / "private-source-manifest.json").write_text(json.dumps(manifest, indent=2))
        print(json.dumps(counts))

    if args.code_only or args.backend == "proxy" or args.v1_report or args.repeats_report:
        if args.cases_file is not None:
            cases, corpus_sha256 = load_cases_file(args.cases_file)
        else:
            cases_path = args.cases / "cases.json"
            cases, corpus_sha256 = load_cases_file(cases_path)
        if args.limit is not None:
            cases = cases[: max(0, args.limit)]
            corpus_sha256 = hashlib.sha256(
                json.dumps(cases, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        labels_map = None
        labels_sha = _DEFAULT_EMPTY_FACTS_SHA256
        if args.labels is not None:
            labels_map, labels_sha = load_labels(args.labels)
        records_by_hash, facts_sha = load_card_facts_dir(args.card_facts_dir)
        backend_mode = "none" if args.code_only else args.backend
        repeat_runs, v1_report, repeats_summary = run_benchmark(
            cases,
            corpus_sha256=corpus_sha256,
            labels=labels_map,
            labels_sha256=labels_sha,
            records_by_hash=records_by_hash,
            facts_sha256=facts_sha,
            backend_mode=backend_mode,
            model=args.model,
            thinking=args.thinking,
            priority=args.priority,
            repeats=args.repeats,
            workers=args.workers,
            planning_timeout=args.planning_timeout,
        )
        results = repeat_runs[0]
        if args.report:
            report = {
                "results": results,
                "counts": dict(collections.Counter(r["status"] for r in results)),
                "seconds": sum(r["seconds"] for r in results),
                "model_layer": v1_report["model_layer"],
            }
            args.report.write_text(json.dumps(report, indent=2))
        if args.v1_report:
            args.v1_report.write_text(json.dumps(v1_report, indent=2))
        if args.repeats_report and repeats_summary is not None:
            args.repeats_report.write_text(json.dumps(repeats_summary, indent=2))
        report_mod = _load_report_helper("benchmark_report")
        summary_out = {
            "counts": dict(collections.Counter(r["status"] for r in results)),
            "llm_failures": dict(collections.Counter((r.get("llm_failure") or "none") for r in results)),
            "seconds": sum(r["seconds"] for r in results),
            "model_layer": v1_report["model_layer"],
            "v1_summary": report_mod.summary(v1_report),
        }
        if repeats_summary is not None:
            summary_out["repeats_summary"] = {
                "repeats": repeats_summary["repeats"],
                "spread": repeats_summary["spread"],
                "observed_trials": repeats_summary["observed_trials"],
                "expected_trials": repeats_summary["expected_trials"],
            }
        print(json.dumps(summary_out, indent=2))


if __name__ == "__main__":
    main()
