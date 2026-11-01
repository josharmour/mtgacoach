"""Public immediate cast-trigger facts omitted by bounded line search (#349)."""

import re

from arenamcp.combat_keywords import has_combat_keyword
from arenamcp.optional_prompts import _local_seat

_COLORED_EXILE = re.compile(
    r"\bwhen you cast this spell, exile up to one target permanent that(?:'|’)s one or more colors\.", re.I
)
_COLORS = {"w", "u", "b", "r", "g", "white", "blue", "black", "red", "green"}
_PERMANENTS = {"creature", "artifact", "enchantment", "planeswalker", "land", "battle"}


def _possible_target_restriction(card):
    text = str(card.get("oracle_text") or "").lower()
    return (
        any(has_combat_keyword(card, k) for k in ("hexproof", "shroud"))
        or bool(re.search(r"\b(?:protection|ward)\b|\bcan(?:not|'t) be (?:the )?target", text))
        or any("protection" in str(k).lower() or "ward" in str(k).lower() for k in card.get("keywords") or [])
    )


def colored_exile_payoffs(decision, state):
    """Payable supported casts plus public candidates, not a binding target menu.

    Unknown colors and protection/ward are deliberately not priced here.
    The eventual trigger's own Arena menu remains the target authority.
    """
    local = _local_seat(state)
    if decision.request_type != "ActionsAvailable" or local is None:
        return []
    opponents = [
        c
        for c in state.get("battlefield") or []
        if (c.get("controller_seat_id") or c.get("owner_seat_id")) not in (None, local)
        and not c.get("is_phased_out")
        and not c.get("face_down")
    ]
    # A permanent may grant targeting protection without it being copied into
    # each recipient's keyword list (e.g. Greater Auramancy).
    if any(
        re.search(
            r"\b(?:creatures|enchantments|artifacts|permanents)\b[^.\n]*\b(?:have|gain)\b[^.\n]*\b(?:hexproof|shroud|protection)\b",
            str(c.get("oracle_text") or ""),
            re.I,
        )
        for c in opponents
    ):
        return []
    candidates = []
    for card in opponents:
        colors = {str(c).lower().removeprefix("cardcolor_") for c in card.get("colors") or []}
        types = {str(t).lower().removeprefix("cardtype_") for t in card.get("card_types") or []}
        if not types:
            types = set(re.findall(r"\w+", str(card.get("type_line") or "").lower()))
        if colors & _COLORS and types & _PERMANENTS and not _possible_target_restriction(card):
            candidates.append(card)
    if not candidates:
        return []
    sources = {c.get("instance_id"): c for zone in ("hand", "command") for c in state.get(zone) or []}
    result = []
    for option in decision.options:
        if option.payable is not True or option.meta.get("actionType") != "ActionType_Cast":
            continue
        card = sources.get(option.meta.get("instanceId"), {})
        trigger = _COLORED_EXILE.search(str(card.get("oracle_text") or ""))
        if trigger:
            result.append((option, card, trigger.group(), candidates))
    return result


def _payoff_note(option, card, trigger, candidates):
    names = ", ".join(f"{c.get('name') or 'Permanent'} [id:{c.get('instance_id')}]" for c in candidates)
    loyalty = re.findall(r"(?<!\S)([+]?\d+:\s*[^.\n]+[.]?)", str(card.get("oracle_text") or ""))
    followup = (
        " After this planeswalker resolves, printed nonnegative loyalty choices include "
        + " / ".join(loyalty)
        + " These are not selectable while it is in hand: wait for resolution and a fresh legal activation menu. "
        "Mana from a later loyalty ability cannot pay this cast's upfront cost."
        if loyalty and "planeswalker" in str(card.get("type_line") or "").lower()
        else ""
    )
    return (
        f"IMMEDIATE CAST PAYOFF — {option.option_id} {card.get('name')}: Arena marks this cast payable. "
        f"Printed cast trigger: {trigger} Public opposing colored candidates: {names}. "
        "This trigger is separate from the spell and can resolve before the permanent; countering the "
        "spell alone does not counter its cast trigger. Recheck the trigger's actual Arena target menu "
        "and responses; these candidate facts do not guarantee removal or resolution. "
        "An unmodelled search effect is not zero value. Compare this immediate removal opportunity "
        "against tapping payment sources, losing blockers and holding interaction; do not automatically cast it."
        + followup
    )


def cast_payoff_note(decision, state):
    return "\n".join(_payoff_note(*payoff) for payoff in colored_exile_payoffs(decision, state))


def cast_payoff_concern(decision, state, chosen, reasoning):
    if chosen != ["pass"]:
        return ""
    for payoff in colored_exile_payoffs(decision, state):
        name = str(payoff[1].get("name") or "")
        if not name:
            continue
        for sentence in re.split(r"[.;!?]", str(reasoning)):
            if not re.search(rf"\b{re.escape(name.split(',')[0])}\b", sentence, re.I):
                continue
            if re.search(r"\b(?:exile|cast trigger|removal|draw|gains? \d+ life)\b", sentence, re.I):
                continue  # An acknowledged payoff with a payment tradeoff is not this contradiction.
            if re.search(
                r"\bno immediate (?:payoff|impact|value)\b|\bdoes nothing (?:now|this turn)\b", sentence, re.I
            ):
                return (
                    _payoff_note(*payoff)
                    + " Reconsider the pass using these effects; state a concrete tradeoff if still holding."
                )
    return ""


def correct_cast_payoff_claim(decision, state, chosen, data, trace):
    """Do not store a repeated false payoff claim as accepted decision memory.

    The choice remains the model's; this check supplies no card-specific cast
    override and does not invent a reason why holding is strategically better.
    """
    reason = str(data.get("reasoning") or "")
    concern = cast_payoff_concern(decision, state, chosen, reason)
    if not concern:
        return data
    trace["cast_payoff_claim"] = {"uncorrected_reasoning": reason, "facts": concern, "choice": list(chosen)}
    return {
        **data,
        "reasoning": "Holding despite a payable cast-trigger exile opportunity. The model did not justify "
        "the payment/defense tradeoff; the supported cast trigger has immediate value, not zero payoff.",
    }
