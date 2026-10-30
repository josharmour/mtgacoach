"""Deterministic answers to an opponent's spell that targets one of our permanents.

2026-10-18 pro game (PremierDraft_YFRA, match 3 turn 12): the opponent cast
Silence the Echo ("Destroy target creature or planeswalker") on Valgavoth; the
pro flashed in Teyo, Lightshield Expert in response ("target permanent you
control gains hexproof until end of turn") and the spell fizzled. The coach
only had a prompt rule ("decide whether to respond"); this module finds the
window from the snapshot itself: an opponent object on the stack whose
targets include our permanent, and an instant-speed card in hand we can pay
for whose effect (an instant's text, or a flash creature's enters trigger)
gives a permanent we control hexproof, shroud, protection or indestructible.

Rules read from Oracle text, never card names. Pure: no I/O, no LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from arenamcp.mulligan_policy import COLORS, _land_colors, _pip_matching, hand_card

# "Target permanent you control gains hexproof and indestructible", "target creature you
# control gains protection from the color of your choice", "... can't be the target of ...".
_SHIELD = re.compile(
    r"\btarget (?P<what>permanent|creature|planeswalker|nonland permanent|artifact or creature)"
    r"(?: or (?:planeswalker|creature))? you control (?:gains?|has|have|gets?[^.]*?and gains?) "
    r"(?P<grant>[^.]*)"
)
_GROUP_SHIELD = re.compile(
    r"^(?P<what>permanents|creatures|artifacts|planeswalkers) you control "
    r"(?:gain|have) (?P<grant>[^.]*)"
)
_INDESTRUCTIBLE = re.compile(r"\bindestructible\b")
# An effect indestructible does not stop: exile, -N/-N, bounce, tuck, base 0/0.
_NOT_DESTROY = re.compile(
    r"\bexile\b|\bgets? -\d+/-\d+|\breturn [^.]*to (?:its|their) owner'?s? hand\b"
    r"|\bon (?:the )?(?:top|bottom) of (?:its|their) owner'?s? library\b|\bbase power and toughness 0/0\b"
)


@dataclass(frozen=True)
class ShieldResponse:
    threat: str  # the opponent's spell or ability on the stack
    target: str  # our permanent it targets
    answers: tuple[str, ...]  # castable cards in hand that protect it
    fizzles: bool  # every target of the threat is protected: it does nothing


def untapped_sources(state: dict[str, Any], seat: Any) -> list[SimpleNamespace]:
    """Our untapped lands as mana sources (``produces``) for ``_pip_matching``.

    A land whose colours the snapshot doesn't say (no text, no colour
    production) counts as any colour: an unknown never hides a response.
    """
    sources = []
    for card in state.get("battlefield") or []:
        if not isinstance(card, dict):
            continue
        if (card.get("controller_seat_id") or card.get("owner_seat_id")) != seat:
            continue
        if "land" not in str(card.get("type_line") or "").lower() or card.get("is_tapped"):
            continue
        colors = _land_colors(card)
        if not colors:
            known = card.get("oracle_text") or card.get("color_production")
            colors = frozenset({"C"}) if known else frozenset(COLORS)
        sources.append(SimpleNamespace(produces=colors))
    return sources


def affordable_at_instant_speed(card: dict, sources: list) -> bool:
    """An instant or Flash card (not Flashback) these sources can pay for."""
    info = hand_card({**card, "rarity": card.get("rarity") or "-"})
    if info.is_land or not info.instant_speed:
        return False
    return info.mana_value <= len(sources) and _pip_matching(info.pips, sources)


def _local_seat(state: dict[str, Any]) -> Any:
    seat = state.get("local_seat_id")
    if seat is not None:
        return seat
    for player in state.get("players") or []:
        if isinstance(player, dict) and player.get("is_local"):
            return player.get("seat_id")
    return None


def _clean(text: str) -> str:
    from arenamcp.opponent_tricks import clean_oracle

    return clean_oracle(text).lower()


def _effect_text(card: dict) -> str:
    """What casting ``card`` does at instant speed: an instant's text, a flash creature's enters trigger."""
    from arenamcp.opponent_tricks import _enters_text, clean_oracle

    cleaned = clean_oracle(str(card.get("oracle_text") or ""))
    types = str(card.get("type_line") or "").lower()
    if "instant" not in types and "creature" in types:
        return _enters_text(cleaned).lower()
    return cleaned.lower()


def _shield_kind(card: dict, target: dict, threat_text: str) -> str:
    """Return the supported mechanism, not a blanket promise of protection."""
    target_types = str(target.get("type_line") or "").lower()
    text = _effect_text(card)
    matches = [*_SHIELD.finditer(text), *_GROUP_SHIELD.finditer(text)]
    for match in matches:
        what = match.group(0).split(" you control")[0]
        if "permanent" not in what and not any(
            kind in target_types and kind in what for kind in ("creature", "planeswalker", "artifact")
        ):
            continue
        grant = match.group("grant")
        targeted = match.re is _SHIELD
        keywords = str(target.get("oracle_text") or "").lower()
        if targeted and ("shroud" in keywords or "shroud" in target.get("keywords", ())):
            continue
        # Unqualified hexproof/shroud stops opposing targeting regardless of color.
        if re.search(r"\b(?:hexproof(?! from)|shroud)\b", grant):
            return "targeting"
        if "protection from the color of your choice" in grant:
            # Do not promise protection from colorless sources or unknown colors.
            continue
        if _INDESTRUCTIBLE.search(grant) and not _NOT_DESTROY.search(threat_text):
            if re.search(r"\bdestroy\b", threat_text):
                return "destruction"
            if (
                "creature" in target_types
                and "planeswalker" not in target_types
                and re.search(r"\bdeals?\b.*\bdamage\b", threat_text)
            ):
                return "destruction"
    return ""


def _protects(card: dict, target: dict, threat_text: str) -> bool:
    return bool(_shield_kind(card, target, threat_text))


def shield_responses(state: dict[str, Any], *, payable_ids=None) -> list[ShieldResponse]:
    """Each opponent stack object targeting our permanent that a castable card in hand can protect."""
    seat = _local_seat(state)
    stack = state.get("stack") or []
    hand = [c for c in state.get("hand") or [] if isinstance(c, dict)]
    if seat is None or not stack or not hand:
        return []
    ours = {
        card.get("instance_id"): card
        for card in state.get("battlefield") or []
        if isinstance(card, dict) and (card.get("controller_seat_id") or card.get("owner_seat_id")) == seat
    }
    sources = untapped_sources(state, seat)
    castable = [
        c
        for c in hand
        if (
            affordable_at_instant_speed(c, sources)
            if payable_ids is None
            else c.get("instance_id") in payable_ids
        )
    ]
    if not castable:
        return []
    found: list[ShieldResponse] = []
    for obj in stack:
        if not isinstance(obj, dict):
            continue
        if (obj.get("controller_seat_id") or obj.get("owner_seat_id")) == seat:
            continue
        targets = [t for t in obj.get("targeting") or [] if t is not None]
        hit = [ours[t] for t in targets if t in ours]
        if not hit:
            continue
        threat_text = "\n".join(  # its effect, not an additional cost ("sacrifice a creature or pay {3}")
            line
            for line in _clean(str(obj.get("oracle_text") or "")).splitlines()
            if not line.startswith("as an additional cost")
        )
        for target in hit:
            answers = tuple(
                dict.fromkeys(
                    str(c.get("name") or "?") for c in castable if _protects(c, target, threat_text)
                )
            )
            if answers:
                found.append(
                    ShieldResponse(
                        threat=str(obj.get("name") or "an opponent's spell"),
                        target=str(target.get("name") or "your permanent"),
                        answers=answers,
                        fizzles=len(set(targets)) == 1
                        and all(
                            _shield_kind(c, target, threat_text) == "targeting"
                            for c in castable
                            if str(c.get("name") or "?") in answers
                        ),
                    )
                )
    return found


def shield_response_note(state: dict[str, Any]) -> str:
    """One prompt line per protectable target, or "" (no opponent removal aimed at us we can answer)."""
    lines = []
    for response in shield_responses(state):
        outcome = (
            "it then has no legal target and does nothing"
            if response.fizzles
            else "the supported protection applies to your permanent; other effects may still resolve"
        )
        lines.append(
            f"RESPOND NOW: OPP {response.threat} targets your {response.target}. "
            f"Cast {' or '.join(response.answers)} in response to protect it — {outcome}. "
            "Once it resolves the window is gone."
        )
    return "\n".join(lines)


def protective_pass(decision, state, chosen):
    """Narrow pass correction: a payable, unconditional group shield vs targeted removal.

    Never guesses a legal action, targets, payment, optional costs or spell mode.
    Other choices remain the planner's; this only prevents throwing away an
    established protection window with a pure group-protection instant in hand.
    """
    if chosen != ["pass"] or decision.request_type != "ActionsAvailable" or not decision.can_pass:
        return None
    local = _local_seat(state)
    turn = state.get("turn") or state.get("turn_info") or {}
    if local is None or turn.get("priority_player") != local:
        return None
    stack = state.get("stack") or []
    if not stack or (stack[0].get("controller_seat_id") or stack[0].get("owner_seat_id")) in (None, local):
        return None
    threat = stack[0]
    text = _clean(str(threat.get("oracle_text") or ""))
    # Only an explicit detrimental effect; no inference from a name or mere targeting.
    if not re.search(
        r"\b(?:exile|destroy) target|\bdeals?\b.*\bdamage to|\btarget.*gets? -\d+/-\d+|\breturn target.*hand",
        text,
    ):
        return None
    options = {
        o.meta.get("instanceId"): o
        for o in decision.options
        if o.payable is True and o.meta.get("actionType") == "ActionType_Cast"
    }
    hand = []
    for card in state.get("hand") or []:
        effect = _effect_text(card)
        # Exact full effect keeps conditional, modal, extra-cost and delayed shields out.
        if (
            card.get("instance_id") in options
            and "instant" in str(card.get("type_line") or "").lower()
            and re.fullmatch(
                r"(?:permanents|creatures|artifacts|planeswalkers) you control gain (?:hexproof|indestructible)(?: and (?:hexproof|indestructible))? until end of turn\.?",
                effect,
            )
        ):
            hand.append(card)
    for card in hand:
        responses = shield_responses({**state, "stack": [threat], "hand": [card]}, payable_ids=options)
        if not responses:
            continue
        option = options[card["instance_id"]]
        if not decision.selection_is_valid([option.option_id]):
            continue
        response = responses[0]
        mechanism = "make its target illegal" if response.fizzles else "protect against destruction"
        return (
            option.option_id,
            f"Cast {card['name']} before {response.threat} resolves to {mechanism} and save {response.target}.",
        )
    return None
