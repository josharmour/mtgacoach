"""Current-board damage for narrow creature-count activated abilities."""

import re

from arenamcp.mana import get_local_seat_id
from arenamcp.rules_engine import _normalize_mana_symbols


def creature_count_damage(
    card: dict, state: dict | None, cost: str, effect: str, metadata: dict | None = None
):
    """Forecast one creature-count damage clause after known self-sacrifice costs.

    This is the amount with the current board, not a guarantee against responses.
    An unidentified body, ambiguous sacrifice, or visible count-changing death
    trigger makes the projection unknown. A pending source already absent from
    the battlefield is not subtracted a second time.
    """
    if not isinstance(state, dict):
        return None
    local = get_local_seat_id(state)
    if local is None or card.get("controller_seat_id") not in (None, local):
        return None
    name = str(card.get("name") or "")
    self_name = r"(?:it|this creature|this permanent|cardname" + ("|" + re.escape(name) if name else "") + ")"
    match = re.fullmatch(
        self_name
        + r" deals damage equal to the number of creatures you control to (target creature|any target)\.?",
        " ".join(effect.lower().split()),
        re.I,
    )
    if not match:
        return None
    cost = _normalize_mana_symbols(re.sub(r"<[^>]*>", "", cost)).lower().strip()
    if not cost:
        return None  # effect-only metadata does not establish the activation's costs
    paid = re.sub(r"\{(?:\d+|[wubrgctq])\}[\s,]*", "", cost).strip()
    self_sacrifice = bool(
        re.fullmatch(
            r"sacrifice (?:this(?: creature| permanent)?|cardname"
            + ("|" + re.escape(name.lower()) if name else "")
            + ")",
            paid,
        )
    )
    if paid and not self_sacrifice:
        return None  # another creature, X creatures, discard, and other costs need explicit choices
    meta = metadata or {}
    context = state.get("decision_context") or {}
    parent_id = card.get("parent_instance_id") or card.get("source_parent_instance_id")
    iid = meta.get("instanceId") or card.get("instance_id")
    pending_id = context.get("source_id") or context.get("sourceId")
    stack_source = next(
        (c for c in state.get("stack") or [] if str(c.get("instance_id")) == str(pending_id or iid)), {}
    )
    if not parent_id and (not iid or str(iid) == str(pending_id)):
        parent_id = context.get("source_parent_instance_id") or stack_source.get("parent_instance_id")
    pending = bool(
        parent_id
        and (
            stack_source
            or str(card.get("object_kind") or "").upper() == "ABILITY"
            or context.get("source_parent_instance_id")
        )
    )
    source_id = parent_id or iid
    if any(c is not stack_source for c in state.get("stack") or []):
        return None  # other unresolved spells/triggers may change the count first
    if stack_source and not pending:
        return None
    board = [c for c in state.get("battlefield") or [] if not c.get("is_phased_out")]
    if any(
        re.search(
            r"damage[^.]*\binstead\b|\b(?:double|twice|triple)\b[^.]*damage|\bprevent\b[^.]*damage",
            str(c.get("oracle_text") or ""),
            re.I,
        )
        for c in board
    ):
        return None  # visible damage replacement/prevention needs its own projection
    if self_sacrifice:
        if not source_id:
            return None
        source_present = any(str(c.get("instance_id")) == str(source_id) for c in board)
        if not source_present and not pending:
            return None  # absence alone does not prove that the activation cost was paid
        # A sacrifice/death trigger may change the creature count before this
        # ability resolves. Do not assume those visible triggers do nothing.
        for obj in board:
            text = str(obj.get("oracle_text") or "").lower()
            if re.search(r"\b(?:when|whenever)\b[^.]*\b(?:dies|die|sacrifice)\b", text) and re.search(
                r"\bcreate\b|return[^.]*battlefield", text
            ):
                return None
            if obj.get("controller_seat_id") == local and re.search(
                r"isn't a creature|is not a creature", text
            ):
                return None  # losing the source may change a devotion-style type condition
    count = 0
    for obj in board:
        controller = obj.get("controller_seat_id")
        if controller is None:
            return None
        if controller != local or (self_sacrifice and str(obj.get("instance_id")) == str(source_id)):
            continue
        types = obj.get("card_types")
        if isinstance(types, list) and types:
            current_types = {str(t).lower().removeprefix("cardtype_") for t in types}
        else:
            current_types = set(
                re.findall(
                    r"\b(?:creature|land|artifact|enchantment|planeswalker|battle)\b",
                    str(obj.get("type_line") or "").lower(),
                )
            )
        if not current_types & {"creature", "land", "artifact", "enchantment", "planeswalker", "battle"}:
            return None
        count += "creature" in current_types
    return count, "any" if match[1].lower() == "any target" else "target"


def target_activation_damage(state: dict, source_id: int, fallback_oracle: str = ""):
    """Resolve one current ability's damage without discarding its cost/parent.

    Target selection can precede paying the self-sacrifice cost. Effect-only
    ability text therefore borrows a cost only from its exact current parent
    with one matching non-mana activation. Once that parent is absent, effect-
    only text is insufficient to reconstruct the cost.
    """
    from arenamcp.play_safety import _offered_ability, damage_effect

    context = state.get("decision_context") or {}
    requested_id = context.get("source_id") or context.get("sourceId")
    if requested_id and str(requested_id) != str(source_id):
        return None
    source = next((c for c in state.get("stack") or [] if str(c.get("instance_id")) == str(source_id)), None)
    local = get_local_seat_id(state)
    if source is None or local is None or source.get("controller_seat_id") != local:
        return None
    parent_id = source.get("parent_instance_id") or source.get("source_parent_instance_id")
    if not parent_id and requested_id and str(requested_id) == str(source_id):
        parent_id = context.get("source_parent_instance_id")
    if not parent_id or str(source.get("object_kind") or "").upper() != "ABILITY":
        return None
    parent = next(
        (
            c
            for c in state.get("battlefield") or []
            if str(c.get("instance_id")) == str(parent_id) and not c.get("is_phased_out")
        ),
        None,
    )
    source = {
        **source,
        "parent_instance_id": parent_id,
        "oracle_text": source.get("oracle_text") or fallback_oracle,
    }
    cost, effect = _offered_ability(source, None)
    if not cost:
        if parent is None or parent.get("controller_seat_id") != local:
            return None
        parent_cost, parent_effect = _offered_ability(parent, None)
        if not parent_cost or not parent_effect:
            return None
        ability_effect = str(source.get("oracle_text") or "")

        def canonical(text):
            text = re.sub(r"<[^>]*>", "", text).lower()
            names = [
                str(parent.get("name") or "").lower(),
                "cardname",
                "this creature",
                "this permanent",
                "it",
            ]
            for name in names:
                if name:
                    text = re.sub(r"\b" + re.escape(name) + r"\b", "SELF", text)
            return " ".join(text.split()).rstrip(".")

        if canonical(ability_effect) != canonical(parent_effect):
            return None
        cost, effect = parent_cost, parent_effect
    if parent is not None:
        source["name"] = parent.get("name") or source.get("name")
    source["oracle_text"] = f"{cost}: {effect}"
    return damage_effect(source, state, activation=True)
