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
_TARGET_PROOF = re.compile(r"\b(?:hexproof|shroud|protection from)\b")
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


def _protects(card: dict, target: dict, threat_text: str) -> bool:
    """``card``'s effect can shield ``target`` from a spell whose text is ``threat_text``."""
    target_types = str(target.get("type_line") or "").lower()
    for match in _SHIELD.finditer(_effect_text(card)):
        what = match.group(0).split(" you control")[0]
        if "permanent" not in what:
            kinds = [t for t in ("creature", "planeswalker", "artifact") if t in what]
            if not any(kind in target_types for kind in kinds):
                continue  # "target creature you control" can't shield a planeswalker
        grant = match.group("grant")
        if _TARGET_PROOF.search(grant):
            return True
        if _INDESTRUCTIBLE.search(grant) and not _NOT_DESTROY.search(threat_text):
            return True
    return False


def shield_responses(state: dict[str, Any]) -> list[ShieldResponse]:
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
    castable = [c for c in hand if affordable_at_instant_speed(c, sources)]
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
                        fizzles=len(set(targets)) == 1,
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
            else "your permanent is no longer affected"
        )
        lines.append(
            f"RESPOND NOW: OPP {response.threat} targets your {response.target}. "
            f"Cast {' or '.join(response.answers)} in response to protect it — {outcome}. "
            "Once it resolves the window is gone."
        )
    return "\n".join(lines)
