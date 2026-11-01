"""Small, explicit public rules reminders for session grounding failures.

These are facts and limitations, not a claim that a different play wins.
They accompany fresh board math even when no model-authored plan exists.
"""

import re

from arenamcp.combat_keywords import has_combat_keyword
from arenamcp.combat_solver import leaves_at_our_end_step


def public_rules_notes(state):
    if not isinstance(state, dict):
        return []
    local = state.get("local_seat_id") or next(
        (p.get("seat_id") for p in state.get("players", []) if p.get("is_local")), None
    )
    notes = [fact for _, fact, _ in _turn_loss_multipliers(state)]
    for card in state.get("battlefield", []) + state.get("hand", []):
        if not isinstance(card, dict) or card.get("is_phased_out") or card.get("face_down"):
            continue
        name = card.get("name")
        if not name:
            continue
        text = str(card.get("oracle_text") or "").lower().replace(str(name).lower(), "this creature")
        own = card.get("controller_seat_id", card.get("owner_seat_id")) == local
        board = card in state.get("battlefield", [])
        if own and leaves_at_our_end_step(card):
            notes.append(
                f"{name} leaves at your end step; it cannot be held as a blocker for the following opposing turn."
            )
        if board and not own and has_combat_keyword(card, "reach") and not card.get("is_tapped"):
            notes.append(
                f"{name} has reach and is untapped: it can block a flyer. Do not call flying damage unblocked or lethal without accounting for this blocker."
            )
        death = re.search(r"when (?:this creature|it) dies, (create [^.]+creature token[^.]*\.)", text)
        if death:
            notes.append(
                f"{name} has a death trigger: {death[1]} Killing it does not deny that token; exile or a suppressed trigger is different."
            )
        if (
            "return it to the battlefield" in text
            and "enchantment" in text
            and "when" in text
            and "dies" in text
        ):
            notes.append(
                f"{name} returns as an enchantment after dying. Killing the creature removes its body, not its persistent engine; the returned enchantment retains its abilities."
            )
        if board and "whenever" in text and "counter" in text and state.get("stack"):
            notes.append(
                f"{name}: do not count unresolved counter growth before damage above it on the stack; use current toughness until the trigger resolves."
            )
    return list(dict.fromkeys(notes))[:12]


def rules_block(state):
    notes = public_rules_notes(state)
    return "\n  RULES CHECK: " + "\n  ".join(notes) if notes else ""


def claim_notes(text, state):
    """Flag the session's repeated concrete rule contradictions in displayed rationale."""
    lower = str(text or "").lower().replace("’", "'")
    if not lower:
        return []
    notes = public_rules_notes(state)
    found = damage_attribution_notes(text, state)
    for fact in notes:
        name = (
            fact.split(" has a death trigger:", 1)[0]
            .split(" leaves at your end step", 1)[0]
            .split(" returns as an enchantment", 1)[0]
        )
        if (
            "death trigger:" in fact
            and name.lower() in lower
            and re.search(
                r"\b(?:deny|denies|denying|prevent|prevents|preventing|stop|stops)\b[^.]{0,85}\btoken|\bbefore it can[^.]{0,50}\bleave[^.]{0,30}\btoken",
                lower,
            )
        ):
            if "exile" not in lower and not re.search(
                r"\b(?:doesn't|does not|cannot|can't|won't|not)\b", lower
            ):
                found.append(fact)
        elif (
            "leaves at your end step" in fact
            and name.lower() in lower
            and re.search(r"\b(?:blocker|block|blocks|blocking|defense|defensive)\b", lower)
        ):
            if not re.search(
                r"\b(?:no|not|can't|cannot)\b[^.;,]{0,25}\bblock|\b(?:leaves|returns)\b[^.;,]{0,35}\b(?:end step|hand)\b",
                lower,
            ):
                found.append(fact)
        elif "has reach and is untapped" in fact and re.search(
            r"\b(?:no reach|no (?:flying |flyer |untapped )?blockers|unblockable flyers?)\b", lower
        ):
            found.append(fact)
        elif (
            "returns as an enchantment" in fact
            and name.lower() in lower
            and re.search(
                r"\b(?:kill|kills|remove|removes|stop|stops|end|ends|shut|deny|denies)\b[^.]{0,65}\b(?:engine|drain|lifegain)",
                lower,
            )
        ):
            if "exile" not in lower and (
                "harmless enchantment" in lower
                or not re.search(r"\b(?:not|doesn't|cannot|can't|still|remains|persists)\b", lower)
            ):
                found.append(fact)
    return found


def corrected_reasoning(text, state):
    """Replace proven rules contradictions; preserve raw text separately in diagnostics."""
    notes = claim_notes(text, state)
    return "Rules correction: " + " ".join(notes) if notes else str(text or "")


def _turn_loss_multipliers(state):
    """Public, controller-relative facts for Bloodletter's exact replacement rule.

    Ownership is deliberately not a substitute for control of a permanent.
    Unknown control/turn and hidden cards cannot establish a contradiction.
    """
    if not isinstance(state, dict):
        return []
    local = state.get("local_seat_id") or next(
        (p.get("seat_id") for p in state.get("players", []) if p.get("is_local")), None
    )
    active = (state.get("turn") or {}).get("active_player")
    if local is None or active is None:
        return []
    result = []
    for card in state.get("battlefield") or []:
        if not isinstance(card, dict) or card.get("is_phased_out") or card.get("face_down"):
            continue
        controller = card.get("controller_seat_id")
        name = str(card.get("name") or "")
        oracle = " ".join(str(card.get("oracle_text") or "").lower().split())
        if (
            controller is None
            or not name
            or not re.search(
                r"if an opponent would lose life during your turn, they lose twice that much life instead",
                oracle,
            )
        ):
            continue
        ours = controller == local
        applies = ours and active == controller
        fact = (
            f"{name} is controlled by {'you' if ours else 'the opponent'}: it doubles life loss only "
            "for that controller's opponents during that controller's turn. "
            + (
                "It applies to the opponent's life loss now."
                if applies
                else "It does not double life loss dealt to the opponent now."
            )
        )
        result.append((name, fact, applies))
    return result


def damage_attribution_notes(text, state):
    """Contradicted claims that a controller-relative effect boosts our face burn."""
    lower = str(text or "").lower().replace("’", "'")
    multipliers = _turn_loss_multipliers(state)
    found = []
    for name, fact, applies in multipliers:
        # Two copies under different controllers make an abbreviated name ambiguous.
        if applies or any(other == name and active for other, _, active in multipliers):
            continue
        keys = [name.lower(), name.lower().split()[0]]
        for sentence in re.split(r"(?<=[.!?])\s+", lower):
            if not any(re.search(rf"\b{re.escape(key)}\b", sentence) for key in keys):
                continue
            if not re.search(r"\bdoubl(?:e|es|ed|ing)\b", sentence):
                continue
            if re.search(r"\b(?:not|no|doesn't|cannot|can't|won't|without|if|would|unless)\b", sentence):
                continue
            if re.search(
                r"\b(?:my turn|our turn|my damage|our damage|our burn|my burn|to (?:the |their )?face|face burn)\b",
                sentence,
            ):
                found.append(fact)
                break
    return list(dict.fromkeys(found))
