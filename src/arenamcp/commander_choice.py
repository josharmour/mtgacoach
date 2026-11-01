"""Ground commander relocation in the recipient's identity, not surviving copies (#343)."""

from __future__ import annotations

import re

from arenamcp.optional_prompts import _ZONES, _int, _local_seat, _same_request


def commander_zone_note(decision, state: dict) -> str:
    """Only assert post-death/exile facts for this exact recognized commander request."""
    context = state.get("decision_context") or {}
    if (
        decision.request_type != "OptionalAction"
        or not context.get("commander_return")
        or not _same_request(decision, context)
        or decision.find("optional:decline") is None
    ):
        return ""
    accept = decision.find("optional:accept")
    if accept is None:
        return ""
    recipients = {_int(i) for i in accept.meta.get("recipients") or []}
    if not recipients or None in recipients or 0 in recipients:
        return ""
    if recipients != {_int(i) for i in context.get("recipient_ids") or []}:
        return ""
    local = _local_seat(state)
    facts = []
    for iid in sorted(recipients):
        found = [
            (zone, card)
            for zone in _ZONES
            for card in state.get(zone) or []
            if isinstance(card, dict) and _int(card.get("instance_id")) == iid
        ]
        if len(found) != 1:
            return ""  # Missing or contradictory membership is not evidence.
        zone, card = found[0]
        if (
            zone not in {"graveyard", "exile"}
            or local is None
            or _int(card.get("owner_seat_id")) != local
            or str(card.get("object_kind") or "").lower() not in {"card", "gameobjecttype_card"}
            or card.get("is_token")
        ):
            return ""
        facts.append(f"{card.get('name') or 'Commander'} [instance {iid}] is already in {zone}.")
    return (
        "COMMANDER ZONE FACTS: "
        + " ".join(facts)
        + " Accept moves that recipient to the command zone; it does not cast it or pay a recast tax now."
        " Decline leaves that recipient in its current zone; it cannot keep or restore it on the battlefield."
        " Same-name battlefield copies are different objects and are not moved by this choice."
        " A concrete graveyard/reanimation or exile recovery plan may justify declining; compare that plan"
        " with command-zone access using the recipient's actual zone, not an older battlefield plan."
    )


def false_battlefield_decline(chosen: list[str], reason: str) -> bool:
    """Narrow contradiction check; graveyard value and recast-tax judgments are left to the planner."""
    if chosen != ["optional:decline"]:
        return False
    for clause in re.split(r"[.;!?]|\bbut\b", str(reason).lower()):
        if re.search(r"\b(?:not|no|never|cannot|can't|won't|doesn't|isn't|aren't)\b", clause):
            continue
        if re.search(r"\b(?:copy|copies|token|tokens)\b", clause):
            continue
        if re.search(
            r"\b(?:keep(?:ing|s)? (?:the |our |my |your )?(?:commander|original(?: commander)?|it)"
            r"|(?:the |our |my |your )?(?:commander|original|it) (?:stay(?:s|ing)?|remain(?:s|ing)?))"
            r" (?:safely |still )?on (?:the )?battlefield\b",
            clause,
        ):
            return True
    return False


def review_commander_decline(decision, state, data, chosen, ask, parse, message, trace):
    """One correction within the caller's existing deadline; never invent a strategic accept."""
    note = commander_zone_note(decision, state)
    if not note or not false_battlefield_decline(chosen, data.get("reasoning", "")):
        return data, chosen
    check = {"original_ids": list(chosen), "original_reasoning": data.get("reasoning"), "facts": note}
    trace["commander_zone_check"] = check
    correction = (
        "\n\nCOMMANDER ZONE CHECK: Your decline was justified by keeping the commander on the battlefield. "
        + note
        + " Reconsider both legal choices. If declining, explain the actual recovery/value plan in its"
        " current zone. Reply with the same JSON schema."
    )
    try:
        revised, picked = parse(ask(message + correction, effort="low", budget=12.0))
        check.update(revised_ids=list(picked), revised_reasoning=revised.get("reasoning"))
        if false_battlefield_decline(picked, revised.get("reasoning", "")):
            raise ValueError("commander decline still contradicts the recipient's current zone")
    except Exception as error:
        check["failed"] = str(error)[:200]
        # The caller's normal OptionalAction failure path declines automation,
        # not the Arena prompt. Do not submit the known-false original answer.
        raise
    return revised, picked
