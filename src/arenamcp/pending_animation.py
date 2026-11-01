"""Narrow repeat checks for already-recognized, nonadditive self-animation."""

from __future__ import annotations

import re

from arenamcp.stack_order import stack_top


def repeat_animation_has_payoff(state: dict, ability: str, metadata: dict) -> bool:
    """Keep unusual costs and activation/payment triggers as model decisions.

    A creature buff below the animation benefits its first resolution, not
    another copy. It is deliberately not evidence of a repeat payoff (#348).
    """
    cost, separator, _ = ability.partition(":")
    if not separator or not re.fullmatch(r"(?:\s*\{o?[0-9wubrgc/]+\}\s*)+", cost, re.I):
        return True
    pays_by_tapping = any(payment.get("instanceId") for payment in metadata.get("autoTapActions") or [])
    for permanent in [*(state.get("battlefield") or []), *(state.get("stack") or [])]:
        text = str(permanent.get("oracle_text") or "")
        if re.search(r"\b(?:when|whenever)\b[^.\n]*\bactivat\w*\b", text, re.I):
            return True
        if pays_by_tapping and re.search(r"\b(?:when|whenever)\b[^.\n]*\btap\w*\b", text, re.I):
            return True
    return False


def pending_animation_reason(state: dict, card: dict, metadata: dict) -> str:
    """Require current exact source, ability, controller and top-of-stack identity.

    No submission history is retained: a rejected/cancelled/removed ability
    cannot leave a sticky ban. An intervening response may justify reactivation.
    The caller must first prove the effect is a simple temporary animation.
    """
    pending = stack_top(state)
    source, ability = metadata.get("instanceId"), metadata.get("abilityGrpId")
    local = state.get("local_seat_id") or next(
        (p.get("seat_id") for p in state.get("players") or [] if p.get("is_local")), None
    )
    if not pending or not local or not all(type(v) is int and v > 0 for v in (source, ability)):
        return ""
    if (
        source == card.get("instance_id") == pending.get("parent_instance_id")
        and ability == pending.get("grp_id")
        and str(pending.get("object_kind") or "").removeprefix("GameObjectType_").upper() == "ABILITY"
        and (pending.get("controller_seat_id") or pending.get("owner_seat_id")) == local
        and (card.get("controller_seat_id") or card.get("owner_seat_id")) == local
    ):
        return "this source's same animation is already pending on top of the stack; let it resolve"
    return ""
