"""Shared preflight checks for plays, independent of planner transport."""

import logging
import re
from collections import Counter
from typing import Any

from arenamcp.combat_keywords import has_combat_keyword
from arenamcp.mana import has_autotap_solution
from arenamcp.rules_engine import RulesEngine, _normalize_mana_symbols

logger = logging.getLogger(__name__)


def find_source(state: dict, metadata: dict, name: str = "") -> dict:
    instance_id = metadata.get("instanceId")
    for zone in ("hand", "battlefield", "command", "graveyard", "exile", "stack"):
        for card in state.get(zone, []) or []:
            if instance_id:
                if card.get("instance_id") == instance_id:
                    return card
            elif name and str(card.get("name", "")).casefold() == name.casefold():
                return card
    return {}


def _local_seat(state: dict) -> int | None:
    return state.get("local_seat_id") or next(
        (player.get("seat_id") for player in state.get("players", []) if player.get("is_local")), None
    )


def _tutor_requirement(card: dict) -> re.Match | None:
    return re.search(
        r"search your library for (?:a |an )?(?:(green|white|blue|black|red) )?"
        r"creature card with (?:mana value|converted mana cost) x( or less)?\b",
        str(card.get("oracle_text") or "").lower(),
    )


def tutor_has_target(state: dict, card: dict, value: int) -> bool | None:
    """Whether a known remaining creature satisfies this simple X-tutor clause."""
    requirement = _tutor_requirement(card)
    if requirement is None or not state.get("deck_cards"):
        return None
    remaining = Counter(state["deck_cards"])
    local_seat = _local_seat(state)
    for zone in ("hand", "battlefield", "graveyard", "exile", "stack", "command"):
        for visible in state.get(zone, []) or []:
            if visible.get("owner_seat_id") == local_seat and visible.get("object_kind") != "ABILITY":
                remaining[visible.get("grp_id")] -= 1
    unknown = False
    try:
        from arenamcp.card_db import get_card_database

        database = get_card_database()
        for grp_id, count in remaining.items():
            if count <= 0:
                continue
            info = database.get_card_by_arena_id(grp_id)
            if info is None or not info.type_line:
                unknown = True
                continue
            if "creature" not in info.type_line.lower():
                continue
            color = requirement.group(1)
            if (
                color
                and {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G"}[color]
                not in info.colors
            ):
                continue
            if (info.cmc <= value) if requirement.group(2) else (info.cmc == value):
                return True
    except Exception as exc:
        logger.debug("Cannot establish X-tutor targets: %s", exc)
        return None
    return None if unknown else False


def _effective_cost(card: dict, metadata: dict) -> str:
    raw_cost = metadata.get("manaCost")
    if not isinstance(raw_cost, list) or not raw_cost:
        return _normalize_mana_symbols(str(card.get("mana_cost") or ""))
    symbols = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G", "colorless": "C", "x": "X"}
    cost = ""
    for part in raw_cost:
        colors = re.findall(r"[A-Za-z]+", str(part.get("color", "")).replace("ManaColor_", ""))
        count = int(part.get("count") or 0)
        if len(colors) != 1:
            return ""
        color = colors[0].lower()
        if color in ("generic", "any"):
            cost += f"{{{count}}}"
        elif color in symbols:
            cost += f"{{{symbols[color]}}}" * count
        else:
            return ""
    return cost


def useful_tutor_x(state: dict, card: dict, value: int) -> bool:
    """Keep useful zero-X plays without treating an unverified empty tutor as useful."""
    if _tutor_requirement(card) is None:
        return True
    target = tutor_has_target(state, card, value)
    return target is not False and (value > 0 or target is True)


def pending_x_source(state: dict) -> dict:
    context = state.get("decision_context") or {}
    payload = state.get("_bridge_request_payload") or context.get("raw") or {}
    requests = payload.get("castingTimeOptionReq") or []
    if not isinstance(requests, list):
        requests = [requests]
    for request in [payload, *requests]:
        if not isinstance(request, dict):
            continue
        numeric = request.get("numericInputReq") or request
        if not any(
            str(kind or "").endswith("ChooseX")
            for kind in (
                request.get("castingTimeOptionType"),
                numeric.get("numericInputType"),
                state.get("_bridge_numeric_input_type"),
            )
        ):
            continue
        source_id = request.get("affectedId") or numeric.get("sourceId") or context.get("source_id")
        return find_source(state, {"instanceId": source_id})
    return {}


def removal_lacks_opponent_target(card: dict, state: dict, *, activation: bool = False) -> bool:
    """Reject simple mandatory removal with no plausible opposing target."""
    oracle = str(card.get("oracle_text") or "").lower()
    if activation:
        abilities = [line.split(":", 1)[1] for line in oracle.splitlines() if ":" in line]
        if len(abilities) != 1:
            return False
        oracle = abilities[0]
    else:
        oracle = "\n".join(line for line in oracle.splitlines() if ":" not in line)
    if not re.search(r"\b(?:exile|destroy) target\b", oracle):
        return False
    if any(
        phrase in oracle
        for phrase in (
            "choose ",
            "you may",
            "up to",
            "any target",
            "target player",
            "return it",
            "return that",
        )
    ):
        return False
    requirements = RulesEngine._infer_target_requirements(oracle)
    if requirements["player_target"] or requirements["must_control"] == "you":
        return False
    if requirements["zones"] - {"battlefield"} or not (
        requirements["types"] or requirements["permanent_target"]
    ):
        return False
    local_seat = _local_seat(state)
    if local_seat is None:
        return False
    opposing = [
        permanent
        for permanent in state.get("battlefield", []) or []
        if (permanent.get("controller_seat_id") or permanent.get("owner_seat_id")) != local_seat
    ]
    if any(not permanent.get("type_line") for permanent in opposing):
        return False
    return not RulesEngine._match_battlefield_targets(opposing, local_seat, None, requirements)


def _animation_has_visible_payoff(state: dict, card: dict) -> bool:
    # Creature status may matter outside combat (sacrifices, untap tricks,
    # activation triggers, or responding to a targeted spell). Only payable
    # hand spells establish an immediate payoff when Arena supplies a menu.
    local = _local_seat(state)
    available = state.get("_bridge_actions")
    payable_ids = {
        action.get("instanceId")
        for action in available or []
        if has_autotap_solution(action)
        and str(action.get("actionType", "")).removeprefix("ActionType_") == "Cast"
    }

    def ongoing_text(other: dict) -> str:
        lines = []
        for line in str(other.get("oracle_text") or "").splitlines():
            # Animation/crew does not enter the battlefield. A spent ETB or
            # another creature's enter trigger is not a payoff for animating.
            condition = line.split(",", 1)[0]
            if (
                re.match(r"(?:when|whenever)\b", condition.strip(), re.I)
                and re.search(r"\benters?\b", condition, re.I)
                and not re.search(r"\b(?:crew\w*|activat\w*|becomes)\b", condition, re.I)
            ):
                continue
            lines.append(line)
        return "\n".join(lines)

    relevant = (
        [
            str(other.get("oracle_text") or "")
            for other in state.get("stack") or []
            if not other.get("targeting") or card.get("instance_id") in other["targeting"]
        ]
        + [
            "\n".join(
                line
                for line in ongoing_text(card).splitlines()
                if line.lower().startswith(("whenever", "at the beginning"))
            )
        ]
        + [
            ongoing_text(other)
            for other in state.get("battlefield") or []
            if other.get("instance_id") != card.get("instance_id")
            and (other.get("controller_seat_id") or other.get("owner_seat_id")) == local
        ]
        + [
            str(other.get("oracle_text") or "")
            for other in state.get("hand") or []
            if available is None or other.get("instance_id") in payable_ids
        ]
    )
    return any(
        re.search(
            r"\b(?:untap|haste)\b|\bsacrifice\b[^.]*\bcreature\b|\btarget\b[^.]*\b(?:noncreature|creature)\b"
            r"|\bwhenever\b[^.]*\b(?:activat\w*|becomes|crew\w*)\b"
            r"|number of creatures|for each (?:other )?creature|creatures you control (?:get|have)"
            r"|greatest power|total power|creatures? (?:you control )?with power",
            other.lower(),
        )
        for other in relevant
    )


def pointless_self_animation(state: dict, card: dict, metadata: dict) -> str:
    """Withhold simple temporary animation/crew with no visible use for its body.

    Ambiguous abilities and additional effects remain model decisions. This
    checks for wasted resources, not whether Arena legally allows activation.
    """
    text = re.sub(r"<[^>]*>", "", card.get("oracle_text") or "").lower()
    ability = str(metadata.get("ability_text") or "").lower()
    if not ability:
        abilities = list(
            dict.fromkeys(
                line.strip()
                for line in text.splitlines()
                if (":" in line and not re.search(r":\s*add\b", line))
                or re.match(r"crew\s+\d+\b", line.strip())
            )
        )
        if len(abilities) != 1:
            return ""
        ability = abilities[0]
    crew = re.fullmatch(r"crew\s+\d+(?:\s*\([^\n]*\))?\.?", ability) is not None
    effect = ability.split(":", 1)[-1].strip()
    name = re.escape(str(card.get("name") or "").lower())
    match = re.fullmatch(
        rf"(?:this (?:artifact|land|permanent)|cardname|{name}) becomes (?:a|an) "
        r"(\d+)/(\d+) (?:[\w-]+ )*creature until end of turn\.?",
        effect,
    )
    if not crew and not match:
        return ""
    if _animation_has_visible_payoff(state, card):
        return ""

    if "CardType_Creature" in (card.get("card_types") or []):
        if crew:
            return "Vehicle is already a creature, with no visible benefit to crewing again"
        power, toughness = card.get("power"), card.get("toughness")
        if match and isinstance(power, int) and isinstance(toughness, int):
            if power >= int(match[1]) and toughness >= int(match[2]):
                return "already a creature with at least the animation's power and toughness"
    taps_source = any(
        payment.get("instanceId") == card.get("instance_id")
        for payment in metadata.get("autoTapActions") or []
    )
    if card.get("is_tapped") or taps_source:
        return "temporary animation leaves this source tapped, with no visible attack, block, or other payoff"
    turn = state.get("turn") or {}
    turn_number = turn.get("turn_number")
    if (
        _local_seat(state) is not None
        and turn.get("active_player") == _local_seat(state)
        and isinstance(turn_number, int)
        and turn_number > 0
        and card.get("turn_entered_battlefield") == turn_number
        and not has_combat_keyword(card, "haste")
        and not any("haste" in str(grant).lower() for grant in card.get("granted_abilities") or [])
    ):
        return "entered this turn without visible haste; temporary animation cannot enable an attack and has no other visible payoff"
    return ""


def unsafe_play_reason(state: dict, card: dict, action_type: str, metadata: dict | None = None) -> str:
    """Return a reason to withhold a play, not a claim of full MTG legality."""
    action_type = action_type.removeprefix("ActionType_").lower()
    if action_type not in ("cast", "activate") or not card:
        return ""
    metadata = metadata or next(
        (
            entry
            for entry in state.get("_bridge_actions") or []
            if card.get("instance_id")
            and entry.get("instanceId") == card["instance_id"]
            and str(entry.get("actionType", "")).removeprefix("ActionType_").lower() == action_type
        ),
        {},
    )
    if removal_lacks_opponent_target(card, state, activation=action_type == "activate"):
        return "mandatory removal has no opposing target"
    if action_type == "activate":
        return pointless_self_animation(state, card, metadata)
    if action_type != "cast" or _tutor_requirement(card) is None:
        return ""
    type_line = str(card.get("type_line") or "").lower()
    if type_line and not any(card_type in type_line for card_type in ("instant", "sorcery")):
        return ""
    cost = _effective_cost(card, metadata)
    x_count = cost.upper().count("{X}")
    local_seat = _local_seat(state)
    if not x_count or local_seat is None:
        return ""
    pool = RulesEngine._get_mana_pool(state, local_seat)
    maximum = max(0, (pool["total"] - RulesEngine._parse_cmc(cost)) // x_count)
    target = tutor_has_target(state, card, maximum)
    if target is False or (maximum == 0 and target is not True):
        return f"X tutor has no confirmed creature to find at affordable X={maximum}"
    return ""


def filter_play_options(decision: Any, state: dict) -> Any:
    """Use the same safe option set for model selection and fallback."""
    from dataclasses import replace

    if decision.request_type == "CastingTimeOptions":
        source = pending_x_source(state)
        return replace(
            decision,
            options=tuple(
                option
                for option in decision.options
                if option.meta.get("numericValue") is None
                or useful_tutor_x(state, source, int(option.meta["numericValue"]))
            ),
        )
    if decision.request_type != "ActionsAvailable":
        return decision
    state = {**state, "_bridge_actions": [option.meta for option in decision.options if option.meta]}
    kept = []
    for option in decision.options:
        metadata = option.meta or {}
        source = find_source(state, metadata)
        reason = unsafe_play_reason(state, source, metadata.get("actionType", ""), metadata)
        if option.payable is not False and not reason:
            kept.append(option)
        elif reason:
            logger.info("Withholding %s: %s", option.label, reason)
    return replace(decision, options=tuple(kept))
