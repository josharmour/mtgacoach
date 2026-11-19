"""Public protection and draw-trigger sequencing facts for decision rechecks (#308).

These are reasons to compare lines, never a forced removal target. Unknown
opponent cards and unobserved once-per-turn uses are not invented.
"""

import re
from dataclasses import dataclass, field

from arenamcp.mana import get_local_seat_id
from arenamcp.play_safety import (
    damage_effect,
    find_source,
    record_for_card,
    record_is_complete,
)

_DRAW_ENTRY = re.compile(
    r"whenever one or more other creatures you control with power (\d+) or less enter, draw a card\.", re.I
)
_ONCE = re.compile(r"this ability triggers only once (?:each|per) turn", re.I)
_CREATURE_DRAW_TRIGGERS = frozenset(
    {
        "OTHER_CREATURE_ETB",
        "ANOTHER_CREATURE_ETB",
        "OTHER_CREATURE_ENTERS",
        "CREATURE_ETB",
        "CREATURE_ENTERS",
        "ENTERS_THE_BATTLEFIELD",
    }
)
_OPP_DRAW_TRIGGERS = frozenset(
    {
        "OPPONENT_DRAWS",
        "OPPONENT_DRAW",
        "WHEN_OPPONENT_DRAWS",
        "WHENEVER_OPPONENT_DRAWS",
        "ON_OPPONENT_DRAW",
    }
)


def _text(card):
    return re.sub(r"<[^>]*>", "", str(card.get("oracle_text") or "")).replace("’", "'")


_MAX_POWER_COND_RE = re.compile(
    r"(?:power\s*(?:<=|≤)\s*(\d+)|power\s+(\d+)\s*or less|max_power\s*[:=]\s*(\d+))",
    re.I,
)
_LOWER_BOUND_OR_SELF_RE = re.compile(
    r"(?:power\s*(?:>=|>)\s*\d+|power\s+\d+\s*or (?:more|greater)|\bself\b)",
    re.I,
)
_OPP_DRAW_FACE_TARGETS = frozenset(
    {
        "",
        "any",
        "any_target",
        "target_any",
        "opponent",
        "each_opponent",
        "opponents",
        "player",
        "target_opponent",
        "target_player",
        "target_creature_or_player",
        "creature_or_player",
    }
)


def _draw_entry_spec(card: dict, state: dict | None = None) -> tuple[int | None, bool]:
    """Return ``(max_power, once_per_turn)`` for small-creature ETB draw engines."""
    from arenamcp.ability_synthesizer import Draw

    rec = record_for_card(card, state)
    if record_is_complete(rec):
        creature_draws = [
            e
            for e in rec.effects
            if isinstance(e.effect, Draw)
            and str(e.trigger or "").strip().upper().replace(" ", "_") in _CREATURE_DRAW_TRIGGERS
        ]
        if not creature_draws:
            return None, False
        for entry in creature_draws:
            trig = str(entry.trigger or "").strip().upper().replace(" ", "_")
            cond_blob = " ".join(str(c) for c in entry.conditions or ())
            if _LOWER_BOUND_OR_SELF_RE.search(cond_blob):
                continue
            power_m = _MAX_POWER_COND_RE.search(cond_blob)
            if not power_m:
                continue
            raw_power = power_m.group(1) or power_m.group(2) or power_m.group(3)
            max_power = int(raw_power) if raw_power else None
            if max_power is not None and ("other" in trig.lower() or "other" in cond_blob.lower()):
                once = any("once" in str(c).lower() for c in entry.conditions or ())
                return max_power, once
        return None, False
    m = _DRAW_ENTRY.search(_text(card))
    return (int(m.group(1)), bool(_ONCE.search(_text(card)))) if m else (None, False)


def _draw_entry(card: dict, state: dict | None = None):
    """Compatibility wrapper returning ``(match_str, max_power)`` or regex Match for draw engines."""
    rec = record_for_card(card, state)
    max_power, _once = _draw_entry_spec(card, state)
    if max_power is None:
        return None
    if record_is_complete(rec):
        return (str(max_power), str(max_power))
    match = _DRAW_ENTRY.search(_text(card))
    if match is not None:
        return match
    return (str(max_power), str(max_power))


def _discard_indestructible_protection(card: dict, state: dict | None = None) -> bool:
    """True when card has an activated discard-to-gain-indestructible shield."""
    rec = record_for_card(card, state)
    if record_is_complete(rec):
        if not any(
            getattr(mode.cost, "discard", 0) >= 1
            and str(getattr(mode, "name", "") or "").strip().casefold() != "cast"
            for mode in rec.modes
        ):
            return False
        for entry in rec.effects:
            eff = entry.effect
            blob = " ".join(
                [
                    str(getattr(eff, "target", "") or ""),
                    *(str(c) for c in entry.conditions or ()),
                ]
            ).lower()
            if "indestructible" in blob:
                return True
        return False
    name = str(card.get("name") or "")
    return bool(
        re.search(
            r"(?:^|\n)discard a card:\s*(?:this creature|"
            + re.escape(name)
            + r") gains indestructible until end of turn\.(?:\s*tap it\.)?",
            _text(card),
            re.I,
        )
    )


def _draw_punisher_spec(card: dict, state: dict | None = None) -> tuple[bool, bool]:
    """Return ``(is_opp_draw_1dmg_punisher, also_amasses_orcs_1)``."""
    from arenamcp.ability_synthesizer import CreateToken, Damage, PutCounters

    rec = record_for_card(card, state)
    if record_is_complete(rec):
        opp_entries = [
            e
            for e in rec.effects
            if str(e.trigger or "").strip().upper().replace(" ", "_") in _OPP_DRAW_TRIGGERS
        ]
        dmg_entries = [
            e
            for e in opp_entries
            if isinstance(e.effect, Damage)
            and e.effect.amount == 1
            and str(e.effect.target or "").strip().lower().replace("-", "_").replace(" ", "_")
            in _OPP_DRAW_FACE_TARGETS
        ]
        if not dmg_entries:
            return False, False
        has_amass = False
        for entry in opp_entries:
            eff = entry.effect
            if (
                isinstance(eff, CreateToken)
                and any(
                    tok in " ".join([eff.spec.name, *eff.spec.subtypes]).lower() for tok in ("orc", "army")
                )
            ) or (
                isinstance(eff, PutCounters)
                and any(
                    tok in " ".join(str(c) for c in entry.conditions or ()).lower() for tok in ("orc", "army")
                )
            ):
                has_amass = True
        return True, has_amass
    text = _text(card)
    matched = bool(
        re.search(r"whenever an opponent draws a card", text, re.I)
        and re.search(r"deals 1 damage to any target", text, re.I)
    )
    return matched, (matched and bool(re.search(r"amass orcs 1", text, re.I)))


@dataclass
class TriggerUseMemory:
    """Only observed, parent-linked draw triggers consume a known once/turn use."""

    key: tuple | None = None
    used: set[int] = field(default_factory=set)

    def observe(self, state):
        turn = state.get("turn") or {}
        key = (state.get("match_id"), state.get("game_number"), turn.get("turn_number"))
        if key != self.key:
            self.key, self.used = key, set()
        if not key[0] or key[2] is None:
            return frozenset()  # no cross-match/turn inference without identity
        board = {c.get("instance_id"): c for c in state.get("battlefield") or []}
        for entry in state.get("stack") or []:
            parent = entry.get("parent_instance_id")
            source = board.get(parent)
            text = str(entry.get("ability_text") or entry.get("oracle_text") or "")
            # A death ability from the same source does not spend its draw trigger.
            if source:
                max_power, is_once = _draw_entry_spec(source, state)
                if (
                    max_power is not None
                    and is_once
                    and _DRAW_ENTRY.search(text)
                    and len(re.findall(r"\bwhen(?:ever)?\b", text, re.I)) == 1
                ):
                    self.used.add(parent)
        return frozenset(self.used)


def _cast_source(decision, state, chosen):
    if decision.request_type != "ActionsAvailable" or len(chosen) != 1:
        return None
    option = decision.find(chosen[0])
    if option is None or option.meta.get("actionType") != "ActionType_Cast":
        return None
    return find_source(state, option.meta)


def _claims_another_draw(name, rationale):
    # Bind the acknowledgement to the clause about this engine. An earlier
    # "we used Helix" does not acknowledge that Innocence's draw was used.
    return any(
        re.search(re.escape(name), clause, re.I)
        and re.search(r"\b(?:draw|draws|drawing)\b", clause, re.I)
        and not re.search(r"\b(?:already|used|spent|cannot|can't|no draw)\b", clause, re.I)
        for clause in re.split(r"[.;!?]", rationale)
    )


def public_sequence_concerns(decision, state, chosen, reasoning, *, used_draw_triggers=frozenset()):
    """Factual challenges for one recheck; acknowledged tradeoffs remain available."""
    source = _cast_source(decision, state, chosen)
    local = get_local_seat_id(state)
    if not source or local is None:
        return ()
    board = state.get("battlefield") or []
    rationale = str(reasoning or "")
    concerns = []
    damage = damage_effect(source)
    hand_count = state.get("opponent_hand_count", (state.get("zones") or {}).get("opponent_hand_count"))
    # Visible activation restrictions require a richer rules check. Do not
    # call the response available while such a lock is on the public board.
    activation_lock = any(re.search(r"can't activate|cannot activate", _text(c), re.I) for c in board)
    if damage and isinstance(hand_count, int) and hand_count > 0 and not activation_lock:
        for target in board:
            if target.get("controller_seat_id") in (None, local):
                continue
            name = str(target.get("name") or "")
            protection = _discard_indestructible_protection(target, state)
            kills = name and re.search(
                r"\b(?:kills?|removes?|destroy(?:s)?)\s+(?:the\s+\d+/\d+\s+)?" + re.escape(name),
                rationale,
                re.I,
            )
            acknowledged = re.search(r"\b(?:discard|indestructible|bait|force.*card)\b", rationale, re.I)
            if protection and kills and not acknowledged:
                concerns.append(
                    f"{name} has a public discard-a-card ability that grants indestructible, and its controller has cards in hand. "
                    f"{source.get('name')} does not guarantee killing it. Compare other targets; damage, lifegain and extracting a discard can still have value."
                )
    if "creature" not in str(source.get("type_line") or "").lower():
        return tuple(concerns)
    power = source.get("modified_power", source.get("power"))
    for engine in board:
        max_power, is_once = _draw_entry_spec(engine, state)
        if engine.get("controller_seat_id") != local or max_power is None:
            continue
        name = str(engine.get("name") or "draw engine")
        if engine.get("instance_id") in used_draw_triggers:
            if _claims_another_draw(name, rationale):
                concerns.append(
                    f"{name}'s once-per-turn draw trigger was already observed this turn. This entry cannot trigger that same object again; compare the creature without another draw."
                )
            continue
        if type(power) is not int or power > max_power:
            continue
        for enemy in board:
            if enemy.get("controller_seat_id") in (None, local):
                continue
            is_punisher, has_amass = _draw_punisher_spec(enemy, state)
            if is_punisher:
                enemy_name = str(enemy.get("name") or "opposing draw punisher")
                if re.search(re.escape(enemy_name), rationale, re.I):
                    continue  # the planner explicitly considered this public tradeoff
                availability = "If its once-per-turn trigger is still unused, " if is_once else ""
                concerns.append(
                    f"{availability}{source.get('name')} entering can trigger {name}'s draw, which lets {enemy_name} deal 1 damage to any target"
                    + (" and amass Orcs 1" if has_amass else "")
                    + ". Compare removing that engine before deploying a vulnerable creature; keep the draw's value and any protection in the comparison."
                )
    return tuple(concerns)
