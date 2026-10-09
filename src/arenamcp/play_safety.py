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


def _animation_has_visible_payoff(state: dict, card: dict, metadata: dict) -> bool:
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

    # Becoming every creature type can increase a tribal mana engine even
    # when the animated land is tapped. Newly entered/tapped Hobbits cannot
    # cash in that benefit before the animation expires.
    current_creature = any(
        str(kind).removeprefix("CardType_") == "Creature" for kind in card.get("card_types") or []
    )
    if not current_creature and "with all creature types" in str(card.get("oracle_text") or "").lower():
        creature_subtypes = {
            str(subtype).lower()
            for permanent in state.get("battlefield") or []
            if any(
                str(kind).removeprefix("CardType_") == "Creature"
                for kind in permanent.get("card_types") or []
            )
            for subtype in permanent.get("subtypes") or []
        }
        turn_number = (state.get("turn") or {}).get("turn_number")
        payment_taps = {payment.get("instanceId") for payment in metadata.get("autoTapActions") or []}
        for producer in state.get("battlefield") or []:
            if (
                local is None
                or (producer.get("controller_seat_id") or producer.get("owner_seat_id")) != local
            ):
                continue
            if producer.get("is_tapped") or producer.get("instance_id") in payment_taps:
                continue
            sick = producer.get("summoning_sickness") or (
                isinstance(turn_number, int)
                and turn_number > 0
                and producer.get("turn_entered_battlefield") == turn_number
            )
            if sick and not has_combat_keyword(producer, "haste"):
                continue
            tribal_mana = re.search(
                r"\badd\b[^.\n]*\bfor each (?:other )?(\w+) you control",
                str(producer.get("oracle_text") or ""),
                re.I,
            )
            if tribal_mana and tribal_mana[1].lower() in creature_subtypes:
                return True

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
        r"(\d+)/(\d+) (?:[\w-]+ )*creature(?: with all creature types)? until end of turn\.?"
        r"(?:\s+it['’]s still (?:a|an) (?:land|artifact)\.?)?",
        effect,
    )
    if not crew and not match:
        return ""
    if _animation_has_visible_payoff(state, card, metadata):
        return ""

    if any(str(kind).removeprefix("CardType_") == "Creature" for kind in card.get("card_types") or []):
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
        and str(turn.get("phase") or "").removeprefix("Phase_") in {"Main2", "Ending"}
    ):
        return "temporary animation expires this turn, with no remaining combat or other visible payoff"
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


_FIXED_DAMAGE = re.compile(
    r"\bdeals (\d+) damage to (?:another )?target (creature or planeswalker|creature|planeswalker)\b"
)
_VARIABLE_DAMAGE = ("x damage", "where x", "for each", "instead", "divided", "any target", "choose ", "up to")


def fixed_damage_removal(card: dict) -> tuple[int, bool] | None:
    """(damage, may target planeswalkers) for plain fixed-damage removal; None otherwise."""
    oracle = "\n".join(
        line for line in str(card.get("oracle_text") or "").lower().splitlines() if ":" not in line
    )
    match = _FIXED_DAMAGE.search(oracle)
    if not match or any(phrase in oracle for phrase in _VARIABLE_DAMAGE):
        return None
    return int(match.group(1)), "planeswalker" in match.group(2)


def damage_would_kill(creature: dict, damage: int) -> bool | None:
    """True/False when the board proves it; None when damage already marked or combat could change it."""
    from arenamcp.combat_keywords import has_combat_keyword

    toughness = creature.get("toughness")
    if type(toughness) is not int:
        return None
    if has_combat_keyword(creature, "indestructible"):
        return False
    if toughness <= damage:
        return True
    if creature.get("damaged_this_turn") or creature.get("is_attacking") or creature.get("is_blocking"):
        return None
    return False


def damage_removal_kills_nothing(card: dict, state: dict) -> str:
    """Withhold fixed-damage removal when no opposing target would die.

    2026-10-05 Arena Direct: Wrath of the Bloodmane (4 damage) was cast at a
    5/5 Uldaros Theorix; it survived and its flying damage was lethal next turn.
    """
    parsed = fixed_damage_removal(card)
    type_line = str(card.get("type_line") or "").lower()
    local_seat = _local_seat(state)
    if not parsed or local_seat is None or not any(kind in type_line for kind in ("instant", "sorcery")):
        return ""
    damage, hits_walkers = parsed
    opposing = [
        permanent
        for permanent in state.get("battlefield", []) or []
        if (permanent.get("controller_seat_id") or permanent.get("owner_seat_id")) != local_seat
    ]
    if hits_walkers and any("planeswalker" in str(p.get("type_line") or "").lower() for p in opposing):
        return ""
    creatures = [p for p in opposing if "creature" in str(p.get("type_line") or "").lower()]
    if not creatures or any(damage_would_kill(creature, damage) is not False for creature in creatures):
        return ""
    toughness = sorted(creature["toughness"] for creature in creatures)
    return f"{damage} damage kills no opposing creature (toughness {toughness})"


POWER_ONLY_DEBUFF = re.compile(r"\bgets? [-\u2212]\d+/[-\u2212]0\b", re.IGNORECASE)
_COUNTER_MODE = re.compile(r"^\W*counter target\b[^.]*\bspell\b", re.IGNORECASE)
_POWER_ONLY_MODE = re.compile(
    r"^target creature(?: an opponent controls| you don't control)? gets? [-\u2212]\d+/[-\u2212]0"
    r" until end of turn\.?$",
    re.IGNORECASE,
)


def _live_menu(state: dict) -> list[dict]:
    """The current request's actions: the bridge's live menu, else the log's."""
    actions = state.get("_bridge_actions")
    if actions is None:
        actions = state.get("legal_actions_raw")
    return [action for action in actions or [] if isinstance(action, dict)]


def menu_proves_empty_stack(state: dict) -> bool:
    """The live request offers a land play, which is legal only in a main phase with an empty stack.

    Snapshots can trail the request. 2026-10-06 13:52:06 (match 3da54de9 G1
    T10): Theoretical Necromancer was cast and resolved in one log batch; the
    coach fired "stack_spell_opponent" on a board still showing it on the
    stack while Arena's turn-10 request already offered Play:Island. Icy
    Reception was cast "to counter" it, the counter mode was gone, and its
    -5/-0 was spent in our own main phase.
    """
    return any(
        str(action.get("actionType") or "").removeprefix("ActionType_").startswith("Play")
        for action in _live_menu(state)
    )


def _is_spell(entry: dict) -> bool:
    kind = str(entry.get("object_kind") or "").upper()
    return kind != "ABILITY" and "ability" not in str(entry.get("type_line") or "").lower()


def opponent_spell_on_stack(state: dict) -> bool:
    """An opposing spell (not an ability) is on the stack in the current state."""
    if menu_proves_empty_stack(state):
        return False
    local_seat = _local_seat(state)
    return any(
        (entry.get("controller_seat_id") or entry.get("owner_seat_id")) not in (None, local_seat)
        and _is_spell(entry)
        for entry in state.get("stack", []) or []
    )


def power_debuff_has_combat_use(state: dict) -> bool:
    """A -N/-0 this turn can matter: we are in combat and an opposing creature attacks or blocks.

    Unknown phase falls back to the combat flags alone. A land play on the
    live menu means a main phase, whatever stale flags the snapshot holds.
    """
    if menu_proves_empty_stack(state):
        return False
    phase = str((state.get("turn") or {}).get("phase") or "")
    if phase and "combat" not in phase.lower():
        return False
    local_seat = _local_seat(state)
    return any(
        (creature.get("is_attacking") or creature.get("is_blocking"))
        and (
            local_seat is None
            or (creature.get("controller_seat_id") or creature.get("owner_seat_id")) != local_seat
        )
        for creature in state.get("battlefield", []) or []
    )


def power_only_debuff_wasted(card: dict, state: dict) -> str:
    """Withhold a spell whose only effects are -N/-0 (or a counter with nothing to counter) outside combat.

    2026-10-05 Premier Draft: Icy Reception's "-5/-0 until end of turn" was
    cast in main phase 1 "to kill" Carnivorous Cultivator. Power-only
    shrinking never kills, and nothing attacked that turn. 2026-10-06: the
    counter mode needs an opposing spell on the stack NOW (see
    ``menu_proves_empty_stack``), not in a snapshot that trails the request.
    """
    type_line = str(card.get("type_line") or "").lower()
    text = re.sub(r"<[^>]*>", "", str(card.get("oracle_text") or ""))
    if not any(kind in type_line for kind in ("instant", "sorcery")) or not POWER_ONLY_DEBUFF.search(text):
        return ""
    effects = []
    for line in re.split(r"\n|•", text):
        line = line.strip(" \t-—")
        if line and line not in effects and not re.match(r"^choose (?:one|two|up to)", line, re.IGNORECASE):
            effects.append(line)
    counters = [line for line in effects if _COUNTER_MODE.search(line)]
    others = [line for line in effects if line not in counters]
    if not others or any(not _POWER_ONLY_MODE.match(line) for line in others):
        return ""  # another effect (draw, damage, a second clause) may be the point
    if counters and opponent_spell_on_stack(state):
        return ""
    if power_debuff_has_combat_use(state):
        return ""
    if counters and menu_proves_empty_stack(state):
        return (
            "the stack is empty (a land play is on offer), so there is nothing to counter, and -N/-0 "
            "outside combat never kills; hold it until a spell is cast or creatures attack or block"
        )
    return "-N/-0 only shrinks power for this turn's combat (it never kills); hold it until creatures attack or block"


def power_only_mode_label(label: str) -> bool:
    """A casting-time mode label ("Mode 2: Target creature gets -5/-0 until end of turn.") that only shrinks power."""
    text = re.sub(r"<[^>]*>", "", str(label or "")).strip()
    text = re.sub(r"^(?:mode \d+|modal)\s*:\s*", "", text, flags=re.IGNORECASE)
    return bool(_POWER_ONLY_MODE.match(text))


# --- temporary -X/-Y shrinks and discard-for-effect ---------------------------
#
# 2026-10-07 18:33 (bug_20261007_183358, Pick-Two FRA, our T16 at 3 life vs 34):
# with "Cast Proft, Sinister Mastermind [OK]" (threshold live, a 5/5 menace)
# on the menu, the model cast Void Extrapolator first, then — Proft's cast gone
# while our own spell sat on the stack — activated "{B}, Discard this card:
# Target creature gets -3/-1 until end of turn" "to kill" Hallway Heckler
# (a 2/3: -1/2, alive), and aimed it at the 6/4 Apex Witchstalker "to remove
# the crackback threat". The shrink wore off at our end step, before their
# attack; the castable 5/5 was thrown away for nothing.

_TEMP_SHRINK = re.compile(
    r"^(?:up to one |another )?target creature(?: an opponent controls| you don['’]t control)? gets? "
    r"[-−](?P<p>\d+)/[-−](?P<t>\d+) until end of turn\.?$",
    re.IGNORECASE,
)
_DISCARD_COST = re.compile(r"\bdiscard (?:this card|~|cardname)\b", re.IGNORECASE)
_SORCERY_SPEED = re.compile(r"\bactivate only as a sorcery\b", re.IGNORECASE)
_KILL_CLAIM = re.compile(r"\b(?:kill\w*|finish\w* off|destroy\w*|dies|die)\b", re.IGNORECASE)


def _rules_lines(text: Any) -> list[str]:
    """Rules lines with Arena's tags removed and its repeated formatting variants collapsed."""
    lines: list[str] = []
    for raw in re.sub(r"<[^>]*>", "", str(text or "")).splitlines():
        line = " ".join(raw.split())
        if line and line not in lines:
            lines.append(line)
    return lines


def _activated_abilities(card: dict) -> list[tuple[str, str]]:
    """(cost, effect) of each non-mana activated ability line of the card's rules text."""
    found = []
    for line in _rules_lines(card.get("oracle_text")):
        if ":" not in line or re.search(r":\s*add\b", line, re.IGNORECASE):
            continue
        cost, effect = line.split(":", 1)
        found.append((cost.strip(), effect.strip()))
    return found


def _offered_ability(card: dict, metadata: dict | None) -> tuple[str, str]:
    """(cost, effect) of the activation on offer: the menu's ability text, else the card's only one."""
    ability = str((metadata or {}).get("ability_text") or "").strip()
    if ability:
        cost, _, effect = ability.partition(":") if ":" in ability else ("", "", ability)
        return cost.strip(), effect.strip()
    abilities = _activated_abilities(card)
    return abilities[0] if len(abilities) == 1 else ("", "")


def _cast_effects(card: dict) -> list[str]:
    """An instant's or sorcery's effects, one per line or mode (costs and 'choose one' headers aside)."""
    type_line = str(card.get("type_line") or "").lower()
    if not any(kind in type_line for kind in ("instant", "sorcery")):
        return []
    effects: list[str] = []
    for line in _rules_lines(card.get("oracle_text")):
        if ":" in line:
            continue
        for part in line.split("•"):
            part = part.strip(" \t-—")
            if part and not re.match(r"^choose (?:one|two|up to)", part, re.IGNORECASE):
                effects.append(part)
    return effects


def temporary_shrink(effects: list[str]) -> tuple[int, int] | None:
    """(power loss, toughness loss) when the whole effect is one 'target creature gets -X/-Y until end of turn'."""
    if len(effects) != 1:
        return None
    match = _TEMP_SHRINK.match(effects[0].strip())
    return (int(match.group("p")), int(match.group("t"))) if match else None


def shrink_would_kill(creature: dict, toughness_loss: int) -> bool | None:
    """True/False when the board proves it (marked damage counted); None when combat could still change it."""
    toughness = creature.get("toughness")
    if type(toughness) is not int:
        return None
    damage = creature.get("damage") if type(creature.get("damage")) is int else 0
    if toughness - damage <= toughness_loss:
        return True
    if (
        (creature.get("damaged_this_turn") and not damage)
        or creature.get("is_attacking")
        or creature.get("is_blocking")
    ):
        return None
    return False


def _opposing_creatures(state: dict) -> list[dict]:
    local_seat = _local_seat(state)
    return [
        permanent
        for permanent in state.get("battlefield", []) or []
        if local_seat is not None
        and (permanent.get("controller_seat_id") or permanent.get("owner_seat_id")) != local_seat
        and "creature" in str(permanent.get("type_line") or "").lower()
    ]


def kill_verdict(reach: tuple[str, int], creature: dict) -> bool | None:
    """Whether a ("shrink", Y) / ("damage", N) effect kills this creature; None when unproven."""
    kind, amount = reach
    if kind == "shrink":
        return shrink_would_kill(creature, amount)
    if kind == "damage":
        return damage_would_kill(creature, amount)
    return None


def play_kill_reach(card: dict, *, activation: bool, metadata: dict | None = None) -> tuple[str, int] | None:
    """("shrink", toughness loss) or ("damage", N) for a play whose whole effect is that one clause."""
    effects = [_offered_ability(card, metadata)[1]] if activation else _cast_effects(card)
    effects = [effect for effect in effects if effect]
    shrink = temporary_shrink(effects)
    if shrink is not None:
        return "shrink", shrink[1]
    if len(effects) == 1:
        parsed = fixed_damage_removal({"oracle_text": effects[0]})
        if parsed:
            return "damage", parsed[0]
    return None


def _combat_in_progress(state: dict) -> bool:
    """Attackers or blockers are declared in the current combat, on either side."""
    if menu_proves_empty_stack(state):
        return False
    phase = str((state.get("turn") or {}).get("phase") or "")
    if phase and "combat" not in phase.lower():
        return False
    return any(
        creature.get("is_attacking") or creature.get("is_blocking")
        for creature in state.get("battlefield", []) or []
    )


def shrink_has_use_without_kill(state: dict) -> bool:
    """A survivor's -X/-Y still matters: their turn (their attackers and blockers), or our combat under way.

    On our own turn outside combat it expires at our end step, before their attack.
    """
    local_seat = _local_seat(state)
    turn = state.get("turn") or {}
    if local_seat is None or turn.get("active_player") != local_seat:
        return True
    return _combat_in_progress(state)


def _sorcery_speed_shrink_before_our_attack(state: dict, sorcery_speed: bool) -> bool:
    """A sorcery-speed shrink in our first main phase may be for this turn's attack: leave it to the model."""
    if not sorcery_speed:
        return False
    local_seat = _local_seat(state)
    turn = state.get("turn") or {}
    phase = str(turn.get("phase") or "").lower()
    if local_seat is None or turn.get("active_player") != local_seat or "main1" not in phase:
        return False
    turn_number = turn.get("turn_number")
    attackers = [
        creature
        for creature in state.get("battlefield", []) or []
        if (creature.get("controller_seat_id") or creature.get("owner_seat_id")) == local_seat
        and "creature" in str(creature.get("type_line") or "").lower()
        and not creature.get("is_tapped")
        and (creature.get("turn_entered_battlefield") != turn_number or has_combat_keyword(creature, "haste"))
    ]
    blockers = [creature for creature in _opposing_creatures(state) if not creature.get("is_tapped")]
    return bool(attackers and blockers)


def temporary_shrink_wasted(
    card: dict, state: dict, *, activation: bool = False, metadata: dict | None = None
) -> str:
    """Withhold a lone 'target creature gets -X/-Y until end of turn' that kills nothing when no combat can use it.

    On our own turn outside combat the effect wears off at our end step,
    before the opponent's attack (bug_20261007_183358, above). On their turn
    it shrinks an attacker or blocker, and inside our combat a blocker, so
    those stay with the model. A -N/-0 never kills: the cast path has
    :func:`power_only_debuff_wasted`; an activation needs the same combat.
    """
    cost, effect = _offered_ability(card, metadata) if activation else ("", "")
    effects = [effect] if activation else _cast_effects(card)
    shrink = temporary_shrink([e for e in effects if e])
    if shrink is None:
        return ""
    power_loss, toughness_loss = shrink
    if toughness_loss == 0:
        if not activation or power_debuff_has_combat_use(state):
            return ""
        return "-N/-0 only shrinks power for this turn's combat (it never kills); hold it until creatures attack or block"
    opposing = _opposing_creatures(state)
    if any(shrink_would_kill(creature, toughness_loss) is not False for creature in opposing):
        return ""
    if shrink_has_use_without_kill(state):
        return ""
    sorcery_speed = (
        bool(_SORCERY_SPEED.search(effect))
        if activation
        else ("sorcery" in str(card.get("type_line") or "").lower())
    )
    if _sorcery_speed_shrink_before_our_attack(state, sorcery_speed):
        return ""
    survivors = sorted(
        creature["toughness"] for creature in opposing if type(creature.get("toughness")) is int
    )
    return (
        f"-{power_loss}/-{toughness_loss} until end of turn kills no opposing creature (toughness {survivors}) "
        "and wears off at our end step, before their turn; no combat this turn can use it"
    )


def _castable_now(state: dict, card: dict) -> bool:
    """Arena offers the card's cast now, or would once our own spell on the stack resolves."""
    instance_id = card.get("instance_id")
    for action in _live_menu(state):
        if (
            instance_id
            and action.get("instanceId") == instance_id
            and str(action.get("actionType") or "").removeprefix("ActionType_") == "Cast"
            and has_autotap_solution(action)
        ):
            return True
    local_seat = _local_seat(state)
    turn = state.get("turn") or {}
    if local_seat is None or turn.get("active_player") != local_seat:
        return False
    if "main" not in str(turn.get("phase") or "").lower():
        return False
    stack = state.get("stack") or []
    if not stack or any(
        (entry.get("controller_seat_id") or entry.get("owner_seat_id")) != local_seat for entry in stack
    ):
        return False
    cost = _normalize_mana_symbols(str(card.get("mana_cost") or ""))
    if not cost:
        return False
    return RulesEngine._can_afford(cost, RulesEngine._get_mana_pool(state, local_seat))


def _all_in(state: dict) -> bool:
    """board_assessment's verdict that no defensive line survives their next attack."""
    try:
        from arenamcp.board_assessment import assess

        assessment = assess(state)
    except Exception as exc:
        logger.debug("No board assessment for the discard guard: %s", exc)
        return False
    return bool(assessment is not None and assessment.all_in)


def castable_discard_for_minor_effect(card: dict, state: dict, metadata: dict | None = None) -> str:
    """Withhold discarding a card we can cast for a lone shrink or burn effect that kills nothing.

    Dead to their next attack whatever we do (board_assessment ``all_in``),
    the discard is left to the model.
    """
    cost, effect = _offered_ability(card, metadata)
    if not cost or not _DISCARD_COST.search(cost):
        return ""
    reach = play_kill_reach(card, activation=True, metadata=metadata)
    if reach is None:
        return ""
    if any(kill_verdict(reach, creature) is not False for creature in _opposing_creatures(state)):
        return ""
    if not _castable_now(state, card) or _all_in(state):
        return ""
    name = str(card.get("name") or "this card")
    return (
        f"{name} is castable now (or once our spell on the stack resolves); discarding it for "
        f"'{effect}' that kills nothing throws the card away"
    )


def claimed_kills(reasoning: str, creatures: list[dict]) -> list[dict]:
    """The creatures a model's reasoning says die: "kills Hallway Heckler", "the Heckler dies"."""
    text = " ".join(str(reasoning or "").lower().split())
    if not text or not _KILL_CLAIM.search(text):
        return []
    found = []
    for creature in creatures:
        full = str(creature.get("name") or "").lower().strip()
        short = full.split(",")[0].strip()
        last = short.split()[-1] if short else ""  # "the Heckler dies", "finish off the Witchstalker"
        names = [n for n in dict.fromkeys((full, short, last if len(last) >= 5 else "")) if len(n) >= 4]
        for name in names:
            pattern = (
                rf"\b(?:kill\w*|finish\w* off|destroy\w*)\s+(?:(?:the|their|that|a|an|its|his|her)\s+)?"
                rf"(?:[\w'’/+-]+\s+){{0,3}}{re.escape(name)}"
                rf"|{re.escape(name)}\b[^.;]{{0,40}}\b(?:dies|die)\b"
            )
            if re.search(pattern, text):
                found.append(creature)
                break
    return found


def shrink_note(state: dict, card: dict, *, activation: bool, metadata: dict | None = None) -> str:
    """A prompt fact for a lone -X/-Y play: whom it kills now, or that it wears off before their turn."""
    reach = play_kill_reach(card, activation=activation, metadata=metadata)
    if reach is None or reach[0] != "shrink":
        return ""
    opposing = _opposing_creatures(state)
    dying = [
        str(c.get("name") or c.get("instance_id")) for c in opposing if shrink_would_kill(c, reach[1]) is True
    ]
    if dying:
        return f"  [-X/-{reach[1]} until end of turn kills now: {', '.join(dying)}]"
    survivors = sorted(c["toughness"] for c in opposing if type(c.get("toughness")) is int)
    when = (
        "this turn's combat only"
        if shrink_has_use_without_kill(state)
        else "wears off at end of turn, before their attack"
    )
    return f"  [-X/-{reach[1]} until end of turn kills nothing now (toughness {survivors}); {when}]"


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
    if action_type == "cast":
        reason = (
            damage_removal_kills_nothing(card, state)
            or power_only_debuff_wasted(card, state)
            or temporary_shrink_wasted(card, state)
            or counter_tax_payable_wasted(card, state)
        )
        if reason:
            return reason
    if action_type == "activate":
        return (
            temporary_shrink_wasted(card, state, activation=True, metadata=metadata)
            or castable_discard_for_minor_effect(card, state, metadata)
            or pointless_self_animation(state, card, metadata)
        )
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
        options = tuple(
            option
            for option in decision.options
            if option.meta.get("numericValue") is None
            or useful_tutor_x(state, source, int(option.meta["numericValue"]))
        )
        # A -N/-0 mode with no attacker or blocker to shrink wastes the card;
        # with nothing left, the planner declines and the cast is cancelled.
        if decision.can_cancel and not power_debuff_has_combat_use(state):
            wasted = [
                option
                for option in options
                if option.meta.get("choiceKind") == "modal" and power_only_mode_label(option.label)
            ]
            for option in wasted:
                logger.info("Withholding %s: -N/-0 outside combat never kills", option.label)
            options = tuple(option for option in options if option not in wasted)
        if decision.can_cancel:
            # "Counter ... unless its controller pays {3}" with the tax payable is no counter.
            payable = [
                (option, why)
                for option in options
                if option.meta.get("choiceKind") == "modal"
                and (why := counter_tax_payable(_mode_text(option.label), state))
            ]
            for option, why in payable:
                logger.info("Withholding %s: %s", option.label, why)
            withheld = {id(option) for option, _why in payable}
            options = tuple(option for option in options if id(option) not in withheld)
        return replace(decision, options=options)
    if decision.request_type != "ActionsAvailable":
        return decision
    state = {**state, "_bridge_actions": [option.meta for option in decision.options if option.meta]}
    kept = []
    for option in decision.options:
        metadata = option.meta or {}
        source = find_source(state, metadata)
        reason = unsafe_play_reason(state, source, metadata.get("actionType", ""), metadata)
        if (
            not reason
            and str(metadata.get("actionType", "")).removeprefix("ActionType_").lower() == "activate"
        ):
            reason = tap_forfeits_free_attack(option.label, source, state) or loyalty_minus_exposes_walker(
                option.label, source, state
            )
        if option.payable is not False and not reason:
            kept.append(option)
        elif reason:
            logger.info("Withholding %s: %s", option.label, reason)
    return replace(decision, options=tuple(kept))


_TAP_COST = re.compile(r"^\s*\{o?T\}")
_NO_PRECOMBAT_WITHHOLD = re.compile(
    r"\badd\b[^.]*(?:\bmana\b|\{[wubrgc]\})|\bdamage\b|\bdestroy\b|\bexile\b|\b(?:gets?|gains?) \+|\bfight\b|\bsacrifice\b"
    r"|\bcan't block\b|\btap target\b|\bcounter target\b|\breturn target\b",
    re.IGNORECASE,
)
# Below this many points of free damage the loot/scry may be worth more.
FREE_ATTACK_MIN_DAMAGE = 2


def tap_forfeits_free_attack(label: str, source: dict, state: dict) -> str:
    """Withhold a {T} ability of a creature whose free attack is worth more this turn.

    2026-10-08 game 2 (match 3288cb65): Arni, Humble Scribe looted before
    combat on T8, T10, T12, T14, T16 and T18 and never attacked; on T8 the
    opposing board was empty and the loot cost 3 free damage. Fires only in
    our own precombat main phase, for a tap ability whose text is a loot,
    scry, surveil or similar value effect (never mana, damage, removal,
    pumps or taps), when :func:`combat_strategy.free_attackers` counts the
    creature in a free attack worth at least FREE_ATTACK_MIN_DAMAGE more
    than the same attack without it. After combat the ability is offered
    again (Arni untaps when another creature enters).
    """
    if not source or "creature" not in str(source.get("type_line") or "").lower():
        return ""
    turn = state.get("turn") or {}
    local = _local_seat(state)
    if local is None or turn.get("active_player") != local:
        return ""
    phase = str(turn.get("phase") or "").lower()
    if "main1" not in phase and "main_1" not in phase and phase not in ("main",):
        return ""
    if state.get("decision_context", {}).get("type") == "declare_attackers":
        return ""
    detail = label.split("[", 1)[1] if "[" in label else ""
    if not _TAP_COST.search(detail):
        return ""
    effect = detail.split(":", 1)[1] if ":" in detail else detail
    if _NO_PRECOMBAT_WITHHOLD.search(effect):
        return ""
    from arenamcp.combat_strategy import free_attackers

    try:
        with_creature = free_attackers(state)
    except Exception:  # the rule never blocks an activation on its own failure
        return ""
    identity = source.get("instance_id")
    if with_creature is None or identity not in with_creature.attacker_ids:
        return ""
    others = [i for i in with_creature.attacker_ids if i != identity]
    without = free_attackers(state, candidate_ids=others) if others else None
    gain = with_creature.damage - (without.damage if without else 0)
    if gain < FREE_ATTACK_MIN_DAMAGE:
        return ""
    logger.info(
        "Free attack: %s attacks for %d unopposed this turn — holding its tap ability until after combat",
        source.get("name"),
        gain,
    )
    return (
        f"Free attack: {source.get('name')} attacks for {gain} through their best blocks this turn "
        f"({with_creature.explanation}); tapping it now forfeits that damage"
    )


# --- counter-unless-pay and the planeswalker loyalty floor ----------------------
#
# 2026-10-08 game 1 (match 07c043d8), from the post-match review:
#
# * 17:10:23, their T12: Icy Reception was cast to "counter the resolving
#   Fblthp (opponent has only ~2 open mana, can't pay the {3})" with four of
#   their lands untapped. They paid; the card and two mana went for nothing.
# * 17:08:39, our T9: Jace went from 5 to 2 loyalty for a card into Tetsuko
#   Umezawa's board — Tetsuko, Geist and Traxos, three power, every one of
#   them unblockable — and was attacked down to nothing.

_COUNTER_UNLESS = re.compile(
    r"\bcounter target (?P<what>[^.•\n]*?\bspell\b[^.•\n]*?) unless (?:its controller|that player|they) pays?"
    r" \{o?(?P<n>\d+)\}",
    re.IGNORECASE,
)
_LOYALTY_MINUS = re.compile(r"\[(?:from [^\]:]+: )?[-\u2212](?P<n>\d+): ")


def _mode_text(label: str) -> str:
    """A casting-time mode label without its "Mode 2:" prefix and Arena's tags."""
    text = re.sub(r"<[^>]*>", "", str(label or "")).strip()
    return re.sub(r"^(?:mode \d+|modal)\s*:\s*", "", text, flags=re.IGNORECASE)


def _opponent_seat(state: dict) -> int | None:
    local = _local_seat(state)
    seat = state.get("opponent_seat_id")
    if seat is None:
        seat = next(
            (p.get("seat_id") for p in state.get("players") or [] if p.get("seat_id") not in (None, local)),
            None,
        )
    return seat if seat != local else None


def opponent_open_mana(state: dict) -> int | None:
    """Mana the opponent can pay right now: their untapped lands, rocks and mana creatures that
    can tap (``opponent_tricks._mana_sources``) plus their floating pool, restricted mana
    ("spend only to cast ...") left out. None when the snapshot shows no land of theirs at all,
    i.e. their board is unknown rather than empty."""
    opponent = _opponent_seat(state)
    if opponent is None:
        return None
    theirs = [
        card
        for card in state.get("battlefield") or []
        if isinstance(card, dict) and (card.get("controller_seat_id") or card.get("owner_seat_id")) == opponent
    ]
    if not any(
        "land" in str(card.get("type_line") or "").lower()
        or any("land" in str(kind).lower() for kind in card.get("card_types") or [])
        for card in theirs
    ):
        return None
    from arenamcp.opponent_tricks import _mana_sources

    return sum(1 for source in _mana_sources(state, opponent) if not getattr(source, "restricted", False))


def counter_tax_payable(text: str, state: dict) -> str:
    """Why a 'counter target ... spell unless its controller pays {N}' in ``text`` counters nothing:
    the opposing spell's controller has N or more mana open. '' when the clause is absent, no
    opposing spell is on the stack, their open mana is unknown, or they are short."""
    taxes = [int(match.group("n")) for match in _COUNTER_UNLESS.finditer(str(text or ""))]
    if not taxes or not opponent_spell_on_stack(state):
        return ""
    open_mana = opponent_open_mana(state)
    if open_mana is None:
        return ""
    tax = max(taxes)
    if open_mana < tax:
        return ""
    return (
        f"'counter unless its controller pays {{{tax}}}' counters nothing: the opponent has "
        f"{open_mana} mana open and pays"
    )


def counter_tax_payable_wasted(card: dict, state: dict) -> str:
    """Withhold casting a counter-unless-pay spell when the opponent can pay the tax and no other
    mode of the card does anything now (a -N/-0 mode outside combat: ``power_only_debuff_wasted``).
    A mode that may still be the point (draw, damage, a -X/-Y) leaves the cast with the model."""
    effects = _cast_effects(card)
    counters = [effect for effect in effects if _COUNTER_UNLESS.search(effect)]
    if not counters:
        return ""
    why = counter_tax_payable("\n".join(counters), state)
    if not why:
        return ""
    others = [effect for effect in effects if effect not in counters]
    for effect in others:
        if _POWER_ONLY_MODE.match(effect) and not power_debuff_has_combat_use(state):
            continue
        return ""
    return why + ("; its -N/-0 mode outside combat never kills" if others else "")


def evasive_power_next_turn(state: dict) -> tuple[int, list[str]]:
    """(power, names) of their creatures that can attack next turn and that our board can't block:
    marked "can't be blocked" (``annotate_cant_be_blocked``: Tetsuko Umezawa's grant, auras, own
    text), flyers while we have no flying or reach blocker, or everything while we have no creature
    that can block. Our creatures all count as blockers (none has attacked yet)."""
    from copy import deepcopy

    from arenamcp.combat_keywords import annotate_cant_be_blocked

    local = _local_seat(state)
    if local is None:
        return 0, []
    battlefield = [deepcopy(card) for card in state.get("battlefield") or [] if isinstance(card, dict)]
    annotate_cant_be_blocked(battlefield)  # marks the copies, never the live snapshot
    creatures = [c for c in battlefield if "creature" in str(c.get("type_line") or "").lower()]

    def controller(card: dict) -> Any:
        return card.get("controller_seat_id") or card.get("owner_seat_id")

    def rules(card: dict) -> str:
        return re.sub(r"<[^>]*>", "", str(card.get("oracle_text") or "")).lower()

    blockers = [c for c in creatures if controller(c) == local and not re.search(r"can(?:'|no)t block\b", rules(c))]
    air = any(has_combat_keyword(c, "flying") or has_combat_keyword(c, "reach") for c in blockers)
    total, names = 0, []
    for card in creatures:
        if controller(card) == local:
            continue
        power = card.get("power")
        if isinstance(power, bool) or not isinstance(power, int) or power <= 0:
            continue
        if has_combat_keyword(card, "defender") or re.search(r"(?m)^(?:this creature )?can't attack\b", rules(card)):
            continue
        evasive = bool(card.get("cant_be_blocked")) or not blockers or (has_combat_keyword(card, "flying") and not air)
        if evasive:
            total += power
            names.append(str(card.get("name") or "a creature"))
    return total, names


def loyalty_minus_exposes_walker(label: str, source: dict, state: dict) -> str:
    """Withhold a loyalty-minus ability that leaves the planeswalker at or below the evasive power
    the opponent can attack it with next turn (``evasive_power_next_turn``).

    Our own turn only. Not when the walker is moot anyway: we have lethal on
    board, or no defensive line survives their next attack (``all_in``,
    ``dead_in == 1`` from the board assessment — the ability may be what
    saves us, and that is the model's call). The search does not model
    loyalty abilities, so "the minus wins the game" can only mean lethal now.
    """
    if not source or "planeswalker" not in str(source.get("type_line") or "").lower():
        return ""
    match = _LOYALTY_MINUS.search(str(label or ""))
    if not match:
        return ""
    cost = int(match.group("n"))
    from arenamcp.combat_strategy import loyalty

    current = loyalty(source)
    if current is None:
        return ""
    turn = state.get("turn") or {}
    local = _local_seat(state)
    if local is None or turn.get("active_player") != local:
        return ""
    threat, names = evasive_power_next_turn(state)
    after = current - cost
    if threat <= 0 or after > threat:
        return ""
    try:
        from arenamcp.board_assessment import assess

        assessment = assess(state)
    except Exception as exc:  # the rule never blocks an activation on its own failure
        logger.debug("No board assessment for the loyalty floor: %s", exc)
        assessment = None
    if assessment is not None and (assessment.lethal_now or assessment.all_in or assessment.dead_in == 1):
        return ""
    name = str(source.get("name") or "the planeswalker")
    reason = (
        f"{name} at {current} loyalty: -{cost} leaves {after}, within the {threat} evasive power "
        f"({', '.join(names)}) they can attack it with next turn"
    )
    logger.info("Loyalty floor: %s", reason)
    return reason
