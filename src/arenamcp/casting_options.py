"""CastingTimeOptions without the model: finish the cast (never cancel it), and say what was chosen.

Ledger #121, match 56fc394b (2026-10-19, 13:05 to 13:07): a Cloudflare outage
cut the model off. The line pick cast Burst Lightning, and its kicker step
(Arena's CastingTimeOptions with Kicker and Done) went to the mechanical
fallback, which declined every CastingTimeOptions request. The autopilot
cancelled its own cast ("typed-decision CastingTimeOptions: declined (cancel)"),
and the self-cancel guard then withheld Burst Lightning for the turn. This
happened on T6, T8, T10 and T12.

:func:`casting_fallback_choice` picks, in this order:

- the kicker, when its whole cost fits our mana and the searched line needs
  the kicked effect: the line's creature target survives the plain damage
  but dies to the kicked damage, or the kicked damage to the opponent's face
  is lethal and the plain damage is not;
- Done, which casts the spell normally, with no kicker or other optional cost;
- a required mode: the mode the per-mode line search scores best, when every
  mode was searched and nothing is unmodelled; otherwise the first legal
  mode or modes;
- X: the largest offered value our mana pays for, or the smallest offered
  value when the mana is not exactly known;
- any other choice: its first legal selection.

:func:`casting_option_speech` names the chosen option for the voice and the
log. Ledger #119, 12:48:55: Done on Burst Lightning's kicker step was spoken
as "Passing", because "Done" read as a pass.
"""

from __future__ import annotations

import logging
from typing import Any

from arenamcp.decisions import casting_selection_bounds

logger = logging.getLogger(__name__)

# Extras the cast does not need: Done (cast normally) is preferred to them.
_OPTIONAL_KINDS = frozenset({"kicker", "additional_cost", "cost_keyword"})


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _groups(decision: Any) -> list[list[Any]]:
    """The menu's options per casting child (childIndex, choiceKind), in menu order."""
    groups: dict[tuple[Any, Any], list[Any]] = {}
    for option in getattr(decision, "options", ()) or ():
        meta = option.meta or {}
        groups.setdefault((meta.get("childIndex"), meta.get("choiceKind")), []).append(option)
    return list(groups.values())


def _kind(group: list[Any]) -> str:
    return str((group[0].meta or {}).get("choiceKind") or "")


def casting_spell(decision: Any, state: dict[str, Any]) -> dict[str, Any]:
    """The spell being cast: the options' source on the stack, the request's source, or our top spell."""
    from arenamcp.mana import get_local_seat_id
    from arenamcp.play_safety import find_source

    ids = [_int((option.meta or {}).get("sourceId")) for option in getattr(decision, "options", ()) or ()]
    context = state.get("decision_context") or {}
    ids.append(_int(context.get("source_id") or context.get("sourceId")))
    for iid in ids:
        if iid:
            card = find_source(state, {"instanceId": iid})
            if card:
                return card
    for name in (context.get("source_card"), getattr(decision, "source_label", "")):
        if name:
            card = find_source(state, {}, str(name))
            if card:
                return card
    local = get_local_seat_id(state)
    for card in state.get("stack") or []:  # the topmost spell of ours (arenamcp.stack_order)
        if (
            isinstance(card, dict)
            and local is not None
            and (card.get("controller_seat_id") or card.get("owner_seat_id")) == local
            and str(card.get("object_kind") or "").upper() != "ABILITY"
        ):
            return card
    return {}


def _cost_total(costs: Any) -> int:
    return sum(_int(cost.get("count")) for cost in costs or () if isinstance(cost, dict))


def kicked_cost(option: Any, card: dict[str, Any]) -> int | None:
    """Mana the kicked cast needs in all, or None when neither the request nor the card says.

    Arena's Kicker option carries the whole kicked cost (Burst Lightning: {R}
    and four generic); the card's own cost plus its kicker is the floor.
    """
    from arenamcp.mana import mana_cost_to_cmc
    from arenamcp.play_safety import _KICKER_COST
    from arenamcp.rules_engine import _normalize_mana_symbols

    totals = [_cost_total((option.meta or {}).get("manaCost"))]
    kicker = _KICKER_COST.search(str(card.get("oracle_text") or ""))
    cost = str(card.get("mana_cost") or "")
    if kicker and cost:
        try:
            totals.append(
                mana_cost_to_cmc(_normalize_mana_symbols(cost))
                + mana_cost_to_cmc(_normalize_mana_symbols(kicker.group(1)))
            )
        except Exception:
            pass
    total = max(totals)
    return total or None


def _opponent_life(state: dict[str, Any]) -> int | None:
    from arenamcp.mana import get_local_seat_id

    local = get_local_seat_id(state)
    for player in state.get("players") or []:
        if (
            isinstance(player, dict)
            and player.get("seat_id") != local
            and player.get("life_total") is not None
        ):
            return _int(player.get("life_total"))
    return None


def _line_creature(state: dict[str, Any], name: str, instance_id: int) -> dict[str, Any] | None:
    from arenamcp.mana import get_local_seat_id

    local = get_local_seat_id(state)
    theirs = [
        card
        for card in state.get("battlefield") or []
        if isinstance(card, dict)
        and (card.get("controller_seat_id") or card.get("owner_seat_id")) not in (None, local)
    ]
    return next(
        (c for c in theirs if instance_id and _int(c.get("instance_id")) == instance_id), None
    ) or next((c for c in theirs if name and c.get("name") == name), None)


def kicker_wanted(
    option: Any, card: dict[str, Any], state: dict[str, Any], line_target: str = "", line_target_id: int = 0
) -> str:
    """Why the searched line needs this kicked cast, or ''."""
    from arenamcp.play_safety import damage_effect, damage_would_kill
    from arenamcp.ward import available_mana

    if not card or not line_target:
        return ""
    plain, kicked = damage_effect(card, kicked=False), damage_effect(card, kicked=True)
    if plain is None or kicked is None or kicked[0] <= plain[0]:
        return ""
    needed = kicked_cost(option, card)
    mana = available_mana(state)
    if needed is None or mana < needed:
        return ""
    name = str(card.get("name") or "the spell")
    if line_target == "opponent":
        life = _opponent_life(state)
        if kicked[1] == "any" and life is not None and plain[0] < life <= kicked[0]:
            return f"kicked, {name}'s {kicked[0]} damage is lethal (they are at {life}); {mana} mana pays {needed}"
        return ""
    target = _line_creature(state, line_target, line_target_id)
    if target is None or damage_would_kill(target, plain[0]) is not False:
        return ""
    if damage_would_kill(target, kicked[0]) is not True:
        return ""
    return (
        f"the searched line aims {name} at {line_target}, which only the kicked {kicked[0]} damage kills; "
        f"{mana} mana pays {needed}"
    )


def _best_mode(decision: Any, state: dict[str, Any], group: list[Any], comparison: Any) -> tuple[str, str]:
    """(option id, why) of the mode the per-mode line search scores best, or ('', '')."""
    if comparison is None:
        try:
            from arenamcp import line_guard

            comparison = line_guard.mode_comparison(state, decision)
        except Exception as error:  # the strategic layer never blocks a decision
            logger.debug("mode comparison skipped: %s", error)
            return "", ""
    if (
        comparison is None
        or not getattr(comparison, "complete", False)
        or getattr(comparison, "contingent", None)
    ):
        return "", ""
    modes = getattr(comparison, "modes", {}) or {}
    lines = getattr(comparison, "lines", {}) or {}
    order = [option.option_id for option in group]
    searched = [oid for oid in order if oid in lines]
    if len(searched) != len(order) or any(modes.get(oid) == "other" for oid in order):
        return "", ""
    best = max(searched, key=lambda oid: (lines[oid].score, -order.index(oid)))
    if not decision.selection_is_valid([best]):
        return "", ""
    line = lines[best]
    outcome = line.outcome_text() if hasattr(line, "outcome_text") else ""
    mode = modes.get(best) or next(o.label for o in group if o.option_id == best)
    return best, f"the line search scores the mode '{mode}' best" + (f" ({outcome})" if outcome else "")


def _x_choice(group: list[Any], decision: Any, state: dict[str, Any]) -> tuple[str, str]:
    from arenamcp.play_safety import affordable_x, numeric_option_source, x_body_dies_at

    values = sorted(
        (_int((option.meta or {}).get("numericValue")), option.option_id)
        for option in group
        if (option.meta or {}).get("numericValue") is not None
    )
    if not values:
        return "", ""
    card = numeric_option_source(state, decision)
    maximum = affordable_x(state, card) if card else None
    alive = [(value, oid) for value, oid in values if not (card and x_body_dies_at(card, value))] or values
    if maximum is not None:
        paid = [(value, oid) for value, oid in alive if value <= maximum]
        if paid:
            return paid[-1][1], f"X={paid[-1][0]}, the largest our mana pays for"
    return alive[0][1], f"X={alive[0][0]}, the smallest offered (our mana is not exactly known)"


def casting_fallback_choice(
    decision: Any,
    state: dict[str, Any],
    *,
    line_target: str = "",
    line_target_id: int = 0,
    comparison: Any = None,
) -> tuple[list[str], str]:
    """(option ids, reason) that finish this cast without the model; ([], '') only for an empty menu.

    ``line_target``/``line_target_id`` are where the searched line aimed this
    spell when it was cast ("opponent", or a creature's name and id).
    ``comparison`` is a precomputed per-mode line search (else it is run here).
    """
    state = state or {}
    groups = _groups(decision)
    if not groups:
        return [], ""
    card = casting_spell(decision, state)
    name = str(card.get("name") or "the spell")
    by_kind = {_kind(group): group for group in reversed(groups)}  # the first group of each kind
    kicker = by_kind.get("kicker")
    if kicker:
        why = kicker_wanted(kicker[0], card, state, line_target, line_target_id)
        if why and decision.selection_is_valid([kicker[0].option_id]):
            return [kicker[0].option_id], f"cast {name} kicked: {why}"
    done = by_kind.get("done")
    if done and decision.selection_is_valid([done[0].option_id]):
        extras = [kind for kind in by_kind if kind in _OPTIONAL_KINDS]
        how = "unkicked" if "kicker" in extras else "normally"
        return [done[0].option_id], f"cast {name} {how}"
    modal = by_kind.get("modal")
    if modal:
        minimum = casting_selection_bounds(modal[0].meta or {})[0]
        if minimum == 1:
            best, why = _best_mode(decision, state, modal, comparison)
            if best:
                return [best], f"cast {name}: {why}"
        first = [option.option_id for option in modal[: max(1, minimum)]]
        if decision.selection_is_valid(first):
            labels = ", ".join(option.label for option in modal[: len(first)])
            return first, f"cast {name} with its first legal mode ({labels})"
    numeric = by_kind.get("numeric_input")
    if numeric:
        option_id, why = _x_choice(numeric, decision, state)
        if option_id and decision.selection_is_valid([option_id]):
            return [option_id], f"cast {name} with {why}"
    required = [group for group in groups if _kind(group) not in _OPTIONAL_KINDS]
    for group in [*required, *(group for group in groups if group not in required)]:
        minimum = casting_selection_bounds(group[0].meta or {})[0]
        first = [option.option_id for option in group[: max(1, minimum)]]
        if decision.selection_is_valid(first):
            return first, f"cast {name} with its first legal choice ({group[0].label})"
    return [], ""


def casting_option_speech(decision: Any, option_ids: list[str], state: dict[str, Any]) -> str:
    """The submitted casting choice as a sentence ("Cast Burst Lightning unkicked."), or '' for a mode or X.

    Done casts the spell (unkicked when a kicker was offered); the kicker,
    an additional cost or a timing permission casts it with that. A mode or
    an X value keeps its own label.
    """
    chosen = [decision.find(option_id) for option_id in option_ids]
    if len(chosen) != 1 or chosen[0] is None:
        return ""
    kind = str((chosen[0].meta or {}).get("choiceKind") or "")
    name = str(casting_spell(decision, state).get("name") or "").strip() or "the spell"
    if kind == "done":
        kinds = {_kind(group) for group in _groups(decision)}
        if "kicker" in kinds:
            return f"Cast {name} unkicked."
        if kinds & _OPTIONAL_KINDS:
            return f"Cast {name} without the extra cost."
        return f"Cast {name}."
    if kind == "kicker":
        return f"Cast {name} kicked."
    if kind == "additional_cost":
        return f"Cast {name} paying the additional cost."
    if kind == "timing_permission":
        return f"Cast {name} at instant speed."
    return ""
