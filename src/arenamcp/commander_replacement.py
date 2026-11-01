"""Compare hand/library commander replacements without treating either choice as automatic."""

from __future__ import annotations

import re
from dataclasses import dataclass

from arenamcp.optional_prompts import _ZONES, _int, _local_seat, _same_request


@dataclass(frozen=True)
class Replacement:
    card: dict
    destination: str
    casts: int | None
    printed: str
    command_cost: str
    top: bool


def replacement_facts(decision, state) -> Replacement | None:
    context = state.get("decision_context") or {}
    if decision.request_type != "OptionalAction" or not _same_request(decision, context):
        return None
    accept = decision.find("optional:accept")
    if accept is None or decision.find("optional:decline") is None:
        return None
    meta = accept.meta or {}
    if not any("ZoneTransfer" in str(m) for m in meta.get("mechanics") or []):
        return None
    recipients = {_int(i) for i in meta.get("recipients") or []}
    if len(recipients) != 1 or recipients & {None, 0}:
        return None
    if recipients != {_int(i) for i in context.get("recipient_ids") or []}:
        return None
    iid = next(iter(recipients))
    found = [
        (z, c)
        for z in _ZONES
        for c in state.get(z) or []
        if isinstance(c, dict) and _int(c.get("instance_id")) == iid
    ]
    if len(found) != 1:
        return None
    zone, card = found[0]
    local = _local_seat(state)
    if (
        zone not in {"battlefield", "stack", "hand"}
        or local is None
        or _int(card.get("owner_seat_id")) != local
        or str(card.get("object_kind") or "").lower() not in {"card", "gameobjecttype_card"}
        or card.get("is_token")
    ):
        return None
    known_ids = {
        _int(i)
        for p in state.get("players") or []
        if p.get("seat_id") == local
        for i in p.get("commander_ids") or []
    }
    known_grps = {_int(i) for i in state.get("commander_grp_ids") or []} - {None}
    if iid not in known_ids and not ({_int(card.get("grp_id")), _int(card.get("base_grp_id"))} & known_grps):
        return None
    question = str(
        context.get("prompt") or getattr(decision, "prompt_text", "") or meta.get("promptText") or ""
    )
    # Use the current rendered Arena question, not an inferred destination from an old spell.
    match = re.fullmatch(r"Put (.+) in the command zone or in your (hand|library)\?", question.strip(), re.I)
    if not match or match.group(1).casefold() != str(card.get("name") or "").casefold():
        return None
    from arenamcp.board_model import _casts_so_far, _with_generic

    casts = _casts_so_far(state, card)
    printed = str(card.get("mana_cost") or "")
    cost = _with_generic(printed, 2 * casts) if printed and casts is not None else ""
    # A single other counter on the stack with this exact instruction establishes top.
    # Other library movements retain unknown placement rather than inventing the next draw.
    others = [c for c in state.get("stack") or [] if c.get("instance_id") != iid]
    top = (
        match.group(2).lower() == "library"
        and zone == "stack"
        and len(others) == 1
        and bool(
            re.search(
                r"counter target spell\.\s*if that spell is countered this way, put it on top of its owner's library",
                str(others[0].get("oracle_text") or "").replace("’", "'"),
                re.I,
            )
        )
    )
    return Replacement(card, match.group(2).lower(), casts, printed, cost, top)


def replacement_note(decision, state) -> str:
    facts = replacement_facts(decision, state)
    if facts is None:
        return ""
    name = facts.card["name"]
    destination = "top of your library" if facts.top else "your " + facts.destination
    note = (
        f"COMMANDER REPLACEMENT COMPARISON: {name} [instance {facts.card['instance_id']}]. "
        f"Accept sends this card to the command zone; decline sends it to {destination}. "
        "Neither choice casts it or pays mana now; relocation does not itself increase commander tax. "
    )
    if facts.printed and facts.command_cost:
        note += (
            f"Base recast from hand: {facts.printed}; from command: {facts.command_cost} "
            f"({facts.casts} previous command-zone cast(s), +{{{2 * facts.casts}}} tax). "
            "These are base mana costs plus commander tax; apply any current cost modifiers separately. "
        )
    else:
        note += "Commander cast count or printed cost is unknown; do not assume a two-mana difference. "
    if facts.destination == "hand":
        note += (
            "Hand preserves access without paying commander tax. When hand is cheaper, prefer that recast unless a concrete "
            "hand-risk, casting restriction, or other benefit justifies command-zone access. "
        )
    elif facts.top:
        note += (
            "This is top-library, not a return to hand. The commander must be drawn/recovered before a hand cast. "
            "Compare drawing it for a tax-free hand recast with command-zone access plus drawing another card; "
            "account for turn order, draw replacement/denial, milling and current mana. "
            "A normal draw trigger applies in either branch; top-library does not itself add an extra draw. "
        )
    else:
        note += (
            "This is library, not hand; placement/recovery timing is not established here. "
            "Weigh reliable command-zone access against actual library recovery and any tax saved on a hand recast. "
        )
    return (
        note
        + "Explain the concrete tradeoff; do not choose command automatically merely because it is the commander."
    )


def replacement_concern(decision, state, chosen, reason) -> str:
    if chosen != ["optional:accept"]:
        return ""
    facts = replacement_facts(decision, state)
    if facts is None or not facts.command_cost or not facts.casts:
        return ""
    # Both choices remain legal. Only ask for a comparison when accepting the
    # more expensive recast with no cost/recovery/risk tradeoff at all.
    if re.search(
        r"\b(?:tax|mana|cost|cheaper|draw|drawing|discard|mill|lock|denial|restrict\w*)\b", str(reason), re.I
    ):
        return ""
    return replacement_note(decision, state)
