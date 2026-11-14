"""Local-only decision corpus and offline planner replay. Never uploads cases."""

from __future__ import annotations

import argparse
import collections
import dataclasses
import gzip
import hashlib
import json
import re
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


def discover_identities(value, player=False):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            sensitive = private_key(key) or (player and normalized_key(key) in {"name", "id"})
            if sensitive:

                def strings(node):
                    if isinstance(node, (str, int)) and not isinstance(node, bool):
                        found.add(str(node))
                    elif isinstance(node, dict):
                        for v in node.values():
                            strings(v)
                    elif isinstance(node, list):
                        for v in node:
                            strings(v)

                strings(item)
            else:
                found.update(discover_identities(item, normalized_key(key) == "players"))
    elif isinstance(value, list):
        for item in value:
            found.update(discover_identities(item, player))
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
            if str(key).isdecimal() and str(key) in identities:
                raise UnsafeCase("identity in dynamic dictionary key")
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


def assert_record_schema(value, context):
    if not isinstance(value, dict):
        raise UnsafeCase("expected typed container")
    for key, item in value.items():
        if private_key(key) or (context == "player" and key in {"name", "id"}):
            continue
        valid = False
        if context == "card":
            if key in _CARD_TEXT:
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
    from arenamcp.decision_snapshot import _STATE_KEYS
    from arenamcp.decisions import PendingDecision, TargetSlot

    if set(snapshot) - {"version", "state"}:
        raise UnsafeCase("snapshot fields")
    state = snapshot["state"]
    if set(state) - set(_STATE_KEYS) - {"zones", "_zone_aliases"}:
        raise UnsafeCase("state fields")
    for key, value in state.items():
        if private_key(key):
            continue
        if key in {"hand", "battlefield", "graveyard", "exile", "command", "stack", "deck_cards"}:
            if not isinstance(value, list):
                raise UnsafeCase("card list")
            for card in value:
                assert_record_schema(card, "card")
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
                    "Phase_Beginning",
                    "Phase_Main1",
                    "Phase_Combat",
                    "Phase_Main2",
                    "Phase_Ending",
                    "",
                }:
                    continue
                if k == "step" and isinstance(v, str) and re.fullmatch(r"(?:Step_)?[A-Za-z]+", v):
                    continue
                raise UnsafeCase("turn field")
        elif key == "_zone_aliases":
            if not isinstance(value, list) or any(
                v not in {"battlefield", "hand", "my_hand", "graveyard", "exile", "command", "stack"}
                for v in value
            ):
                raise UnsafeCase("zone aliases")
        elif key == "format_name":
            if value not in {
                "Brawl",
                "Standard",
                "Historic",
                "Timeless",
                "Alchemy",
                "Explorer",
                "Draft",
                "Sealed",
                "Limited",
                "",
            }:
                raise UnsafeCase("format")
        elif key in {"legal_actions", "legal_actions_raw", "_bridge_actions"}:
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


class ModelRequired(BaseException):
    """Escapes production fallback handling rather than manufacturing a reply."""


class NoModel:
    def complete(self, *args, **kwargs):
        raise ModelRequired()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        raise ModelRequired()


def replay(case):
    from unittest.mock import patch

    from arenamcp.action_planner import ActionPlanner
    from arenamcp.decision_snapshot import game_state_from_snapshot

    decision = typed_decision(case["decision"])
    start = time.perf_counter()
    result = {"case_id": case["case_id"], "status": "withheld", "chosen": []}
    # No cached or remote facts silently change the benchmark input.
    with (
        patch("arenamcp.card_facts.client.facts_for", return_value={}),
        patch("arenamcp.line_search_moves.facts_for", return_value={}),
    ):
        try:
            planner = ActionPlanner(NoModel())
            chosen = planner.plan_decision_options(decision, game_state_from_snapshot(case["snapshot"]))
            result.update(
                status="answered" if decision.selection_is_valid(chosen) else "invalid", chosen=chosen
            )
        except ModelRequired:
            pass
        except Exception as error:
            result.update(status="error", error=type(error).__name__)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundles", type=Path)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--code-only", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.bundles:
        cases, manifest, counts = build_cases(args.bundles.glob("*.json.gz"))
        args.cases.mkdir(parents=True, exist_ok=True)
        (args.cases / "cases.json").write_text(json.dumps(cases))
        (args.cases / "private-source-manifest.json").write_text(json.dumps(manifest, indent=2))
        print(json.dumps(counts))
    if args.code_only:
        cases = json.loads((args.cases / "cases.json").read_text())
        results = [replay(case) for case in cases]
        report = {
            "results": results,
            "counts": dict(collections.Counter(r["status"] for r in results)),
            "seconds": sum(r["seconds"] for r in results),
            "model_layer": "not_run",
        }
        if args.report:
            args.report.write_text(json.dumps(report, indent=2))
        print(json.dumps({k: v for k, v in report.items() if k != "results"}))


if __name__ == "__main__":
    main()
