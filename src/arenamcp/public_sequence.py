"""Public protection and draw-trigger sequencing facts for decision rechecks (#308).

These are reasons to compare lines, never a forced removal target. Unknown
opponent cards and unobserved once-per-turn uses are not invented.
"""

import re
from dataclasses import dataclass, field

from arenamcp.mana import get_local_seat_id
from arenamcp.play_safety import damage_effect, find_source

_DRAW_ENTRY = re.compile(
    r"whenever one or more other creatures you control with power (\d+) or less enter, draw a card\.", re.I
)
_ONCE = re.compile(r"this ability triggers only once (?:each|per) turn", re.I)


def _text(card):
    return re.sub(r"<[^>]*>", "", str(card.get("oracle_text") or "")).replace("’", "'")


def _draw_entry(card):
    return _DRAW_ENTRY.search(_text(card))


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
            if (
                source
                and _draw_entry(source)
                and _ONCE.search(_text(source))
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
            protection = re.search(
                r"(?:^|\n)discard a card:\s*(?:this creature|"
                + re.escape(name)
                + r") gains indestructible until end of turn\.(?:\s*tap it\.)?",
                _text(target),
                re.I,
            )
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
        match = _draw_entry(engine)
        if engine.get("controller_seat_id") != local or not match:
            continue
        name = str(engine.get("name") or "draw engine")
        if engine.get("instance_id") in used_draw_triggers:
            if (
                re.search(re.escape(name), rationale, re.I)
                and re.search(r"\b(?:draw|draws|drawing)\b", rationale, re.I)
                and not re.search(r"\b(?:already|used|spent|cannot|can't|no draw)\b", rationale, re.I)
            ):
                concerns.append(
                    f"{name}'s once-per-turn draw trigger was already observed this turn. This entry cannot trigger that same object again; compare the creature without another draw."
                )
            continue
        if type(power) is not int or power > int(match[1]):
            continue
        for enemy in board:
            text = _text(enemy)
            if enemy.get("controller_seat_id") in (None, local):
                continue
            if re.search(r"whenever an opponent draws a card", text, re.I) and re.search(
                r"deals 1 damage to any target", text, re.I
            ):
                enemy_name = str(enemy.get("name") or "opposing draw punisher")
                if re.search(re.escape(enemy_name), rationale, re.I):
                    continue  # the planner explicitly considered this public tradeoff
                availability = (
                    "If its once-per-turn trigger is still unused, " if _ONCE.search(_text(engine)) else ""
                )
                concerns.append(
                    f"{availability}{source.get('name')} entering can trigger {name}'s draw, which lets {enemy_name} deal 1 damage to any target"
                    + (" and amass Orcs 1" if re.search(r"amass orcs 1", text, re.I) else "")
                    + ". Compare removing that engine before deploying a vulnerable creature; keep the draw's value and any protection in the comparison."
                )
    return tuple(concerns)
