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


@dataclass(frozen=True)
class DiscardThreat:
    threat: str
    opponent_chooses: bool
    instants_eligible: bool


_REVEAL_OR_LOOK_HAND_RE = re.compile(
    r"\b(?:reveals?\s+(?:their|his or her|your)\s+hand|looks?\s+at\s+[^.\n]*?\bhand)\b",
    re.IGNORECASE,
)
_REVEAL_CHOOSE_WHAT_RE = re.compile(
    r"\b(?:choose|exile)\s+(?:a|an|up to \w+|\d+)\s+(?P<what>[^.\n]*?\bcards?\b)"
    r"(?:\s+from\s+(?:it|that player'?s? hand|their hand))?",
    re.IGNORECASE,
)
_REVEAL_LOSS_RE = re.compile(
    r"\bdiscards?\s+(?:that|those|a|an|\d+|two|three|it|them)\b"
    r"|\bexiles?\s+(?:a|an|that|those|up to \w+|\d+)[^.\n]*\b(?:card|from\s+(?:it|that player'?s? hand|their hand))\b",
    re.IGNORECASE,
)
_BLIND_FORCED_DISCARD_RE = re.compile(
    r"(?<!\bunless that player )\b(?:target\s+(?:opponent|player)|each\s+(?:opponent|player)"
    r"|that player|defending player)\s+discards?\s+"
    r"(?:(?:a|an|\d+|two|three|four)\s+(?P<what>[^.\n]*?\bcards?\b)|their\s+hand|all\b)",
    re.IGNORECASE,
)
_NON_TYPE_PREFIX_RE = re.compile(
    r"\bnon[-\s]?(?:creature|land|artifact|enchantment|planeswalker|permanent|instant|sorcery|battle)\b",
    re.IGNORECASE,
)
_EXCLUDES_INSTANT_RE = re.compile(
    r"\b(?:creature|artifact|enchantment|planeswalker|battle|sorcery|land)\b",
    re.IGNORECASE,
)
_NON_INSTANT_RE = re.compile(r"\bnon[-\s]?instant\b", re.IGNORECASE)


def _instants_eligible(what: str) -> bool:
    if not what:
        return True
    low = what.lower()
    if _NON_INSTANT_RE.search(low):
        return False
    if re.search(r"\binstants?\b", low):
        return True
    stripped = _NON_TYPE_PREFIX_RE.sub("", low)
    return not bool(_EXCLUDES_INSTANT_RE.search(stripped))


def discard_threat_on_stack(state: dict[str, Any]) -> DiscardThreat | None:
    """Detect an opposing discard or hand-exile threat on the stack aimed at us."""
    seat = _local_seat(state)
    stack = state.get("stack") or []
    if seat is None or not stack:
        return None
    for obj in stack:
        if not isinstance(obj, dict):
            continue
        ctrl = obj.get("controller_seat_id") or obj.get("owner_seat_id")
        if ctrl in (None, seat):
            continue
        raw_lines = [
            line
            for line in _clean(str(obj.get("oracle_text") or "")).splitlines()
            if not line.strip().startswith("as an additional cost")
        ]
        text = "\n".join(raw_lines)
        if not text:
            continue
        targets = [t for t in obj.get("targeting") or [] if t is not None]
        global_opp = bool(re.search(r"\b(?:each\s+(?:opponent|player)|defending player)\b", text))
        if targets:
            if seat not in targets and not global_opp:
                continue
        elif not re.search(
            r"\b(?:target\s+(?:opponent|player)|each\s+(?:opponent|player)|defending player)\b",
            text,
        ):
            continue
        threat_name = str(obj.get("name") or "an opponent's spell")
        if _REVEAL_OR_LOOK_HAND_RE.search(text):
            choose = _REVEAL_CHOOSE_WHAT_RE.search(text)
            if (
                choose
                and _REVEAL_LOSS_RE.search(text)
                and not re.search(r"\bfrom\s+[^.\n]*graveyard\b", text)
            ):
                return DiscardThreat(
                    threat=threat_name,
                    opponent_chooses=True,
                    instants_eligible=_instants_eligible(choose.group("what") or ""),
                )
        if not re.search(r"\b(?:may discard|(?:its|their) controller discards)\b", text):
            blind = _BLIND_FORCED_DISCARD_RE.search(text)
            if blind:
                return DiscardThreat(
                    threat=threat_name,
                    opponent_chooses=False,
                    instants_eligible=_instants_eligible(blind.group("what") or ""),
                )
    return None


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


def discard_response_note(state: dict[str, Any]) -> str:
    """One prompt line when an opposing discard spell threatens a castable instant in hand."""
    threat = discard_threat_on_stack(state)
    if threat is None or not threat.instants_eligible:
        return ""
    seat = _local_seat(state)
    hand = [c for c in state.get("hand") or [] if isinstance(c, dict)]
    if seat is None or not hand:
        return ""
    sources = untapped_sources(state, seat)
    answers = tuple(
        dict.fromkeys(str(c.get("name") or "?") for c in hand if affordable_at_instant_speed(c, sources))
    )
    if not answers:
        return ""
    if threat.opponent_chooses:
        return (
            f"RESPOND NOW: OPP {threat.threat} will look at your hand and force you to discard. "
            f"Cast {' or '.join(answers)} in response before losing it to discard."
        )
    has_lands = any("land" in str(c.get("type_line") or "").lower() for c in hand)
    if len(hand) <= 2 or not has_lands:
        return (
            f"RESPOND NOW: OPP {threat.threat} forces a discard while your hand is tight. "
            f"Consider casting {' or '.join(answers)} in response rather than discarding it."
        )
    return ""


def discard_response_pass(decision, state, chosen):
    """Override a Pass into casting payable instant face burn before opposing discard resolves."""
    if chosen != ["pass"] or decision.request_type != "ActionsAvailable" or not decision.can_pass:
        return None
    local = _local_seat(state)
    turn = state.get("turn") or state.get("turn_info") or {}
    if local is None or turn.get("priority_player") != local:
        return None
    threat = discard_threat_on_stack(state)
    if threat is None or not threat.instants_eligible:
        return None
    hand = [c for c in state.get("hand") or [] if isinstance(c, dict)]
    if not threat.opponent_chooses:
        lands_in_hand = sum(1 for c in hand if "land" in str(c.get("type_line") or "").lower())
        nonlands_in_hand = len(hand) - lands_in_hand
        if not (lands_in_hand == 0 and nonlands_in_hand <= 1):
            return None

    from arenamcp.burn_timing import _cost, damage_only, face_damage, instant_speed

    by_id = {c.get("instance_id"): c for c in hand if c.get("instance_id") is not None}
    best: tuple[tuple[int, int], Any, dict] | None = None
    for option in getattr(decision, "options", ()) or ():
        meta = getattr(option, "meta", None) or {}
        if getattr(option, "payable", None) is not True or meta.get("actionType") != "ActionType_Cast":
            continue
        card = by_id.get(meta.get("instanceId"))
        if not card or not decision.selection_is_valid([option.option_id]):
            continue
        if not (instant_speed(card) and damage_only(card, state)):
            continue
        dmg = face_damage(card)
        if dmg <= 0:
            continue
        key = (dmg, _cost(card))
        if best is None or key > best[0]:
            best = (key, option, card)
    if best is None:
        return None
    _key, option, card = best
    name = str(card.get("name") or option.label)
    return (
        option.option_id,
        f"Cast {name} before {threat.threat} resolves and strips it from hand.",
    )
