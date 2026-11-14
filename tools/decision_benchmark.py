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


# Frozen v1 observed structural vocabulary; new fields require explicit review.
_SCHEMA_KEYS = frozenset(
    (
        "Burden",
        "C",
        "Charge",
        "Colorless",
        "Energy",
        "Finality",
        "Flying",
        "G",
        "Green",
        "Lore",
        "Loyalty",
        "M1M1",
        "P1P1",
        "Plan",
        "Quest",
        "R",
        "Stun",
        "Time",
        "U",
        "W",
        "Wish",
        "_ability_grp_id",
        "_alternative",
        "_alternative_grp_id",
        "_autotap_lands",
        "_bridge_actions",
        "_bridge_allow_undo",
        "_bridge_can_cancel",
        "_bridge_can_pass",
        "_bridge_game_state_id",
        "_bridge_request_class",
        "_bridge_request_type",
        "_bridge_source",
        "_castable",
        "_mana_cost_str",
        "_return_credit_blocked",
        "_source_id",
        "_unaffordable",
        "_zone_aliases",
        "abilityGrpId",
        "ability_grp_ids",
        "ability_text",
        "actionType",
        "action_types",
        "active_player",
        "affectedId",
        "affectorId",
        "allowCancel",
        "allowFailToFind",
        "alternative",
        "alternativeCostText",
        "alternativeGrpId",
        "alternativeSourceZcid",
        "assumeCanBePaidFor",
        "attached_to_id",
        "attack_target_id",
        "attackerInstanceId",
        "autoTapActions",
        "autoTapSolution",
        "base_grp_id",
        "battlefield",
        "can_cancel",
        "can_pass",
        "candidate_ids",
        "card",
        "card_grp_id",
        "card_name",
        "card_types",
        "castingTimeOptionReq",
        "castingTimeOptionType",
        "childIndex",
        "choice",
        "choiceKind",
        "class_level",
        "cmc",
        "color",
        "color_id",
        "color_production",
        "colors",
        "command",
        "commander_casts",
        "commander_grp_ids",
        "commander_ids",
        "commander_return",
        "context",
        "context_raw",
        "controlled_turn",
        "controller_seat_id",
        "count",
        "counters",
        "ctoId",
        "damage",
        "damage_taken",
        "damaged_this_turn",
        "death_swing",
        "deciding_for_opponent",
        "decision_context",
        "decision_player",
        "decision_seat_id",
        "deck_cards",
        "designations",
        "door_token_seen",
        "dungeon_status",
        "entered_via_play_land",
        "event_id",
        "excludedOptions",
        "exile",
        "face_down",
        "face_down_reason",
        "facetId",
        "format_name",
        "freeMulliganCount",
        "gameStateId",
        "game_number",
        "graveyard",
        "groupSpecs",
        "groupType",
        "grpId",
        "grp_id",
        "hand",
        "hand_complete",
        "hand_count",
        "hand_loss",
        "hasAutoTap",
        "has_enduring_story",
        "haveTriggersBeenApplied",
        "highlight",
        "id",
        "id_type",
        "identity_known",
        "ids",
        "instanceId",
        "instanceIds",
        "instance_id",
        "isBatchable",
        "isManaAbilityWithSideEffect",
        "isRequired",
        "is_attacking",
        "is_blocked",
        "is_blocking",
        "is_copy",
        "is_local",
        "is_room",
        "is_tapped",
        "itemsSought",
        "itemsToSearch",
        "keywords",
        "label",
        "lands_played",
        "legalAction",
        "legalDamageRecipients",
        "legal_actions",
        "legal_actions_raw",
        "library_count",
        "library_count_source",
        "life_gained_this_game",
        "life_gained_this_turn",
        "life_total",
        "linkedSelections",
        "list_type",
        "local_graveyard",
        "local_seat_id",
        "london_mulligan",
        "lower",
        "lowerBound",
        "loyalty",
        "mana",
        "manaColor",
        "manaCost",
        "manaId",
        "manaPaymentConditions",
        "manaPaymentOption",
        "manaPaymentOptions",
        "manaPayments",
        "manaSelections",
        "manaSpentMatters",
        "mana_cost",
        "mana_pool",
        "match_id",
        "max",
        "maxActivations",
        "maxFind",
        "maxSel",
        "maxTargets",
        "maxValue",
        "max_select",
        "max_targets",
        "max_weight",
        "mechanics",
        "meta",
        "min",
        "minSel",
        "minTargets",
        "min_select",
        "min_targets",
        "min_weight",
        "modalOptions",
        "modalReq",
        "msgId",
        "mulliganCount",
        "mulligan_count",
        "name",
        "num_actions",
        "num_options",
        "numberValue",
        "numericInputReq",
        "numericInputType",
        "numericValue",
        "objectId",
        "object_kind",
        "opponent_controlled_by_you",
        "opponent_graveyard",
        "opponent_hand_count",
        "opponent_library_count",
        "opponent_seat_id",
        "optionIndex",
        "option_cards",
        "option_context",
        "option_id",
        "option_ids",
        "optionalActionMessage",
        "optionalActionTypes",
        "optional_action_types",
        "options",
        "oracle_text",
        "owner_seat_id",
        "parameterName",
        "parameters",
        "parent_instance_id",
        "payable",
        "pending_combat_steps",
        "pending_selection_objects",
        "phase",
        "playerIdToPrompt",
        "playerSystemSeatId",
        "player_seat",
        "players",
        "power",
        "prior_prompt",
        "priority_player",
        "prompt",
        "promptCardIds",
        "promptCards",
        "promptId",
        "promptText",
        "prompt_card_ids",
        "prompt_cards",
        "prompt_id",
        "prompt_source_id",
        "prompt_text",
        "raw",
        "recipientCards",
        "recipientIds",
        "recipient_ids",
        "recipient_names",
        "recipients",
        "related_faces",
        "repeatedSelectAllowed",
        "requestClass",
        "requestType",
        "request_id",
        "request_type",
        "seat_id",
        "selected",
        "selectedColor",
        "selectedDamageRecipient",
        "selectionCount",
        "selection_context",
        "selection_source_grp_id",
        "shouldStop",
        "slots",
        "sourceId",
        "source_card",
        "source_card_oracle_text",
        "source_id",
        "source_label",
        "source_oracle_text",
        "source_parent_instance_id",
        "spec_index",
        "specs",
        "srcInstanceId",
        "stack",
        "stage",
        "state",
        "static_list",
        "status",
        "step",
        "stepSize",
        "subZoneType",
        "sub_zone",
        "subtype_id",
        "subtypes",
        "summarized",
        "summoning_sickness",
        "super_types",
        "symbol",
        "systemSeatIds",
        "targetId",
        "targetIdx",
        "targetInstanceId",
        "targetSourceZoneId",
        "target_idx",
        "targeting",
        "targetingAbilityGrpId",
        "targetingPlayer",
        "targets",
        "team_id",
        "timeout_count",
        "title_id",
        "token_creator",
        "token_creator_id",
        "toughness",
        "turn",
        "turn_entered_battlefield",
        "turn_number",
        "type",
        "type_line",
        "unfiltered_ids",
        "uniqueAbilityId",
        "unknown",
        "upper",
        "upperBound",
        "validationType",
        "value",
        "valueCase",
        "version",
        "visibility",
        "weight",
        "you_controlled_by_opponent",
        "zone",
        "zoneType",
        "zone_id",
        "zones",
        "zonesToSearch",
    )
)


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
            if key not in _SCHEMA_KEYS and not str(key).isdecimal():
                raise UnsafeCase("unrecognized schema field")
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
