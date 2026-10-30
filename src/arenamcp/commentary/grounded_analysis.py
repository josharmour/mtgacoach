"""Bounded factual analyst observations; no inference service or fabricated plays."""

import re
from dataclasses import replace

from arenamcp.player_names import actor_name

from .contracts import SpeechCategory, Utterance
from .timeline import event_lifetime, event_priority, position_fingerprint


def _label(observation, seat):
    name = actor_name(observation.public_state, seat)
    return "Your" if name == "You" else name + "'s"


def _cards(observation):
    return [c for rows in observation.public_state.get("zones", {}).values() for c in rows]


def _pair(first, text, observation, config, *, position=False, snapshot=False, scope=""):
    if config.speakers != 2 or not text or text in observation.recent_commentary:
        return [first]
    tail = replace(
        first,
        utterance_id=first.utterance_id + ":analyst",
        text=text,
        role="color_analyst",
        anchor_id=first.utterance_id,
        event_bound=not (position or snapshot),
        position_id=position_fingerprint(observation, scope) if position else "",
        position_scope=scope if position else "",
        expires_at=min(first.expires_at, observation.arrived_at + (8 if snapshot else 15))
        if position or snapshot
        else first.expires_at,
    )
    return [first, tail]


def outcome_exchange(observation, config):
    events = [e for e in observation.events if e.kind == "outcome"]
    if not events:
        return []
    event = max(events, key=lambda e: (event_priority(e, observation), e.received_at))
    if any(
        event_priority(other, observation) > event_priority(event, observation)
        for other in observation.events
    ):
        return []
    p = event.payload
    count = p.get("count")
    if type(count) is not int or not 1 <= count <= 250:
        return []
    owner = _label(observation, event.seat)
    actor = actor_name(observation.public_state, event.seat)
    category, origin, destination = p.get("category"), p.get("from_zone"), p.get("to_zone")
    cards = p.get("cards") or []
    names = [c.get("name") for c in cards if c.get("name") and c.get("name") != "Unknown card"]
    name = names[0] if count == 1 and names else None
    tail, position = "", False
    if category == "Mill" and origin == "library" and destination == "graveyard":
        text = f"{actor} milled {count} {'card' if count == 1 else 'cards'}."
        # Public high-mana cards provide the stakes without predicting a reanimation.
        ranked = sorted(
            cards,
            key=lambda c: sum(
                int(v) if v.isdigit() else 1 for v in re.findall(r"\{([^}]+)\}", c.get("mana_cost", ""))
            ),
            reverse=True,
        )
        highlights = list(
            dict.fromkeys(c.get("name") for c in ranked if c.get("name") and c.get("name") != "Unknown card")
        )[:2]
        if highlights:
            tail = "That stocked the graveyard with " + " and ".join(highlights) + "."
    elif category in {"Countered", "Counterspell"} and origin == "stack":
        text = f"{owner} {name or 'spell'} was countered."
        if cards and any("creature" in c.get("type_line", "").lower() for c in cards):
            tail = "That stopped the creature spell before it could join the battlefield."
        else:
            tail = "The counter stopped that spell from resolving."
    elif origin == "battlefield":
        if name:
            text = f"{owner} {name} " + ("was exiled." if destination == "exile" else "left the battlefield.")
        else:
            text = f"{actor} lost {count} permanents from the battlefield."
        lost_creatures = any("creature" in c.get("type_line", "").lower() for c in cards)
        if lost_creatures:
            remaining = [
                c
                for c in observation.public_state.get("zones", {}).get("battlefield", [])
                if c.get("controller_seat_id") == event.seat and "creature" in c.get("type_line", "").lower()
            ]
            tail = (
                f"{actor} has no creatures left on the battlefield."
                if not remaining
                else f"{actor} has {len(remaining)} {'creature' if len(remaining) == 1 else 'creatures'} left on the battlefield."
            )
            position = True
        elif destination == "exile":
            tail = "It went to exile, so ordinary graveyard recursion cannot bring it back."
    else:
        return []
    if config.persona == "esports":
        text = text.rstrip(".") + "!"
    first = Utterance(
        "aoa:" + event.event_id,
        text,
        SpeechCategory.COMMENTARY,
        observation.epoch,
        event.received_at + event_lifetime(event.kind),
        evidence_ids=(event.event_id,),
        snapshot_id=observation.snapshot_id,
        event_bound=True,
    )
    return _pair(first, tail, observation, config, position=position, scope="battlefield")


def add_analyst(first, event, observation, config):
    text, position, snapshot = "", False, False
    if event.kind == "life_change":
        after = event.payload.get("to")
        if type(after) is int and 0 < after <= 3:
            text = "That leaves very little room for another loss of life."
            position = True
    elif event.kind == "action_observed":
        intent = event.payload.get("coach_intent")
        if config.private_information and isinstance(intent, str) and len(intent) <= 240:
            # A statement of the purpose of a completed cast is retrospective,
            # not a prediction that damage, resolution or a block occurred.
            return _pair(first, intent, observation, config)
        card = next(
            (
                c
                for c in _cards(observation)
                if c.get("grp_id") == event.payload.get("grp_id") and c.get("grp_id")
            ),
            None,
        )
        if card:
            rules = str(card.get("oracle_text") or "").lower()
            # Specific mechanics, conditional where the spell has not resolved.
            if "when you cast this spell" in rules:
                text = "Its cast trigger is separate from the spell, so countering the spell alone does not stop that trigger."
            elif card not in observation.public_state.get("zones", {}).get("stack", []):
                return [first]
            elif re.search(r"(?:^|\n)counter target spell\.", rules):
                snapshot = True
                text = "That response threatens to stop a spell before it resolves."
            elif re.search(r"(?:^|\n)destroy all creatures\.", rules):
                snapshot = True
                text = "That threatens to clear the creatures, though indestructible can protect them."
            elif "each player mills" in rules and "graveyard" in rules and "battlefield" in rules:
                snapshot = True
                text = "Both graveyards could supply the next creatures or planeswalkers on the battlefield."
    return _pair(
        first,
        text,
        observation,
        config,
        position=position,
        snapshot=snapshot,
        scope="players" if event.kind == "life_change" else "",
    )
