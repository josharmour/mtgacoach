"""Factual blink intent checks: object identity and enchantment entry (#311).

These checks report contradictions for a recheck; they do not rank blink
payoffs or redirect an opposing target to an arbitrary friendly permanent.
"""

import re
from dataclasses import dataclass

from arenamcp.mana import get_local_seat_id
from arenamcp.play_safety import find_source
from arenamcp.target_effects import effect_mentions_blink


@dataclass(frozen=True)
class BlinkMismatch:
    reason: str
    resolve_first: bool = False


def _named_target(reasoning: str, target: dict) -> bool:
    name = str(target.get("name") or "")
    if not name:
        return False
    # Require a target relationship, not a card mentioned elsewhere in a plan.
    match = re.search(
        r"\b(?:on|target(?:ing)?|blink(?:ing)?)\s+(?:the returning |my |our |own )?"
        + re.escape(name)
        + r"(?=$|[\s.,;:#])",
        reasoning,
        re.I,
    )
    if match is None:
        return False
    prefix = reasoning[: match.start()].rsplit(".", 1)[-1].rsplit(";", 1)[-1]
    return re.search(r"\b(?:not|can't|cannot|don't|never|if)\b", prefix, re.I) is None


def blink_intent_issue(decision, state: dict, chosen: list[str], reasoning: str) -> BlinkMismatch | None:
    """Return only a proved contradiction in an explicitly named own-blink plan.

    Unknown targets/rules and opposing stack actions leave the normal planner in
    charge. The stack's current target ids are authoritative, not matching names
    or an assumption that a returned permanent retains its old identity.
    """
    if decision.request_type != "ActionsAvailable" or len(chosen) != 1:
        return None
    option = decision.find(chosen[0])
    if option is None or option.meta.get("actionType") != "ActionType_Cast":
        return None
    source = find_source(state, option.meta)
    if not effect_mentions_blink(str(source.get("oracle_text") or "")):
        return None
    local = get_local_seat_id(state)
    turn = state.get("turn") or state.get("turn_info") or {}
    if local is None or turn.get("active_player") != local:
        return None
    stack = state.get("stack") or []
    if any(entry.get("controller_seat_id") != local for entry in stack):
        return None  # protection/counter-response timing can justify a second blink
    text = str(reasoning or "").replace("’", "'")
    for target in state.get("battlefield") or []:
        if not _named_target(text, target):
            continue
        if target.get("controller_seat_id") not in (None, local):
            lock = re.search(
                r"during your turn, your opponents can't cast spells",
                str(target.get("oracle_text") or "").replace("’", "'"),
                re.I,
            )
            claimed = re.search(
                r"\b(?:unlock|allow|enable|let|open)\w*\b[^.;]*\b(?:cast|spells)\b", text, re.I
            )
            if lock and claimed and not re.search(r"\b(?:not|can't|cannot|don't|if)\b", text, re.I):
                return BlinkMismatch(
                    f"{target['name']}'s spell restriction applies during its controller's turn. "
                    "This is our turn, so that restriction does not prevent our casts; blinking it does not unlock them. "
                    "Reconsider the blink or identify another concrete benefit."
                )
            continue
        if target.get("controller_seat_id") != local:
            continue
        iid = target.get("instance_id")
        if not iid:
            continue
        for pending in stack:
            targets = pending.get("targeting") or []
            if (
                len(stack) == 1
                and iid in targets
                and effect_mentions_blink(str(pending.get("oracle_text") or ""))
            ):
                return BlinkMismatch(
                    f"{pending.get('name') or 'The pending blink'} already targets {target['name']} #{iid}. "
                    f"Blinking that object again first returns a new object; the earlier spell cannot target "
                    "the returned object using the old id. Let the pending blink resolve before considering another.",
                    resolve_first=True,
                )
        # Constellation's condition is enchantment entry, not the return of its
        # own nonenchantment observer. Real ETB engines (e.g. a Shrine tutor) may
        # create an indirect enchantment entry and are intentionally preserved.
        types = str(target.get("type_line") or "").lower()
        oracle = str(target.get("oracle_text") or "").lower()
        if not types or "enchantment" in types:
            continue
        has_constellation = re.search(
            r"whenever an enchantment enters(?: the battlefield)? under your control", oracle
        )
        self_etb = re.search(r"\bwhen\b[^.\n]*\benters\b", oracle)
        indirect = any(
            re.search(
                r"\bwhenever\b[^.\n]*\bcreature\b[^.\n]*\benters\b", str(c.get("oracle_text") or ""), re.I
            )
            for c in state.get("battlefield") or []
            if c.get("controller_seat_id") == local
        )
        if (
            has_constellation
            and not self_etb
            and not indirect
            and re.search(
                r"\b(?:add\w*|trigger\w*|stack\w*)\b[^.;]*\bconstellation\b|\bconstellation trigger\b",
                text,
                re.I,
            )
            and not re.search(r"\b(?:not|can't|cannot|don't|never|if)\b[^.;]*\bconstellation\b", text, re.I)
        ):
            return BlinkMismatch(
                f"{target['name']} is {target['type_line']}, not an enchantment. Its constellation condition is "
                "an enchantment entering under your control; returning this nonenchantment alone does not trigger it. "
                "Reconsider the blink or identify a different concrete benefit."
            )
    return None
